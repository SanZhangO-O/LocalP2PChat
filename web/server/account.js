"use strict";

const fs = require("fs");
const path = require("path");

const U = require("./util");
const M = require("./models");
const { DeviceIdentity, GA } = require("./identity");
const { Store } = require("./store");
const { DirectChatManager } = require("./direct");
const { GroupApp } = require("./group");
const { SharedListener } = require("./tcpserver");
const { downloadFileOffer } = require("./files");

const BIND_RETRY_MS = 5000;
const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;
const UPLOAD_TTL_MS = 24 * 3600 * 1000;

class Account {
  constructor(server, record) {
    this.server = server;
    this.id = record.id;
    this.username = record.username;
    this.tcpPort = record.tcpPort;
    this.dataDir = path.join(server.dataDir, "accounts", this.id);
    this.uploadsDir = path.join(this.dataDir, "uploads");
    this.downloadsDir = path.join(this.dataDir, "downloads");
    fs.mkdirSync(this.uploadsDir, { recursive: true });
    fs.mkdirSync(this.downloadsDir, { recursive: true });

    this.identity = null;
    this.store = null;
    this.direct = null;
    this.groups = null;
    this.listener = null;
    this.nickname = "";
    this.downloads = new Map();
    this.bindError = null;
    this.stopped = false;
    this._snapshotTimer = null;
    this._bindTimer = null;
  }

  async start() {
    if (this.identity) return;
    this.identity = new DeviceIdentity(this.dataDir);
    this.store = new Store(this.dataDir);
    this.nickname = (this.store.load("settings.json", {}).nickname || this.username).trim();
    this.direct = new DirectChatManager(this.identity);
    this.groups = new GroupApp(this.identity, this.tcpPort);
    this.groups._myName = this.nickname;
    this._wireDirect();
    this._wireGroups();
    this._startListener();
    this._restoreState();
    this._pruneUploads();
  }

  // uploads are a staging area for files already handed to the per-file
  // servers; anything older than a day is dead weight
  _pruneUploads() {
    const cutoff = Date.now() - UPLOAD_TTL_MS;
    let names = [];
    try {
      names = fs.readdirSync(this.uploadsDir);
    } catch (e) {
      return;
    }
    for (const name of names) {
      const p = path.join(this.uploadsDir, name);
      try {
        if (fs.statSync(p).mtimeMs < cutoff) fs.unlinkSync(p);
      } catch (e) {
        /* ignore */
      }
    }
  }

  _startListener() {
    this.listener = new SharedListener(this.tcpPort, this.identity, this.groups, this.direct);
    this.listener
      .start()
      .then(() => {
        this.bindError = null;
        this._scheduleSnapshot();
      })
      .catch((err) => {
        this.bindError = `\u65e0\u6cd5\u76d1\u542c\u7aef\u53e3 ${this.tcpPort}\uff1a${err.message || err}`;
        this._scheduleSnapshot();
        if (this.stopped) return;
        this._bindTimer = setTimeout(() => {
          this._bindTimer = null;
          if (!this.stopped && this.listener) {
            this.listener.stop();
            this._startListener();
          }
        }, BIND_RETRY_MS);
      });
  }

  stop() {
    this.stopped = true;
    if (this._snapshotTimer) clearTimeout(this._snapshotTimer);
    if (this._bindTimer) clearTimeout(this._bindTimer);
    if (this.direct) this.direct.shutdown();
    if (this.groups) this.groups.shutdown();
    if (this.listener) this.listener.stop();
  }

  broadcast(obj) {
    this.server.web.broadcast(obj, this.id);
  }

  _scheduleSnapshot() {
    if (this._snapshotTimer) return;
    this._snapshotTimer = setTimeout(() => {
      this._snapshotTimer = null;
      this.broadcast({ snapshot: this.buildSnapshot() });
    }, 120);
  }

  _wireDirect() {
    const d = this.direct;
    d.events.contactsChanged = () => {
      this._saveContacts();
      this._scheduleSnapshot();
    };
    d.events.messagesChanged = (peerId) => {
      this._saveDirectChat(peerId);
      this._scheduleSnapshot();
    };
    d.events.requestsChanged = () => {
      this.store.save("requests.json", d.contactRequests.map((r) => r.toDict()));
      this._scheduleSnapshot();
    };
    d.events.marksChanged = () => {
      this.store.save("marks.json", d.removedMarks());
    };
    d.events.event = (text) => this.broadcast({ event: text });
    d.events.typingChanged = (peerId, active) => {
      this.broadcast({ typing: { chatKey: "direct:" + peerId, senderId: peerId, active } });
    };
    d.events.sessionEstablished = () => this._scheduleSnapshot();
    d.events.sessionClosed = () => this._scheduleSnapshot();
    d.events.connectFailed = (peer, reason) => {
      this.broadcast({ event: `\u8fde\u63a5 ${peer.name} \u5931\u8d25\uff1a${reason}` });
    };
    d.events.chatMigrated = (fromId, toId) => {
      try {
        this.store.saveChat("direct:" + toId, this._chatForSave("direct:" + toId));
        this.store.deleteChat("direct:" + fromId);
      } catch (e) {
        /* ignore */
      }
      this._scheduleSnapshot();
    };
  }

  _wireGroups() {
    const g = this.groups;
    g.events.messagesChanged = (state) => {
      this._saveGroupChat(state);
      this._scheduleSnapshot();
    };
    g.events.peersChanged = () => this._scheduleSnapshot();
    g.events.groupInfoChanged = (state) => {
      this._saveGroups();
      this._scheduleSnapshot();
    };
    g.events.connectionLost = (state) => {
      this.broadcast({ event: `\u4e0e\u7fa4\u7ec4\u300c${state.name}\u300d\u7684\u4e3b\u673a\u8fde\u63a5\u5df2\u65ad\u5f00` });
      this._scheduleSnapshot();
    };
    g.events.typingChanged = (state, senderId, active) => {
      this.broadcast({ typing: { chatKey: state.groupId, senderId, active } });
    };
    g.events.readReceipt = (state, readerId, upToId) => {
      const target = state.messages.find((m) => m.id === upToId);
      if (!target) return;
      let changed = false;
      for (const m of state.messages) {
        if (m.senderId === this.identity.deviceId && m.timestamp <= target.timestamp) {
          m.readers = m.readers || [];
          if (!m.readers.includes(readerId)) {
            m.readers.push(readerId);
            changed = true;
          }
        }
      }
      if (changed) {
        this._saveGroupChat(state);
        this._scheduleSnapshot();
      }
    };
    g.events.kicked = (state) => {
      this.groups.stopGroup(state.groupId);
      this._saveGroups();
      this.broadcast({ event: `\u4f60\u5df2\u88ab\u7fa4\u4e3b\u79fb\u51fa\u7fa4\u7ec4\u300c${state.name}\u300d` });
      this._scheduleSnapshot();
    };
    g.events.event = (text) => this.broadcast({ event: text });
    g.events.deletedIdsReceived = (state, ids) => {
      for (const id of ids) this.groups.recordTombstone(state, id);
      this._saveGroups();
    };
    g.events.messageEdited = () => this._scheduleSnapshot();
    g.events.reactionChanged = (state, messageId, emoji, senderId, active) => {
      this._applyReaction(state.messages, messageId, emoji, senderId, active);
      this._saveGroupChat(state);
      this._scheduleSnapshot();
    };
    g.events.pinChanged = (state, messageId, senderId, active) => {
      const target = state.messages.find((m) => m.id === messageId);
      if (target) {
        target.pinned = active;
        target.pinnedBy = active ? senderId : null;
      }
      this._saveGroupChat(state);
      this._scheduleSnapshot();
    };
  }

  _applyReaction(messages, messageId, emoji, senderId, active) {
    const target = messages.find((m) => m.id === messageId);
    if (!target) return;
    target.reactions = target.reactions || {};
    const list = target.reactions[emoji] || [];
    const idx = list.indexOf(senderId);
    if (active && idx < 0) list.push(senderId);
    if (!active && idx >= 0) list.splice(idx, 1);
    if (list.length) target.reactions[emoji] = list;
    else delete target.reactions[emoji];
  }

  _restoreState() {
    const contacts = this.store.load("contacts.json", []);
    const marks = this.store.load("marks.json", { ids: {}, endpoints: {} });
    this.direct.restoreRemovedMarks(marks.ids, marks.endpoints);
    this.direct.restoreContactRequests(this.store.load("requests.json", []));
    const savedGroups = this.store.load("groups.json", []);
    for (const sg of savedGroups) {
      if (sg.isHost) {
        const g = this.groups.createHost(sg.name, this.store.unprotect(sg.password || ""), sg.joinId);
        g.announcement = sg.announcement || "";
        g.creatorId = sg.creatorId || this.identity.deviceId;
        g.tombstones = sg.tombstones || [];
        // the host's copy is also the mesh history source: reload it or the
        // whole group chat vanishes from the UI on every restart
        const history = this._restoreGroupMessages(g.groupId);
        if (history.length) {
          g.messages = history.sort((a, b) => a.timestamp - b.timestamp);
        }
      } else {
        const g = this.groups.registerMemberGroup({
          groupId: sg.groupId,
          name: sg.name,
          joinId: sg.joinId,
          password: this.store.unprotect(sg.password || ""),
          announcement: sg.announcement || "",
          creatorId: sg.creatorId || "",
          hostPeer: sg.hostPeer ? M.Peer.fromDict(sg.hostPeer) : null,
        });
        g.tombstones = sg.tombstones || [];
        const savedPeers = (sg.savedPeers || []).map((p) => M.Peer.fromDict(p));
        const history = this._restoreGroupMessages(g.groupId);
        this.groups.enterMesh(g, history, savedPeers);
      }
    }
    this.direct.configure(
      this.identity.deviceId,
      this.nickname,
      U.getLocalIpAddress(),
      this.tcpPort,
      contacts.map((c) => M.Peer.fromDict(c))
    );
    for (const contact of contacts) {
      const msgs = this._restoreDirectMessages(contact.id);
      if (msgs.length) this.direct.seedMessages(contact.id, msgs);
      const pending = msgs.filter((m) => m.pending);
      if (pending.length) this.direct.restorePending(contact.id, pending);
    }
    this._saveGroups();
  }

  _chatForSave(key) {
    if (key.startsWith("direct:")) {
      return this.direct.messagesFor(key.slice(7)).map(stripLocalCruft);
    }
    const g = this.groups.get(key);
    return g ? g.messages.map(stripLocalCruft) : [];
  }

  _saveDirectChat(peerId) {
    this.store.saveChat("direct:" + peerId, this._chatForSave("direct:" + peerId));
  }

  _saveGroupChat(state) {
    this.store.saveChat(state.groupId, this._chatForSave(state.groupId));
  }

  _restoreDirectMessages(peerId) {
    return this.store.loadChatDecrypted("direct:" + peerId).map((m) => new M.ChatMessage(m));
  }

  _restoreGroupMessages(groupId) {
    return this.store.loadChatDecrypted(groupId).map((m) => new M.ChatMessage(m));
  }

  _saveContacts() {
    this.store.save("contacts.json", this.direct.contactsList().map((c) => c.toDict()));
  }

  _saveGroups() {
    const out = [];
    for (const g of this.groups.groups.values()) {
      out.push({
        groupId: g.groupId,
        name: g.name,
        isHost: g.isHost,
        joinId: g.joinId,
        password: this.store.protect(g.password || ""),
        announcement: g.announcement || "",
        creatorId: g.creatorId || "",
        hostPeer: g.hostPeer ? g.hostPeer.toDict() : null,
        savedPeers: Array.from(g.meshPeers.values()).map((p) => p.toDict()),
        tombstones: g.tombstones.slice(-200),
      });
    }
    this.store.save("groups.json", out);
  }

  buildSnapshot() {
    const downloaded = this._downloadedFileNames();
    const contacts = this.direct.contactsList().map((c) => ({
      id: c.id,
      name: c.name,
      ipAddress: c.ipAddress,
      port: c.port,
      alive: this.direct.isChatAlive(c.id),
    }));
    const groups = [];
    for (const g of this.groups.groups.values()) {
      const members = [];
      if (g.isHost) {
        members.push(peerView(this.groups.myPeerFor(g), this.identity, g.groupId));
      }
      for (const p of g.peers.values()) {
        members.push(peerView(p, this.identity, g.groupId));
      }
      groups.push({
        groupId: g.groupId,
        name: g.name,
        isHost: g.isHost,
        joinId: g.joinId,
        announcement: g.announcement || "",
        creatorId: g.creatorId || "",
        members,
        alive: g.isHost ? !this.bindError : Boolean((g.hostConn && g.hostConn.alive) || g.meshLinks.size > 0),
        relayAlive: Boolean(g.hostConn && g.hostConn.alive),
        meshLinks: g.meshLinks.size,
        memberCount: members.length,
      });
    }
    const chats = {};
    for (const c of contacts) {
      chats["direct:" + c.id] = this._chatView(
        "direct:" + c.id,
        this.direct.messagesFor(c.id),
        downloaded
      );
    }
    for (const g of this.groups.groups.values()) {
      chats[g.groupId] = this._chatView(g.groupId, g.messages, downloaded);
    }
    return {
      profile: {
        username: this.username,
        name: this.nickname,
        deviceId: this.identity.deviceId,
        fingerprint: this.identity.fingerprint(),
        ip: U.getLocalIpAddress(),
        port: this.tcpPort,
        bindError: this.bindError,
      },
      contacts,
      groups,
      requests: this.direct.contactRequests.map((r) => r.toDict()),
      chats,
    };
  }

  _chatView(key, messages, downloaded) {
    return {
      key,
      messages: messages.slice(-300).map((m) => ({
        id: m.id,
        content: m.content,
        timestamp: m.timestamp,
        senderId: m.senderId,
        senderName: m.senderName,
        isFromMe: m.senderId === this.identity.deviceId,
        pending: Boolean(m.pending),
        read: Boolean(m.read),
        edited: Boolean(m.edited),
        fileInfo: m.fileInfo
          ? {
              fileId: m.fileInfo.fileId,
              fileName: m.fileInfo.fileName,
              fileSize: m.fileInfo.fileSize,
              kind: m.fileInfo.kind || "file",
              state: this._fileState(m.fileInfo.fileId, m, downloaded),
            }
          : null,
        reactions: m.reactions || {},
        pinned: Boolean(m.pinned),
        readers: m.readers || [],
      })),
    };
  }

  // one directory listing per snapshot instead of an fs stat per message
  _downloadedFileNames() {
    try {
      return new Set(fs.readdirSync(this.downloadsDir));
    } catch (e) {
      return new Set();
    }
  }

  _fileState(fileId, msg, downloaded) {
    if (msg.senderId === this.identity.deviceId) return "sent";
    const dl = this.downloads.get(fileId);
    if (dl && dl.status === "done") return "done";
    const saved = path.basename(this._downloadPath(fileId, msg.fileInfo.fileName));
    if (downloaded && downloaded.has(saved)) return "done";
    if (dl && dl.status === "downloading") {
      return { downloading: true, received: dl.received, total: dl.total };
    }
    return "remote";
  }

  _downloadPath(fileId, fileName) {
    // fileId arrives from the network: never let it steer the save path
    // outside this account's downloads directory
    return path.join(this.downloadsDir, `${U.sanitizeFileId(fileId)}_${U.sanitizeFileName(fileName)}`);
  }

  _uploadPath(uploadId) {
    const safe = U.sanitizeFileName(String(uploadId));
    return path.join(this.uploadsDir, safe);
  }

  async handleAction(conn, action, msg) {
    const d = this.direct;
    const gapp = this.groups;
    switch (action) {
      case "getSnapshot":
        conn.send(JSON.stringify({ snapshot: this.buildSnapshot() }));
        return;
      case "setNickname": {
        const name = String(msg.name || "").trim().slice(0, 64);
        if (!name) return;
        this.nickname = name;
        gapp.setProfile(name);
        d.myName = name;
        this.store.save("settings.json", { nickname: this.nickname });
        this._saveContacts();
        this._scheduleSnapshot();
        return;
      }
      case "addContact": {
        const [ip, port] = U.parseHostPort(String(msg.ip || ""));
        if (!ip) return;
        const peer = new M.Peer(`ip:${ip}:${port}`, `${ip}`, ip, port);
        d.addContact(peer);
        d.startChat(peer, true);
        return;
      }
      case "removeContact":
        d.removeContact(String(msg.peerId || ""));
        return;
      case "acceptRequest":
        d.acceptContactRequest(String(msg.id || ""));
        return;
      case "ignoreRequest":
        d.ignoreContactRequest(String(msg.id || ""));
        return;
      case "queryGroup": {
        const reqId = msg.reqId;
        const [host, port] = U.parseHostPort(String(msg.host || ""));
        const result = await gapp.queryGroup(host, port, String(msg.joinId || "").trim(), String(msg.password || ""));
        conn.send(JSON.stringify({ queryResult: { reqId, ...result } }));
        return;
      }
      case "createGroup": {
        const reqId = msg.reqId;
        const name = String(msg.name || "").trim().slice(0, 64);
        if (!name) return;
        const g = gapp.createHost(name, String(msg.password || ""));
        this._saveGroups();
        conn.send(JSON.stringify({ createdGroup: { reqId, groupId: g.groupId, joinId: g.joinId, name: g.name } }));
        this._scheduleSnapshot();
        return;
      }
      case "joinGroup": {
        const reqId = msg.reqId;
        const [host, port] = U.parseHostPort(String(msg.host || ""));
        const joinId = String(msg.joinId || "").trim();
        const password = String(msg.password || "");
        const result = await gapp.joinGroup(host, port, joinId, password);
        if (result.ok && result.group) {
          const g = result.group;
          this._saveGroups();
          this.groups.enterMesh(g, this._restoreGroupMessages(g.groupId), Array.from(g.peers.values()));
          this._scheduleSnapshot();
        }
        conn.send(
          JSON.stringify({
            joinResult: {
              reqId,
              ok: result.ok,
              message: result.message || "",
              groupId: result.group ? result.group.groupId : null,
            },
          })
        );
        return;
      }
      case "rejoinGroup": {
        const g = gapp.get(String(msg.groupId || ""));
        if (!g || g.isHost) return;
        if (g.hostConn && g.hostConn.alive) return;
        const host = g.hostPeer;
        if (!host || !host.ipAddress) return;
        const result = await gapp.joinGroup(host.ipAddress, host.port, g.joinId, g.password);
        conn.send(JSON.stringify({ joinResult: { reqId: msg.reqId, ok: result.ok, message: result.message || "" } }));
        this._scheduleSnapshot();
        return;
      }
      case "leaveGroup":
        gapp.stopGroup(String(msg.groupId || ""));
        this._saveGroups();
        this._scheduleSnapshot();
        return;
      case "sendChat": {
        const key = String(msg.chatKey || "");
        const content = String(msg.content || "");
        if (key.startsWith("direct:")) {
          d.sendMessage(key.slice(7), content);
        } else {
          const g = gapp.get(key);
          if (g) gapp.sendChatMessage(g, content);
        }
        return;
      }
      case "sendTyping": {
        const key = String(msg.chatKey || "");
        const active = Boolean(msg.active);
        if (key.startsWith("direct:")) {
          d.sendTyping(key.slice(7), active);
        } else {
          const g = gapp.get(key);
          if (g) gapp.sendTyping(g, active);
        }
        return;
      }
      case "deleteMessage": {
        const key = String(msg.chatKey || "");
        const messageId = String(msg.messageId || "");
        if (key.startsWith("direct:")) {
          const peerId = key.slice(7);
          const target = d.messagesFor(peerId).find((m) => m.id === messageId);
          // direct chat delete only retracts OUR message; a missing target
          // must never fabricate a senderId and broadcast a delete claim
          if (!target || target.senderId !== this.identity.deviceId) return;
          d.deleteMessage(peerId, messageId, this.identity.deviceId);
          this._saveDirectChat(peerId);
        } else {
          const g = gapp.get(key);
          if (g) {
            gapp.sendDelete(g, messageId);
            this._saveGroupChat(g);
          }
        }
        this._scheduleSnapshot();
        return;
      }
      case "editMessage": {
        const key = String(msg.chatKey || "");
        if (key.startsWith("direct:")) {
          d.editMessage(key.slice(7), String(msg.messageId || ""), String(msg.content || ""));
        } else {
          const g = gapp.get(key);
          if (g) gapp.sendEdit(g, String(msg.messageId || ""), String(msg.content || ""));
        }
        return;
      }
      case "react": {
        const key = String(msg.chatKey || "");
        const active = Boolean(msg.active);
        const emoji = String(msg.emoji || "");
        const messageId = String(msg.messageId || "");
        if (key.startsWith("direct:")) {
          const peerId = key.slice(7);
          d.sendDirectReaction(peerId, messageId, emoji, active);
          this._applyReaction(d.messagesFor(peerId), messageId, U.sanitizeEmoji(emoji), this.identity.deviceId, active);
          this._saveDirectChat(peerId);
        } else {
          const g = gapp.get(key);
          if (g) {
            gapp.sendReaction(g, messageId, emoji, active);
            this._applyReaction(g.messages, messageId, U.sanitizeEmoji(emoji), this.identity.deviceId, active);
            this._saveGroupChat(g);
          }
        }
        this._scheduleSnapshot();
        return;
      }
      case "pin": {
        const key = String(msg.chatKey || "");
        const active = Boolean(msg.active);
        const messageId = String(msg.messageId || "");
        if (key.startsWith("direct:")) {
          const peerId = key.slice(7);
          d.sendDirectPin(peerId, messageId, active);
          const target = d.messagesFor(peerId).find((m) => m.id === messageId);
          if (target) {
            target.pinned = active;
            target.pinnedBy = active ? this.identity.deviceId : null;
          }
          this._saveDirectChat(peerId);
        } else {
          const g = gapp.get(key);
          if (g) {
            gapp.sendPin(g, messageId, active);
            const target = g.messages.find((m) => m.id === messageId);
            if (target) {
              target.pinned = active;
              target.pinnedBy = active ? this.identity.deviceId : null;
            }
            this._saveGroupChat(g);
          }
        }
        this._scheduleSnapshot();
        return;
      }
      case "openChat": {
        const key = String(msg.chatKey || "");
        if (!key.startsWith("direct:")) {
          const g = gapp.get(key);
          if (g && g.messages.length) {
            gapp.sendGroupReadReceipt(g, g.messages[g.messages.length - 1].id);
          }
        }
        return;
      }
      case "sendFile": {
        const key = String(msg.chatKey || "");
        const uploadPath = this._uploadPath(String(msg.uploadId || ""));
        if (!fs.existsSync(uploadPath)) return;
        if (key.startsWith("direct:")) {
          d.sendFile(key.slice(7), uploadPath);
        } else {
          const g = gapp.get(key);
          if (g) gapp.sendFileMessage(g, uploadPath);
        }
        return;
      }
      case "downloadFile": {
        const key = String(msg.chatKey || "");
        const messageId = String(msg.messageId || "");
        let fileInfo = null;
        if (key.startsWith("direct:")) {
          const target = d.messagesFor(key.slice(7)).find((m) => m.id === messageId);
          fileInfo = target ? target.fileInfo : null;
        } else {
          const g = gapp.get(key);
          const target = g ? g.messages.find((m) => m.id === messageId) : null;
          fileInfo = target ? target.fileInfo : null;
        }
        if (!fileInfo) return;
        await this.downloadToFile(key, messageId, fileInfo);
        return;
      }
      case "kickMember": {
        const g = gapp.get(String(msg.groupId || ""));
        if (g) {
          gapp.kickMember(g, String(msg.targetId || ""));
          this._saveGroups();
          this._scheduleSnapshot();
        }
        return;
      }
      case "groupUpdate": {
        const g = gapp.get(String(msg.groupId || ""));
        if (g) {
          gapp.sendGroupUpdate(
            g,
            msg.name != null ? String(msg.name) : "",
            msg.announcement != null ? String(msg.announcement) : null
          );
          this._saveGroups();
          this._scheduleSnapshot();
        }
        return;
      }
      default:
        return;
    }
  }

  async downloadToFile(chatKey, messageId, fileInfo) {
    const existing = this.downloads.get(fileInfo.fileId);
    if (existing && existing.status === "downloading") return;
    const targetPath = this._downloadPath(fileInfo.fileId, fileInfo.fileName);
    if (fs.existsSync(targetPath)) {
      this.downloads.set(fileInfo.fileId, { status: "done", path: targetPath });
      this.broadcast({ fileDone: { chatKey, messageId, fileId: fileInfo.fileId, ok: true } });
      this._scheduleSnapshot();
      return;
    }
    let offset = 0;
    try {
      offset = fs.statSync(targetPath + ".part").size;
    } catch (e) {
      offset = 0;
    }
    const entry = { status: "downloading", received: offset, total: fileInfo.fileSize };
    this.downloads.set(fileInfo.fileId, entry);
    this._scheduleSnapshot();
    const result = await downloadFileOffer(fileInfo, targetPath, {
      // resume: start at the bytes already staged in the ".part" file so an
      // interrupted download continues instead of restarting from zero
      offset,
      onProgress: (received, total) => {
        entry.received = received;
        entry.total = total;
        this.broadcast({
          fileProgress: { chatKey, messageId, fileId: fileInfo.fileId, received, total },
        });
      },
    });
    if (result.ok) {
      entry.status = "done";
      entry.path = targetPath;
      this.broadcast({ fileDone: { chatKey, messageId, fileId: fileInfo.fileId, ok: true } });
    } else {
      entry.status = "failed";
      this.broadcast({
        fileDone: { chatKey, messageId, fileId: fileInfo.fileId, ok: false, message: result.message },
      });
    }
    this._scheduleSnapshot();
  }

  serveFile(res, fileId) {
    const safeId = U.sanitizeFileId(fileId);
    const prefix = safeId + "_";
    // the in-memory download map is empty after a restart, so look at what is
    // actually on disk (the snapshot marks such files "done" too); partial
    // ".part" files must never be served
    let match = null;
    try {
      for (const name of fs.readdirSync(this.downloadsDir)) {
        if (name.startsWith(prefix) && !name.endsWith(".part")) {
          match = path.join(this.downloadsDir, name);
          break;
        }
      }
    } catch (e) {
      match = null;
    }
    if (!match) {
      res.writeHead(404);
      res.end("not downloaded yet");
      return;
    }
    const displayName = path.basename(match).slice(safeId.length + 1);
    const headers = {
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(displayName)}`,
    };
    try {
      headers["Content-Length"] = String(fs.statSync(match).size);
    } catch (e) {
      /* ignore */
    }
    res.writeHead(200, headers);
    const stream = fs.createReadStream(match);
    stream.on("error", () => {
      try {
        res.destroy();
      } catch (e) {
        /* ignore */
      }
    });
    stream.pipe(res);
  }

  handleUpload(params, req, res) {
    const name = U.sanitizeFileName(params.get("name") || "file");
    const uploadId = `${U.uuid()}_${name}`;
    const target = this._uploadPath(uploadId);
    const out = fs.createWriteStream(target);
    let size = 0;
    let responded = false;
    const respond = (code, body) => {
      if (responded) return;
      responded = true;
      try {
        res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify(body));
      } catch (e) {
        /* ignore */
      }
    };
    const discardPartial = () => {
      try {
        out.destroy();
      } catch (e) {
        /* ignore */
      }
      try {
        fs.unlinkSync(target);
      } catch (e) {
        /* ignore */
      }
    };
    let rejected = false;
    req.on("data", (chunk) => {
      if (rejected) return;
      size += chunk.length;
      if (size > MAX_UPLOAD_BYTES) {
        rejected = true;
        respond(413, { error: "file too large" });
        req.unpipe(out);
        discardPartial();
        // keep consuming and discarding the rest: resetting the socket here
        // would destroy the 413 before the client can read it
        req.resume();
      }
    });
    req.pipe(out);
    out.on("finish", () => respond(200, { uploadId, size }));
    out.on("error", () => respond(500, { error: "upload failed" }));
  }
}

function stripLocalCruft(m) {
  const out = {
    id: m.id,
    content: m.content,
    timestamp: m.timestamp,
    senderId: m.senderId,
    senderName: m.senderName,
  };
  if (m.fileInfo) out.fileInfo = { ...m.fileInfo };
  if (m.replyTo != null) out.replyTo = m.replyTo;
  if (m.replyPreview != null) out.replyPreview = m.replyPreview;
  if (m.replySender != null) out.replySender = m.replySender;
  if (m.forwarded) out.forwarded = m.forwarded;
  if (m.mentions) out.mentions = m.mentions;
  if (m.edited) out.edited = true;
  if (m.senderPubId) out.senderPubId = m.senderPubId;
  if (m.senderSig) out.senderSig = m.senderSig;
  if (m.isFromMe) out.isFromMe = true;
  if (m.pending) out.pending = true;
  if (m.read) out.read = true;
  if (m.reactions && Object.keys(m.reactions).length) out.reactions = m.reactions;
  if (m.pinned) out.pinned = true;
  if (m.readers && m.readers.length) out.readers = m.readers;
  return out;
}

function peerView(p, identity, groupId) {
  const verified = GA.memberVerified(identity, groupId, p.id);
  return {
    id: p.id,
    name: p.name,
    ipAddress: p.ipAddress,
    port: p.port,
    isSelf: p.id === identity.deviceId,
    verified,
    fingerprint: verified ? GA.memberFingerprint(identity, groupId, p.id) : "",
  };
}

module.exports = { Account, stripLocalCruft, peerView };
