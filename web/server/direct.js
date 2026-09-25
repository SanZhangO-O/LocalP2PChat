"use strict";

const fs = require("fs");
const net = require("net");

const M = require("./models");
const C = require("./crypto");
const U = require("./util");
const W = require("./wire");
const { OutgoingFileServer, downloadFileOffer } = require("./files");

const CONNECT_TIMEOUT = 8000;
const READ_TIMEOUT = 45000;
const PING_INTERVAL = 15000;
const REDIAL_BACKOFF = 5000;
const REDIAL_MAX_BACKOFF = 30000;
const REDIAL_LIFETIME = 600000;
const PRESENCE_SWEEP = 60000;
const REMOVED_MARK_TTL = 30 * 24 * 3600 * 1000;
const REQUEST_BOX_MAX = 50;

class DirectChatManager {
  constructor(identity) {
    this.identity = identity;
    this.events = {
      contactsChanged: null,
      messagesChanged: null,
      connectFailed: null,
      typingChanged: null,
      messageEdited: null,
      reactionChanged: null,
      pinChanged: null,
      event: null,
      sessionEstablished: null,
      sessionClosed: null,
      requestsChanged: null,
      marksChanged: null,
      chatMigrated: null,
      callSignal: null,
    };
    this.sessions = new Map();
    this.contacts = new Map();
    this.messages = new Map();
    this.myId = "";
    this.myName = "";
    this.myIp = "";
    this.myPort = 0;
    this.outbox = new Map();
    this.chatEndpoints = new Map();
    this.typingPeers = new Set();
    this.redialLoops = new Set();
    this.dialLocks = new Map();
    this.presenceTimer = null;
    this.presenceDialing = new Set();
    this.removedIds = new Map();
    this.removedEndpoints = new Map();
    this.removedIdEndpoint = new Map();
    this.contactRequests = [];
    this.stopped = false;
  }

  emit(name, ...args) {
    const cb = this.events[name];
    if (cb) {
      try {
        cb(...args);
      } catch (e) {
        /* ignore */
      }
    }
  }

  myPeer() {
    this.myIp = U.getLocalIpAddress() || this.myIp;
    return new M.Peer(this.myId, this.myName, this.myIp, this.myPort);
  }

  configure(myId, myName, myIp, myPort, savedContacts) {
    this.myId = myId;
    this.myName = myName;
    this.myIp = myIp;
    this.myPort = myPort;
    this.contacts = new Map((savedContacts || []).map((c) => [c.id, c]));
    this.announceOnline();
  }

  isConfigured() {
    return Boolean(this.myId);
  }

  contactsList() {
    return Array.from(this.contacts.values()).sort((a, b) =>
      a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1
    );
  }

  addContact(contact) {
    this.unmarkRemoved(
      contact.id,
      contact.ipAddress ? `${contact.ipAddress}:${contact.port}` : null
    );
    let changed = false;
    for (const oldId of Array.from(this.contacts.keys())) {
      const old = this.contacts.get(oldId);
      if (old.ipAddress === contact.ipAddress && old.port === contact.port && old.id !== contact.id) {
        if (contact.id.startsWith("ip:") && !oldId.startsWith("ip:")) return;
        this.contacts.delete(oldId);
        changed = true;
      }
    }
    const oldSame = this.contacts.get(contact.id);
    const same = oldSame && oldSame.id === contact.id && oldSame.name === contact.name &&
      oldSame.ipAddress === contact.ipAddress && oldSame.port === contact.port;
    if (!same) {
      if (
        oldSame &&
        oldSame.ipAddress &&
        contact.ipAddress &&
        oldSame.ipAddress !== contact.ipAddress
      ) {
        this.identity.forgetPeer(contact.id);
      }
      this.contacts.set(contact.id, contact);
      changed = true;
    }
    if (changed) this.emit("contactsChanged");
  }

  removeContact(contactId) {
    const contact = this.contacts.get(contactId);
    if (!contact) return;
    this.contacts.delete(contactId);
    this.markRemoved(
      contactId,
      contact.ipAddress ? `${contact.ipAddress}:${contact.port}` : null
    );
    this.emit("contactsChanged");
  }

  markRemoved(peerId, endpoint) {
    const now = Date.now();
    this.removedIds.set(peerId, now);
    if (endpoint) {
      this.removedEndpoints.set(endpoint, now);
      this.removedIdEndpoint.set(peerId, endpoint);
    }
    this.emit("marksChanged");
  }

  unmarkRemoved(peerId, endpoint) {
    let changed = false;
    if (this.removedIds.delete(peerId)) changed = true;
    const linked = this.removedIdEndpoint.get(peerId);
    if (linked && this.removedEndpoints.delete(linked)) changed = true;
    this.removedIdEndpoint.delete(peerId);
    if (endpoint && this.removedEndpoints.delete(endpoint)) changed = true;
    if (changed) this.emit("marksChanged");
  }

  isRemoved(peer) {
    const now = Date.now();
    const t = this.removedIds.get(peer.id);
    if (t != null && now - t < REMOVED_MARK_TTL) return true;
    if (peer.ipAddress) {
      const te = this.removedEndpoints.get(`${peer.ipAddress}:${peer.port}`);
      if (te != null && now - te < REMOVED_MARK_TTL) return true;
    }
    return false;
  }

  restoreRemovedMarks(ids, endpoints) {
    const now = Date.now();
    for (const [k, v] of Object.entries(ids || {})) {
      if (now - v < REMOVED_MARK_TTL) this.removedIds.set(k, v);
    }
    for (const [k, v] of Object.entries(endpoints || {})) {
      if (now - v < REMOVED_MARK_TTL) this.removedEndpoints.set(k, v);
    }
  }

  removedMarks() {
    const now = Date.now();
    const ids = {};
    for (const [k, v] of this.removedIds) if (now - v < REMOVED_MARK_TTL) ids[k] = v;
    const endpoints = {};
    for (const [k, v] of this.removedEndpoints) if (now - v < REMOVED_MARK_TTL) endpoints[k] = v;
    return { ids, endpoints };
  }

  recordContactRequest(peer, fromRemoved, peerFingerprint) {
    const entry = new M.ContactRequest(
      peer.id,
      peer.name,
      peer.ipAddress,
      peer.port,
      fromRemoved,
      U.nowMs(),
      peerFingerprint || ""
    );
    const sameSlot = (other) =>
      other.id === entry.id || (other.ip === entry.ip && other.port === entry.port);
    const isNew = !this.contactRequests.some(sameSlot);
    this.contactRequests = this.contactRequests
      .filter((r) => !sameSlot(r))
      .concat([entry])
      .slice(-REQUEST_BOX_MAX);
    if (isNew) {
      const suffix = fromRemoved ? "\uff08\u5df2\u79fb\u9664\u7684\u6210\u5458\uff09" : "";
      this.emit("event", `${peer.name} \u8bf7\u6c42\u6dfb\u52a0\u4f60\u4e3a\u6210\u5458${suffix}`);
    }
    this.emit("requestsChanged");
  }

  acceptContactRequest(requestId) {
    const req = this.contactRequests.find((r) => r.id === requestId);
    if (!req) return;
    this.contactRequests = this.contactRequests.filter((r) => r.id !== requestId);
    const peer = new M.Peer(req.id, req.name, req.ip, req.port);
    this.addContact(peer);
    this.emit("event", `\u5df2\u6dfb\u52a0 ${req.name}`);
    this.startChat(peer, true).catch(() => {});
    this.announceOnline();
    this.emit("requestsChanged");
  }

  ignoreContactRequest(requestId) {
    const before = this.contactRequests.length;
    this.contactRequests = this.contactRequests.filter((r) => r.id !== requestId);
    if (this.contactRequests.length !== before) this.emit("requestsChanged");
  }

  restoreContactRequests(saved) {
    if (!saved || !saved.length) return;
    const taken = (other) =>
      this.contactRequests.some(
        (k) => k.id === other.id || (k.ip === other.ip && k.port === other.port)
      );
    const kept = saved.filter((r) => !taken(r));
    if (kept.length) {
      this.contactRequests = this.contactRequests.concat(kept).slice(-REQUEST_BOX_MAX);
      this.emit("requestsChanged");
    }
  }

  announceOnline() {
    this._dialDeadContacts();
    if (this.presenceTimer) clearInterval(this.presenceTimer);
    this.presenceTimer = setInterval(() => {
      if (this.stopped) return;
      this._dialDeadContacts();
    }, PRESENCE_SWEEP);
  }

  _dialDeadContacts() {
    if (!this.myId) return;
    const live = new Set(
      Array.from(this.sessions.entries())
        .filter(([, s]) => s.alive)
        .map(([id]) => id)
    );
    for (const contact of this.contacts.values()) {
      if (!contact.ipAddress || contact.port <= 0) continue;
      if (live.has(contact.id)) continue;
      if (!contact.id.startsWith("ip:") && this.myId >= contact.id) continue;
      if (this.redialLoops.has(contact.id)) continue;
      if (this.presenceDialing.has(contact.id)) continue;
      this.presenceDialing.add(contact.id);
      this.startChat(contact, true)
        .catch(() => {})
        .finally(() => {
          this.presenceDialing.delete(contact.id);
        });
    }
  }

  messagesFor(peerId) {
    return this.messages.get(peerId) || [];
  }

  seedMessages(peerId, messages) {
    const current = this.messages.get(peerId) || [];
    const ids = new Set(current.map((m) => m.id));
    const merged = current.concat(messages.filter((m) => !ids.has(m.id)));
    merged.sort((a, b) => a.timestamp - b.timestamp);
    this.messages.set(peerId, merged);
    this.emit("messagesChanged", peerId);
  }

  seedLastMessage(peerId, message) {
    const current = this.messages.get(peerId) || [];
    if (!current.some((m) => m.id === message.id)) {
      current.push(message);
      current.sort((a, b) => a.timestamp - b.timestamp);
      this.messages.set(peerId, current);
    }
  }

  _appendMessage(peerId, msg) {
    if (!this.messages.has(peerId)) this.messages.set(peerId, []);
    this.messages.get(peerId).push(msg);
    this.emit("messagesChanged", peerId);
  }

  _removeMessage(peerId, messageId) {
    const msgs = this.messages.get(peerId);
    if (!msgs) return;
    const before = msgs.length;
    const next = msgs.filter((m) => m.id !== messageId);
    if (next.length !== before) {
      this.messages.set(peerId, next);
      this.emit("messagesChanged", peerId);
    }
  }

  _setPending(peerId, messageId, pending) {
    const msgs = this.messages.get(peerId);
    if (!msgs) return;
    for (const m of msgs) {
      if (m.id === messageId) {
        if (m.pending !== pending) {
          m.pending = pending;
          this.emit("messagesChanged", peerId);
        }
        break;
      }
    }
  }

  _applyLocalEdit(peerId, messageId, newContent, editorId) {
    const msgs = this.messages.get(peerId);
    if (!msgs) return false;
    for (const m of msgs) {
      if (m.id === messageId) {
        if (m.senderId === editorId && (m.content !== newContent || !m.edited)) {
          m.content = newContent;
          m.edited = true;
          this.emit("messagesChanged", peerId);
          return true;
        }
        break;
      }
    }
    return false;
  }

  _setPeerTyping(peerId, active) {
    const was = this.typingPeers.has(peerId);
    if (active) this.typingPeers.add(peerId);
    else this.typingPeers.delete(peerId);
    if (was !== active) this.emit("typingChanged", peerId, active);
  }

  _applyReadReceipt(peerId, upToId) {
    const msgs = this.messages.get(peerId);
    if (!msgs || !msgs.length) return;
    const target = msgs.find((m) => m.id === upToId);
    if (!target) return;
    let changed = false;
    for (const m of msgs) {
      if (m.isFromMe && !m.read && m.timestamp <= target.timestamp) {
        m.read = true;
        changed = true;
      }
    }
    if (changed) this.emit("messagesChanged", peerId);
  }

  _applyReaction(peerId, messageId, emoji, senderId, active) {
    const msgs = this.messages.get(peerId);
    if (!msgs) return;
    const target = msgs.find((m) => m.id === messageId);
    if (!target) return;
    target.reactions = target.reactions || {};
    const list = target.reactions[emoji] || [];
    const idx = list.indexOf(senderId);
    if (active && idx < 0) list.push(senderId);
    if (!active && idx >= 0) list.splice(idx, 1);
    if (list.length) target.reactions[emoji] = list;
    else delete target.reactions[emoji];
    this.emit("messagesChanged", peerId);
  }

  _applyPin(peerId, messageId, senderId, active) {
    const msgs = this.messages.get(peerId);
    if (!msgs) return;
    const target = msgs.find((m) => m.id === messageId);
    if (!target) return;
    target.pinned = active;
    target.pinnedBy = active ? senderId : null;
    this.emit("messagesChanged", peerId);
  }

  openChatEndpoint(contact) {
    this.chatEndpoints.set(contact.id, `${contact.ipAddress}:${contact.port}`);
  }

  async startChat(peer, quiet = false) {
    if (!this.myId || peer.id === this.myId) return null;
    const existing = this.sessions.get(peer.id);
    if (existing && existing.alive) return peer.id;
    const prev = this.dialLocks.get(peer.id) || Promise.resolve();
    const dialPromise = prev.catch(() => {}).then(() => this._startChatLocked(peer, quiet));
    const tail = dialPromise.catch(() => {});
    this.dialLocks.set(peer.id, tail);
    return dialPromise;
  }

  async _startChatLocked(peer, quiet) {
    const existing = this.sessions.get(peer.id);
    if (existing && existing.alive) return peer.id;
    this.addContact(peer);
    return this._dialPeer(peer, quiet);
  }

  async _dialPeer(peer, quiet) {
    let socket = null;
    try {
      socket = await new Promise((resolve, reject) => {
        const s = net.connect({ host: peer.ipAddress, port: peer.port }, () => resolve(s));
        s.setTimeout(CONNECT_TIMEOUT, () => {
          s.destroy();
          reject(new Error("timeout"));
        });
        s.on("error", (err) => reject(err));
      });
      socket.setNoDelay(true);
      const channel = new W.LineChannel(socket);
      const wire = new W.Wire(channel);
      const expectedId = peer.id && !peer.id.startsWith("ip:") ? peer.id : null;
      const peerIdent = await W.Handshake.initiateDirect(wire, this.identity, expectedId, () => {
        this.emit(
          "event",
          `\u5b89\u5168\u8b66\u544a\uff1a${peer.name} \u7684\u8bbe\u5907\u8eab\u4efd\u53d1\u751f\u53d8\u5316\uff0c\u8fde\u63a5\u5df2\u62d2\u7edd\uff08\u53ef\u80fd\u5b58\u5728\u4e2d\u95f4\u4eba\u653b\u51fb\uff09`
        );
      });
      wire.sendPacket(new M.NetworkPacket({ type: W.DIRECT_HELLO, peer: this.myPeer() }));
      const ack = await wire.recvPacket(15000);
      if (ack && ack.type === W.DIRECT_PENDING) {
        if (!quiet) {
          this.emit(
            "event",
            `\u5df2\u5411 ${peer.name} \u53d1\u9001\u8fde\u63a5\u8bf7\u6c42\uff0c\u7b49\u5f85\u5bf9\u65b9\u5728\u5176\u8bbe\u5907\u4e0a\u786e\u8ba4`
          );
        }
        channel.destroy();
        return null;
      }
      if (!ack || ack.type !== W.DIRECT_ACK || !ack.peer) throw new Error("bad direct_ack");
      const remote = ack.peer;
      if (peerIdent && !this.identity.checkPeer(remote.id, peerIdent)) {
        this.emit(
          "event",
          `\u5b89\u5168\u8b66\u544a\uff1a${remote.name} \u7684\u8bbe\u5907\u8eab\u4efd\u53d1\u751f\u53d8\u5316\uff0c\u8fde\u63a5\u5df2\u62d2\u7edd\uff08\u53ef\u80fd\u5b58\u5728\u4e2d\u95f4\u4eba\u653b\u51fb\uff09`
        );
        throw new Error("peer identity changed");
      }
      this._onEstablished(socket, channel, wire, remote);
      this._migrateAliasesFor(`${peer.ipAddress}:${peer.port}`, remote.id);
      const session = this.sessions.get(remote.id);
      if (session && session.alive) {
        this._flushOutbox(remote.id, session);
      }
      return remote.id;
    } catch (e) {
      if (!quiet) {
        let reason = "\u672a\u77e5\u9519\u8bef";
        if (String(e.message) === "timeout") {
          reason = "\u65e0\u54cd\u5e94\uff08\u8bf7\u786e\u8ba4\u5bf9\u65b9\u5e94\u7528\u5728\u8fd0\u884c\u4e14\u5728\u540c\u4e00\u7f51\u7edc\uff09";
        } else if (String(e.message) === "bad direct_ack") {
          reason = "\u5bf9\u65b9\u672a\u63a5\u53d7\u8fde\u63a5\uff08\u5e94\u7528\u521a\u9000\u51fa\u3001\u4e0d\u5728\u540c\u4e00\u7f51\u7edc\uff0c\u6216\u5df2\u88ab\u5bf9\u65b9\u79fb\u9664\uff09";
        } else if (e.code === "ECONNREFUSED") {
          reason = "\u8fde\u63a5\u88ab\u62d2\u7edd\uff08\u5bf9\u65b9\u5e94\u7528\u672a\u8fd0\u884c\u6216\u7aef\u53e3\u4e0d\u5bf9\uff09";
        } else if (e.code === "EHOSTUNREACH" || e.code === "ENETUNREACH") {
          reason = "\u7f51\u7edc\u4e0d\u53ef\u8fbe";
        } else if (e instanceof W.WireException) {
          reason = e.message;
        }
        this.emit("connectFailed", peer, reason);
      }
      if (socket) {
        try {
          socket.destroy();
        } catch (e2) {
          /* ignore */
        }
      }
      return null;
    }
  }

  async handleDirectHello(socket, channel, wire, packet, peerIdent) {
    const peer = packet.peer;
    if (!peer || peer.id === this.myId) {
      channel.destroy();
      return;
    }
    const removed = this.isRemoved(peer);
    const known = this.contacts.has(peer.id);
    if (removed || !known) {
      const fingerprint = peerIdent ? this.identity.peerFingerprint(peerIdent) : "";
      this.recordContactRequest(peer, removed, fingerprint);
      try {
        wire.sendPacket(new M.NetworkPacket({ type: W.DIRECT_PENDING }));
      } catch (e) {
        /* ignore */
      }
      channel.gracefulClose();
      return;
    }
    const identityProven =
      peerIdent != null &&
      this.identity.hasPeer(peer.id) &&
      this.identity.checkPeer(peer.id, peerIdent, false);
    if (!identityProven) {
      let actualIp = "";
      try {
        actualIp = socket.remoteAddress || "";
      } catch (e) {
        actualIp = "";
      }
      actualIp = actualIp.replace(/^::ffff:/, "");
      const fromLoopback = actualIp.startsWith("127.") || actualIp === "::1";
      if (!fromLoopback && actualIp && peer.ipAddress && actualIp !== peer.ipAddress) {
        this.emit(
          "event",
          `\u5b89\u5168\u8b66\u544a\uff1a${peer.name} \u58f0\u79f0\u7684\u5730\u5740\u4e0e\u8fde\u63a5\u6765\u6e90\u4e0d\u4e00\u81f4\uff0c\u8fde\u63a5\u5df2\u62d2\u7edd`
        );
        channel.destroy();
        return;
      }
      const existing = this.contacts.get(peer.id);
      if (
        existing &&
        existing.ipAddress &&
        peer.ipAddress &&
        existing.ipAddress !== peer.ipAddress
      ) {
        this.emit(
          "event",
          `\u5b89\u5168\u8b66\u544a\uff1a${peer.name} \u7684\u5730\u5740\u4e0e\u5df2\u77e5\u6210\u5458\u4e0d\u4e00\u81f4\uff0c\u8fde\u63a5\u5df2\u62d2\u7edd`
        );
        channel.destroy();
        return;
      }
    }
    if (peerIdent && !this.identity.checkPeer(peer.id, peerIdent)) {
      this.emit(
        "event",
        `\u5b89\u5168\u8b66\u544a\uff1a${peer.name} \u7684\u8bbe\u5907\u8eab\u4efd\u53d1\u751f\u53d8\u5316\uff0c\u8fde\u63a5\u5df2\u62d2\u7edd\uff08\u53ef\u80fd\u5b58\u5728\u4e2d\u95f4\u4eba\u653b\u51fb\uff09`
      );
      channel.destroy();
      return;
    }
    try {
      wire.sendPacket(new M.NetworkPacket({ type: W.DIRECT_ACK, peer: this.myPeer() }));
    } catch (e) {
      channel.destroy();
      return;
    }
    this.addContact(peer);
    this._onEstablished(socket, channel, wire, peer);
  }

  sendPacket(peerId, packet) {
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return false;
    this._putSend(session, packet);
    return true;
  }

  sendMessage(peerId, content) {
    if (!U.isValidContent(content)) return false;
    const contact = this.contacts.get(peerId);
    const session = this.sessions.get(peerId);
    const alive = Boolean(session && session.alive);
    if (!session && !contact) return false;
    const msg = new M.ChatMessage({
      id: U.uuid(),
      content,
      timestamp: U.nowMs(),
      senderId: this.myId,
      senderName: this.myName,
      isFromMe: true,
      pending: !alive,
    });
    this._appendMessage(peerId, msg);
    if (alive) {
      this._putSend(
        session,
        new M.NetworkPacket({ type: "chat", message: msg }),
        null,
        () => this._restoreUndelivered(peerId, [msg])
      );
    } else if (contact) {
      this._enqueuePending(peerId, contact, msg);
    }
    return true;
  }

  sendTyping(peerId, active) {
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return;
    try {
      this._putSend(
        session,
        new M.NetworkPacket({
          type: "typing",
          groupId: "direct:" + this.myId,
          senderId: this.myId,
          active: Boolean(active),
        })
      );
    } catch (e) {
      /* ignore */
    }
  }

  sendReadReceipt(peerId, upToId) {
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return;
    try {
      this._putSend(
        session,
        new M.NetworkPacket({
          type: "read_receipt",
          groupId: "direct:" + this.myId,
          upToId,
          readerId: this.myId,
        })
      );
    } catch (e) {
      /* ignore */
    }
  }

  async editMessage(peerId, messageId, newContent) {
    if (!U.isValidContent(newContent)) return false;
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return false;
    const target = (this.messages.get(peerId) || []).find((m) => m.id === messageId);
    if (!target || target.senderId !== this.myId) return false;
    try {
      this._putSend(
        session,
        new M.NetworkPacket({
          type: "edit_message",
          groupId: "direct:" + peerId,
          messageId,
          senderId: this.myId,
          newContent,
        })
      );
    } catch (e) {
      return false;
    }
    this._applyLocalEdit(peerId, messageId, newContent, this.myId);
    const msgs = this.messages.get(peerId) || [];
    const t = msgs.find((m) => m.id === messageId);
    if (t) {
      t.edited = true;
      this.emit("messagesChanged", peerId);
    }
    return true;
  }

  sendDirectReaction(peerId, messageId, emoji, active) {
    emoji = U.sanitizeEmoji(emoji);
    if (!emoji) return false;
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return false;
    try {
      this._putSend(
        session,
        new M.NetworkPacket({
          type: "reaction",
          groupId: "direct:" + peerId,
          messageId,
          senderId: this.myId,
          emoji,
          active: Boolean(active),
        })
      );
    } catch (e) {
      /* ignore */
    }
    return true;
  }

  sendDirectPin(peerId, messageId, active) {
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return false;
    try {
      this._putSend(
        session,
        new M.NetworkPacket({
          type: "pin_message",
          groupId: "direct:" + peerId,
          messageId,
          senderId: this.myId,
          active: Boolean(active),
        })
      );
    } catch (e) {
      /* ignore */
    }
    return true;
  }

  _enqueuePending(peerId, contact, msg) {
    let q = this.outbox.get(peerId);
    const first = !q;
    if (!q) {
      q = [];
      this.outbox.set(peerId, q);
    }
    q.push(msg);
    this.chatEndpoints.set(peerId, `${contact.ipAddress}:${contact.port}`);
    if (first) {
      this.emit("event", "\u5bf9\u65b9\u672a\u5728\u7ebf\uff0c\u6d88\u606f\u5c06\u5728\u5bf9\u65b9\u4e0a\u7ebf\u540e\u81ea\u52a8\u53d1\u9001");
    }
    this._ensureRedialLoop(peerId);
    const session = this.sessions.get(peerId);
    if (session && session.alive) {
      this._flushOutbox(peerId, session);
    }
  }

  _ensureRedialLoop(peerId) {
    if (this.redialLoops.has(peerId)) return;
    this.redialLoops.add(peerId);
    (async () => {
      try {
        const started = Date.now();
        let backoff = REDIAL_BACKOFF;
        while (Date.now() - started < REDIAL_LIFETIME) {
          const q = this.outbox.get(peerId);
          const contact = this.contacts.get(peerId);
          const session = this.sessions.get(peerId);
          if (!q || !q.length) break;
          if (session && session.alive) {
            this._flushOutbox(peerId, session);
            break;
          }
          if (!contact) break;
          await this.startChat(contact, true).catch(() => {});
          const q2 = this.outbox.get(peerId);
          const session2 = this.sessions.get(peerId);
          if (!q2 || !q2.length || (session2 && session2.alive)) break;
          await U.sleep(backoff);
          backoff = Math.min(backoff * 2, REDIAL_MAX_BACKOFF);
        }
      } finally {
        this.redialLoops.delete(peerId);
        const q = this.outbox.get(peerId);
        const contact = this.contacts.get(peerId);
        const session = this.sessions.get(peerId);
        if (q && q.length && (!session || !session.alive) && contact) {
          this._ensureRedialLoop(peerId);
        }
      }
    })();
  }

  _flushOutbox(peerId, session) {
    const q = this.outbox.get(peerId);
    if (!q || !q.length) return;
    const toSend = q.splice(0, q.length);
    for (const msg of toSend) {
      this._putSend(
        session,
        new M.NetworkPacket({ type: "chat", message: msg }),
        () => this._setPending(peerId, msg.id, false),
        () => this._restoreUndelivered(peerId, [msg])
      );
    }
  }

  _putSend(session, packet, onSent = null, onFailed = null) {
    session.sendQueue.push({ packet, onSent, onFailed });
    if (!session.alive) {
      if (onFailed) onFailed();
      return;
    }
    this._pumpSession(session);
  }

  _drainFailed(session) {
    for (const item of session.sendQueue.splice(0)) {
      if (item.onFailed) item.onFailed();
    }
  }

  _pumpSession(session) {
    if (session.pumping) return;
    session.pumping = true;
    (async () => {
      while (session.alive && session.sendQueue.length > 0) {
        const item = session.sendQueue.shift();
        try {
          session.wire.sendPacket(item.packet);
          if (item.onSent) item.onSent();
        } catch (e) {
          session.channel.destroy();
          session.alive = false;
          if (item.onFailed) item.onFailed();
          this._drainFailed(session);
          break;
        }
      }
      session.pumping = false;
      if (!session.alive) this._drainFailed(session);
    })();
  }

  _restoreUndelivered(peerId, msgs) {
    let q = this.outbox.get(peerId);
    if (!q) {
      q = [];
      this.outbox.set(peerId, q);
    }
    const ids = new Set(q.map((m) => m.id));
    const restored = msgs.filter((m) => !ids.has(m.id));
    if (!restored.length) return;
    q.push(...restored);
    for (const m of restored) this._setPending(peerId, m.id, true);
    if (!this.stopped) this._ensureRedialLoop(peerId);
  }

  _mergeChatState(fromKey, toKey) {
    if (fromKey === toKey) return;
    const fromMsgs = this.messages.get(fromKey);
    if (!fromMsgs) return;
    this.messages.delete(fromKey);
    const current = this.messages.get(toKey) || [];
    const ids = new Set(current.map((m) => m.id));
    const merged = current.concat(fromMsgs.filter((m) => !ids.has(m.id)));
    merged.sort((a, b) => a.timestamp - b.timestamp);
    this.messages.set(toKey, merged);
    const q = this.outbox.get(fromKey);
    if (q && q.length) {
      const toQ = this.outbox.get(toKey) || [];
      this.outbox.set(toKey, toQ.concat(q));
    }
    this.outbox.delete(fromKey);
  }

  _migrateAliasesFor(endpoint, realId) {
    const keys = new Set([...this.chatEndpoints.keys(), ...this.outbox.keys()]);
    const aliases = Array.from(keys).filter(
      (k) => k !== realId && this.chatEndpoints.get(k) === endpoint
    );
    for (const alias of aliases) this.chatEndpoints.delete(alias);
    const droppedPlaceholder = this.contacts.get(`ip:${endpoint}`);
    if (droppedPlaceholder) {
      this.contacts.delete(`ip:${endpoint}`);
      this.emit("contactsChanged");
    }
    for (const alias of aliases) {
      this._mergeChatState(alias, realId);
      this.emit("chatMigrated", alias, realId);
    }
  }

  restorePending(peerId, messages) {
    if (!messages || !messages.length) return;
    this.seedMessages(peerId, messages);
    let q = this.outbox.get(peerId);
    if (!q) {
      q = [];
      this.outbox.set(peerId, q);
    }
    q.push(...messages);
    const contact = this.contacts.get(peerId);
    let endpoint = contact ? `${contact.ipAddress}:${contact.port}` : null;
    if (endpoint == null && peerId.startsWith("ip:")) endpoint = peerId.slice(3);
    if (endpoint != null) this.chatEndpoints.set(peerId, endpoint);
    if (contact) this._ensureRedialLoop(peerId);
  }

  async sendFile(peerId, filePath) {
    const session = this.sessions.get(peerId);
    if (!session || !session.alive) return null;
    let stat;
    try {
      stat = fs.statSync(filePath);
    } catch (e) {
      return null;
    }
    const fileSize = stat.size;
    const fileName = U.baseName(filePath);
    if (!U.isValidContent(fileName)) return null;
    if (fileSize > 512 * 1024 * 1024) return null;
    const fileKey = C.randomBytes(C.KEY_LEN);
    const fileServer = new OutgoingFileServer(U.uuid(), filePath, fileSize, fileKey);
    let port;
    try {
      port = await fileServer.start();
    } catch (e) {
      return null;
    }
    const myIp = U.getLocalIpAddress() || this.myIp;
    const fileInfo = new M.FileInfo(
      fileServer.fileId,
      fileName,
      fileSize,
      myIp,
      port,
      C.toB64(fileKey),
      U.detectMediaKind(fileName)
    );
    const msg = new M.ChatMessage({
      id: fileServer.fileId,
      content: fileName,
      timestamp: U.nowMs(),
      senderId: this.myId,
      senderName: this.myName,
      isFromMe: true,
      fileInfo,
    });
    this._appendMessage(peerId, msg);
    this._putSend(
      session,
      new M.NetworkPacket({ type: "file_message", message: msg }),
      null,
      () => {
        fileServer.close();
        this._removeMessage(peerId, fileServer.fileId);
        this.emit("event", "\u6587\u4ef6\u53d1\u9001\u5931\u8d25\uff0c\u8bf7\u91cd\u8bd5");
      }
    );
    return msg;
  }

  async   downloadFile(fileInfo, targetPath, options = {}) {
    if (typeof options === "function") options = { onProgress: options };
    return downloadFileOffer(fileInfo, targetPath, options);
  }

  deleteMessage(peerId, messageId, senderId) {
    const session = this.sessions.get(peerId);
    let wasPending = false;
    if (senderId === this.myId) {
      const q = this.outbox.get(peerId);
      if (q) {
        const before = q.length;
        const next = q.filter((m) => m.id !== messageId);
        wasPending = next.length !== before;
        this.outbox.set(peerId, next);
      }
    }
    if (senderId === this.myId && session && !wasPending) {
      this._putSend(
        session,
        new M.NetworkPacket({ type: "delete_message", messageId, senderId: this.myId })
      );
    }
    this._removeMessage(peerId, messageId);
  }

  isChatAlive(peerId) {
    const s = this.sessions.get(peerId);
    return Boolean(s && s.alive);
  }

  shutdown() {
    this.stopped = true;
    if (this.presenceTimer) clearInterval(this.presenceTimer);
    for (const session of this.sessions.values()) {
      session.alive = false;
      session.channel.destroy();
    }
    this.sessions.clear();
  }

  _onEstablished(socket, channel, wire, peer) {
    this.addContact(peer);
    this._migrateAliasesFor(`${peer.ipAddress}:${peer.port}`, peer.id);
    const session = {
      peerId: peer.id,
      peerName: peer.name,
      socket,
      channel,
      wire,
      alive: true,
      sendQueue: [],
      pumping: false,
    };
    const previous = this.sessions.get(peer.id);
    this.sessions.set(peer.id, session);
    if (previous && previous !== session) {
      previous.alive = false;
      previous.channel.destroy();
      this._drainFailed(previous);
    }
    this.emit("event", `\u5df2\u8fde\u63a5 ${peer.name}`);
    this.emit("sessionEstablished", peer.id);
    this._flushOutbox(peer.id, session);
    this._readLoop(session);
    this._pingLoop(session);
  }

  _readLoop(session) {
    (async () => {
      while (session.alive) {
        let packet;
        try {
          packet = await session.wire.recvPacket(READ_TIMEOUT);
        } catch (e) {
          break;
        }
        if (packet == null) break;
        try {
          this._processPacket(session, packet);
        } catch (e) {
          /* ignore */
        }
      }
      this._sessionClosed(session);
    })();
  }

  _processPacket(session, packet) {
    const peerId = session.peerId;
    if ((packet.type === "chat" || packet.type === "file_message") && packet.message) {
      const msg = packet.message;
      if (msg.senderId !== peerId || !U.isValidContent(msg.content)) return;
      const dup = (this.messages.get(peerId) || []).some((m) => m.id === msg.id);
      if (dup) return;
      this._appendMessage(peerId, msg.markedFromMe(this.myId));
      if (packet.type === "chat") {
        this.sendReadReceipt(peerId, msg.id);
      }
    } else if (packet.type === "typing" && packet.active != null) {
      if (packet.senderId === peerId && packet.groupId === "direct:" + peerId) {
        this._setPeerTyping(peerId, Boolean(packet.active));
      }
    } else if (packet.type === "read_receipt" && packet.upToId) {
      if (packet.readerId === peerId && packet.groupId === "direct:" + peerId) {
        this._applyReadReceipt(peerId, packet.upToId);
      }
    } else if (packet.type === "delete_message" && packet.messageId) {
      const sender = packet.senderId;
      if (sender !== peerId) return;
      const target = (this.messages.get(peerId) || []).find((m) => m.id === packet.messageId);
      if (target && target.senderId === sender) {
        this._removeMessage(peerId, packet.messageId);
      }
    } else if (packet.type === "edit_message" && packet.messageId && packet.newContent != null) {
      if (packet.senderId === peerId && this._applyLocalEdit(peerId, packet.messageId, packet.newContent, peerId)) {
        this.emit("messageEdited", peerId, packet.messageId, packet.newContent);
      }
    } else if (packet.type === "reaction" && packet.messageId && packet.emoji) {
      if (packet.senderId === peerId) {
        this._applyReaction(peerId, packet.messageId, packet.emoji, packet.senderId, Boolean(packet.active));
        this.emit("reactionChanged", peerId, packet.messageId, packet.emoji, packet.active);
      }
    } else if (packet.type === "pin_message" && packet.messageId) {
      if (packet.senderId === peerId) {
        this._applyPin(peerId, packet.messageId, packet.senderId, Boolean(packet.active));
        this.emit("pinChanged", peerId, packet.messageId, packet.active);
      }
    } else if (packet.type === "ping") {
      try {
        session.wire.sendPacket(new M.NetworkPacket({ type: "pong" }));
      } catch (e) {
        /* ignore */
      }
    }
  }

  _pingLoop(session) {
    const timer = setInterval(() => {
      if (!session.alive) {
        clearInterval(timer);
        return;
      }
      try {
        session.wire.sendPacket(new M.NetworkPacket({ type: "ping" }));
      } catch (e) {
        clearInterval(timer);
      }
    }, PING_INTERVAL);
  }

  _sessionClosed(session) {
    session.alive = false;
    const replaced = this.sessions.get(session.peerId) !== session;
    if (!replaced) this.sessions.delete(session.peerId);
    session.channel.destroy();
    if (!replaced) {
      this._setPeerTyping(session.peerId, false);
      this.emit("event", `\u4e0e ${session.peerName} \u7684\u76f4\u804a\u8fde\u63a5\u5df2\u65ad\u5f00`);
      this.emit("sessionClosed", session.peerId);
    }
  }
}

module.exports = { DirectChatManager };
