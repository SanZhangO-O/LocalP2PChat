"use strict";

const fs = require("fs");
const net = require("net");

const M = require("./models");
const C = require("./crypto");
const U = require("./util");
const W = require("./wire");
const GA = require("./identity");
const { OutgoingFileServer, downloadFileOffer } = require("./files");

const HEARTBEAT_INTERVAL = 15000;
const HEARTBEAT_TIMEOUT = 45000;
const RETRY_INTERVAL = 10000;
const HISTORY_CAP = 500;
const MAX_PEERS_PER_GROUP = 64;
const MAX_DIAL_ATTEMPTS = 30;
const HISTORY_CHUNK_BYTES = 36 * 1024;
const MESH_SEND_QUEUE_CAP = 4096;
const TOMBSTONE_CAP = 200;
const JOIN_ACK_GROUP_FILES_CAP = 120;
const GROUP_FILE_PUSH_BUDGET_BYTES = 32 * 1024;

function mkState(fields) {
  return Object.assign(
    {
      groupId: "",
      name: "",
      joinId: "",
      password: "",
      isHost: false,
      announcement: "",
      creatorId: "",
      hostPeer: null,
      hostConn: null,
      relay: null,
      peers: new Map(),
      clients: new Map(),
      messages: [],
      meshPeers: new Map(),
      meshLinks: new Map(),
      meshDialing: new Set(),
      tombstones: [],
      stopped: false,
      sendQueue: [],
      pumping: false,
      timers: [],
      lastReceiptSent: "",
      hostEndpointTyped: null,
    },
    fields
  );
}

class GroupApp {
  constructor(identity, port) {
    this.identity = identity;
    this.port = port;
    this.groups = new Map();
    this.events = {
      messagesChanged: null,
      peersChanged: null,
      groupInfoChanged: null,
      connectionLost: null,
      typingChanged: null,
      readReceipt: null,
      kicked: null,
      event: null,
      joinResult: null,
      messageEdited: null,
      reactionChanged: null,
      pinChanged: null,
      deletedIdsReceived: null,
    };
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

  myName() {
    return this._myName || "";
  }

  setProfile(name) {
    this._myName = name;
    for (const g of this.groups.values()) {
      if (g.myPeer) g.myPeer.name = name;
    }
  }

  myPeerFor(g) {
    if (!g.myPeer) g.myPeer = new M.Peer("", "", "", 0);
    g.myPeer.id = this.identity.deviceId;
    g.myPeer.name = this.myName();
    g.myPeer.ipAddress = U.getLocalIpAddress();
    g.myPeer.port = this.port;
    return g.myPeer;
  }

  createHost(groupName, password, joinId) {
    const groupId = `${groupName}@${this.identity.hardwareId}`;
    const numeric = joinId || U.numericGroupIdOf(groupName, this.identity.hardwareId);
    const g = mkState({
      groupId,
      name: groupName,
      joinId: numeric,
      password: password || "",
      isHost: true,
      creatorId: this.identity.deviceId,
    });
    this.groups.set(groupId, g);
    return g;
  }

  get(groupId) {
    return this.groups.get(groupId);
  }

  registerMemberGroup(fields) {
    const g = mkState({
      groupId: fields.groupId,
      name: fields.name || "",
      joinId: fields.joinId || "",
      password: fields.password || "",
      isHost: false,
      announcement: fields.announcement || "",
      creatorId: fields.creatorId || "",
      hostPeer: fields.hostPeer || null,
    });
    this.groups.set(g.groupId, g);
    return g;
  }

  resolveByNumericId(idOrName) {
    if (!idOrName) return null;
    for (const g of this.groups.values()) {
      if (g.isHost && g.joinId === idOrName) return g;
    }
    return null;
  }

  memberGroupByJoinId(idOrName) {
    if (!idOrName) return null;
    for (const g of this.groups.values()) {
      if (!g.isHost && g.joinId === idOrName) return g;
    }
    return null;
  }

  passwordFor(mode, groupId) {
    if (!groupId) return null;
    if (mode === W.MODE_MESH) {
      for (const g of this.groups.values()) {
        if (!g.isHost && g.groupId === groupId) return g.password;
      }
      return null;
    }
    const host = this.resolveByNumericId(groupId);
    if (host) return host.password;
    const member = this.memberGroupByJoinId(groupId);
    if (member) return member.password;
    return null;
  }

  stopGroup(groupId) {
    const g = this.groups.get(groupId);
    if (!g) return;
    g.stopped = true;
    for (const t of g.timers) clearInterval(t);
    for (const conn of g.clients.values()) {
      conn.alive = false;
      conn.channel.destroy();
    }
    g.clients.clear();
    if (g.hostConn) {
      g.hostConn.alive = false;
      g.hostConn.channel.destroy();
      g.hostConn = null;
    }
    for (const link of g.meshLinks.values()) {
      link.alive = false;
      link.channel.destroy();
    }
    g.meshLinks.clear();
    this.groups.delete(groupId);
  }

  shutdown() {
    for (const gid of Array.from(this.groups.keys())) this.stopGroup(gid);
  }

  matchesGroup(g, idOrName) {
    return idOrName === g.joinId || idOrName === g.name;
  }

  async handleQueryOrJoinHost(channel, wire, start, packet) {
    const g = this.resolveByNumericId(start.groupId);
    if (!g) return false;
    if (packet.type === W.MODE_QUERY) {
      // answer (group_info or join_rejected) is written by _handleQuery
      this._handleQuery(g, wire, packet, false);
      // close gracefully so the reply is flushed before the FIN (a destroy
      // right after write can discard it). Never keep the query socket open.
      channel.gracefulClose();
      return true;
    }
    if (packet.type === W.MODE_JOIN) {
      this._handleJoin(channel, wire, packet, g);
      return true;
    }
    return false;
  }

  async handleQueryOrJoinMember(channel, wire, start, packet) {
    const g = this.memberGroupByJoinId(start.groupId);
    if (!g) return false;
    const idOrName = packet.groupId;
    if (!idOrName || !this.matchesGroup(g, idOrName)) return false;
    try {
      if (packet.type === W.MODE_QUERY) {
        const info = new M.GroupInfo(
          g.name,
          this.myName(),
          this.identity.deviceId,
          g.peers.size + 1
        );
        wire.sendPacket(new M.NetworkPacket({ type: "group_info", groupInfo: info }));
      } else if (packet.type === W.MODE_JOIN) {
        const peer = packet.peer;
        if (!peer) return false;
        const members = Array.from(g.peers.values()).filter(
          (p) => !g.hostPeer || p.id !== g.hostPeer.id
        );
        wire.sendPacket(
          new M.NetworkPacket({
            type: "join_ack",
            groupId: g.groupId,
            members,
            host: g.hostPeer || undefined,
            announcement: g.announcement || null,
            deletedIds: g.tombstones.length ? g.tombstones.slice(-TOMBSTONE_CAP) : null,
          })
        );
        channel.gracefulClose();
        this.meshAnnouncePeer(g, peer);
      } else {
        return false;
      }
    } catch (e) {
      return false;
    } finally {
      channel.gracefulClose();
    }
    return true;
  }

  _handleQuery(g, wire, packet, keepOpen) {
    const matched = Boolean(packet.groupId) && this.matchesGroup(g, packet.groupId);
    try {
      if (!matched) {
        wire.sendPacket(new M.NetworkPacket({ type: "join_rejected" }));
        return false;
      }
      const info = new M.GroupInfo(
        g.name,
        this.myName(),
        this.identity.deviceId,
        g.peers.size + 1
      );
      wire.sendPacket(new M.NetworkPacket({ type: "group_info", groupInfo: info }));
      return Boolean(keepOpen);
    } catch (e) {
      return false;
    }
  }

  _handleJoin(channel, wire, packet, g) {
    const peer = packet.peer;
    if (
      !packet.groupId ||
      !this.matchesGroup(g, packet.groupId) ||
      !peer ||
      !peer.id ||
      peer.id === this.identity.deviceId
    ) {
      try {
        wire.sendPacket(new M.NetworkPacket({ type: "join_rejected" }));
      } catch (e) {
        /* ignore */
      }
      channel.destroy();
      return;
    }
    if (!g.peers.has(peer.id) && g.peers.size >= MAX_PEERS_PER_GROUP) {
      try {
        wire.sendPacket(new M.NetworkPacket({ type: "join_rejected" }));
      } catch (e) {
        /* ignore */
      }
      channel.destroy();
      return;
    }
    g.peers.set(peer.id, peer);
    const conn = { channel, wire, alive: true };
    const previous = g.clients.get(peer.id);
    g.clients.set(peer.id, conn);
    if (previous && previous !== conn) {
      previous.channel.destroy();
    }
    const members = [this.myPeerFor(g)].concat(
      Array.from(g.peers.entries())
        .filter(([pid]) => pid !== this.identity.deviceId && pid !== peer.id)
        .map(([, p]) => p)
    );
    try {
      const ack = new M.NetworkPacket({
        type: "join_ack",
        groupId: g.groupId,
        members,
        announcement: g.announcement || null,
      });
      if (g.tombstones.length) {
        ack.deletedIds = g.tombstones.slice(-TOMBSTONE_CAP);
      }
      wire.sendPacket(ack);
    } catch (e) {
      if (g.clients.get(peer.id) === conn) {
        g.clients.delete(peer.id);
        g.peers.delete(peer.id);
      }
      channel.destroy();
      return;
    }
    this._broadcastToClients(g, new M.NetworkPacket({ type: "announce", peer }), peer.id);
    this.emit("peersChanged", g);
    this._readLoopFromClient(g, conn, peer.id);
  }

  _broadcastToClients(g, packet, exclude) {
    for (const [pid, conn] of g.clients) {
      if (pid === exclude) continue;
      try {
        conn.wire.sendPacket(packet);
      } catch (e) {
        /* ignore */
      }
    }
  }

  _readLoopFromClient(g, conn, peerId) {
    (async () => {
      while (conn.alive && !g.stopped) {
        let packet;
        try {
          packet = await conn.wire.recvPacket(HEARTBEAT_TIMEOUT);
        } catch (e) {
          break;
        }
        if (packet == null) break;
        try {
          this._processPacketFromClient(g, packet, peerId);
        } catch (e) {
          /* ignore */
        }
      }
      this._clientDisconnected(g, conn, peerId);
    })();
  }

  _clientDisconnected(g, conn, peerId) {
    conn.alive = false;
    conn.channel.destroy();
    const replaced = g.clients.get(peerId) === conn ? false : true;
    if (!replaced) {
      g.clients.delete(peerId);
      g.peers.delete(peerId);
      this._broadcastToClients(
        g,
        new M.NetworkPacket({ type: "peer_left", peer: new M.Peer(peerId, "", "", 0) }),
        peerId
      );
    }
    this.emit("peersChanged", g);
  }

  _processPacketFromClient(g, packet, senderId) {
    if (packet.type === "group_update" || packet.type === "kick_member") {
      this._dropClient(g, senderId);
      return;
    }
    if (packet.type === "chat" || packet.type === "file_message") {
      const msg = packet.message;
      if (!msg || msg.senderId !== senderId || !U.isValidContent(msg.content)) return;
      if (!GA.verifyMessage(this.identity, g.groupId, msg)) return;
      if (!g.messages.some((m) => m.id === msg.id)) {
        g.messages.push(msg.markedFromMe(this.identity.deviceId));
      }
      this.emit("messagesChanged", g);
      this._broadcastToClients(g, packet, senderId);
      return;
    }
    if (packet.type === "delete_message") {
      const target = g.messages.find((m) => m.id === packet.messageId);
      if (
        target &&
        packet.senderId === senderId &&
        target.senderId === senderId
      ) {
        if (!GA.verifyDelete(this.identity, g.groupId, senderId, packet.messageId, packet.senderPubId, packet.senderSig)) {
          return;
        }
        g.messages = g.messages.filter((m) => m.id !== packet.messageId);
        this.recordTombstone(g, packet.messageId);
        this.emit("messagesChanged", g);
        this._broadcastToClients(g, packet, senderId);
      }
      return;
    }
    if (packet.type === "typing" && packet.active != null) {
      if (packet.senderId !== senderId) return;
      this.emit("typingChanged", g, senderId, Boolean(packet.active));
      this._broadcastToClients(g, packet, senderId);
      return;
    }
    if (packet.type === "edit_message" && packet.messageId && packet.newContent != null) {
      const target = g.messages.find((m) => m.id === packet.messageId);
      if (
        target &&
        packet.senderId === senderId &&
        target.senderId === senderId &&
        GA.verifyMessageFields(
          this.identity,
          g.groupId,
          senderId,
          packet.messageId,
          target.timestamp,
          packet.newContent,
          packet.senderPubId,
          packet.senderSig
        )
      ) {
        target.content = packet.newContent;
        target.edited = true;
        this.emit("messagesChanged", g);
        this.emit("messageEdited", g, packet.messageId, packet.newContent, senderId);
        this._broadcastToClients(g, packet, senderId);
      }
      return;
    }
    if (packet.type === "reaction" && packet.messageId && packet.emoji) {
      if (packet.senderId === senderId && g.messages.some((m) => m.id === packet.messageId)) {
        this.emit("reactionChanged", g, packet.messageId, packet.emoji, senderId, Boolean(packet.active));
        this._broadcastToClients(g, packet, senderId);
      }
      return;
    }
    if (packet.type === "pin_message" && packet.messageId) {
      if (packet.senderId === senderId && g.messages.some((m) => m.id === packet.messageId)) {
        this.emit("pinChanged", g, packet.messageId, senderId, Boolean(packet.active));
        this._broadcastToClients(g, packet, senderId);
      }
      return;
    }
    if (
      packet.type === "read_receipt" &&
      packet.upToId &&
      packet.readerId === senderId &&
      packet.groupId === g.groupId
    ) {
      this.emit("readReceipt", g, senderId, packet.upToId);
      this._broadcastToClients(g, packet, senderId);
      return;
    }
    if (packet.type === "ping") {
      const conn = g.clients.get(senderId);
      if (conn) {
        try {
          conn.wire.sendPacket(new M.NetworkPacket({ type: "pong" }));
        } catch (e) {
          /* ignore */
        }
      }
    }
  }

  _dropClient(g, peerId) {
    const conn = g.clients.get(peerId);
    g.clients.delete(peerId);
    g.peers.delete(peerId);
    if (conn) conn.channel.destroy();
    this._broadcastToClients(
      g,
      new M.NetworkPacket({ type: "peer_left", peer: new M.Peer(peerId, "", "", 0) }),
      peerId
    );
    this.emit("peersChanged", g);
  }

  async queryGroup(targetIp, targetPort, idOrName, password) {
    let socket = null;
    try {
      socket = await connectSocket(targetIp, targetPort, 5000);
      const channel = new W.LineChannel(socket);
      const wire = new W.Wire(channel);
      await W.Handshake.initiate(wire, W.MODE_QUERY, idOrName, password || "");
      wire.sendPacket(new M.NetworkPacket({ type: W.MODE_QUERY, groupId: idOrName }));
      const response = await wire.recvPacket(15000);
      if (response == null) return { ok: false, message: "\u65e0\u54cd\u5e94" };
      if (response.type === "group_info" && response.groupInfo) {
        return { ok: true, info: response.groupInfo };
      }
      if (response.type === "join_rejected") {
        return { ok: false, message: "\u8be5\u8bbe\u5907\u4e0d\u5b58\u5728\u6b64\u7fa4\u7ec4" };
      }
      return { ok: false, message: "\u672a\u77e5\u7684\u54cd\u5e94" };
    } catch (e) {
      return { ok: false, message: e instanceof W.WireException ? e.message : `\u67e5\u8be2\u5931\u8d25: ${e.message || e}` };
    } finally {
      if (socket) socket.destroy();
    }
  }

  async joinGroup(targetIp, targetPort, idOrName, password) {
    const queried = await this.queryGroup(targetIp, targetPort, idOrName, password);
    const displayName = queried.ok ? queried.info.groupName : idOrName;
    let socket = null;
    let consumed = false;
    try {
      socket = await connectSocket(targetIp, targetPort, 5000);
      const result = await this._joinExchange(
        socket,
        idOrName,
        password,
        [targetIp, targetPort],
        displayName
      );
      consumed = result.consumed;
      return result;
    } catch (e) {
      return { ok: false, message: e instanceof W.WireException ? e.message : `\u8fde\u63a5\u5931\u8d25: ${e.message || e}` };
    } finally {
      if (socket && !consumed) socket.destroy();
    }
  }

  async _joinExchange(socket, idOrName, password, typedEndpoint, displayName) {
    const channel = new W.LineChannel(socket);
    const wire = new W.Wire(channel);
    await W.Handshake.initiate(wire, W.MODE_JOIN, idOrName, password || "");
    const myPeer = new M.Peer(
      this.identity.deviceId,
      this.myName(),
      U.getLocalIpAddress(),
      this.port
    );
    wire.sendPacket(
      new M.NetworkPacket({ type: W.MODE_JOIN, groupId: idOrName, peer: myPeer })
    );
    const response = await wire.recvPacket(15000);
    if (response == null) {
      channel.destroy();
      return { ok: false, message: "\u8fde\u63a5\u88ab\u5173\u95ed" };
    }
    if (response.type === "join_ack" && response.members) {
      let g = this.groups.get(response.groupId || idOrName);
      if (!g) {
        g = mkState({
          groupId: response.groupId || `${idOrName}@member`,
          name: displayName || idOrName,
          joinId: idOrName,
          password: password || "",
          isHost: false,
        });
        this.groups.set(g.groupId, g);
      } else if (displayName && displayName !== g.name) {
        g.name = displayName;
      }
      g.password = password || g.password;
      for (const peer of response.members) {
        if (peer.id !== this.identity.deviceId) g.peers.set(peer.id, peer);
      }
      if (response.announcement != null) g.announcement = response.announcement;
      if (response.deletedIds && response.deletedIds.length) {
        this._applyDeletedIds(g, U.sanitizeDeletedIds(response.deletedIds));
      }
      const host = response.host;
      if (
        host != null &&
        (typedEndpoint == null ||
          host.ipAddress !== typedEndpoint[0] ||
          host.port !== typedEndpoint[1])
      ) {
        g.hostPeer = host;
        channel.destroy();
        const reconnect = await this._connectToHost(g, host, idOrName, password);
        return reconnect;
      }
      g.hostPeer = new M.Peer("", "", typedEndpoint[0], typedEndpoint[1]);
      g.hostConn = { channel, wire, alive: true };
      this._startHeartbeat(g);
      this._readLoopFromHost(g);
      this.emit("joinResult", g, true, "");
      this.emit("peersChanged", g);
      return { ok: true, group: g, consumed: true };
    }
    if (response.type === "join_rejected") {
      channel.destroy();
      return { ok: false, message: "\u7fa4\u7ec4\u4e0d\u5339\u914d\uff0c\u8fde\u63a5\u88ab\u62d2\u7edd" };
    }
    if (response.type === "error") {
      channel.destroy();
      return { ok: false, message: response.errorMessage || "\u52a0\u5165\u88ab\u62d2\u7edd" };
    }
    channel.destroy();
    return { ok: false, message: "\u672a\u77e5\u7684\u54cd\u5e94" };
  }

  async _connectToHost(g, host, idOrName, password) {
    let socket = null;
    try {
      socket = await connectSocket(host.ipAddress, host.port, 5000);
      const channel = new W.LineChannel(socket);
      const wire = new W.Wire(channel);
      await W.Handshake.initiate(wire, W.MODE_JOIN, idOrName, password || "");
      const myPeer = new M.Peer(
        this.identity.deviceId,
        this.myName(),
        U.getLocalIpAddress(),
        this.port
      );
      wire.sendPacket(
        new M.NetworkPacket({ type: W.MODE_JOIN, groupId: idOrName, peer: myPeer })
      );
      const response = await wire.recvPacket(15000);
      if (response == null || response.type !== "join_ack") {
        throw new Error("host rejected join");
      }
      for (const peer of response.members || []) {
        if (peer.id !== this.identity.deviceId) g.peers.set(peer.id, peer);
      }
      if (response.announcement != null) g.announcement = response.announcement;
      if (response.deletedIds && response.deletedIds.length) {
        this._applyDeletedIds(g, U.sanitizeDeletedIds(response.deletedIds));
      }
      g.hostConn = { channel, wire, alive: true };
      this._startHeartbeat(g);
      this._readLoopFromHost(g);
      this.emit("joinResult", g, true, "");
      this.emit("peersChanged", g);
      return { ok: true, group: g, consumed: true };
    } catch (e) {
      if (socket) socket.destroy();
      const message = `\u4e3b\u673a\u4e2d\u7ee7\u8fde\u63a5\u5931\u8d25\uff08mesh \u4ecd\u53ef\u7528\uff09: ${e.message || e}`;
      this.emit("event", message);
      return { ok: true, group: g, consumed: true, relayFailed: true };
    }
  }

  _startHeartbeat(g) {
    const timer = setInterval(() => {
      if (g.stopped) {
        clearInterval(timer);
        return;
      }
      try {
        if (g.isHost) {
          this._broadcastToClients(g, new M.NetworkPacket({ type: "ping" }));
        } else if (g.hostConn && g.hostConn.alive) {
          g.hostConn.wire.sendPacket(new M.NetworkPacket({ type: "ping" }));
        }
      } catch (e) {
        /* ignore */
      }
    }, HEARTBEAT_INTERVAL);
    g.timers.push(timer);
  }

  _readLoopFromHost(g) {
    const conn = g.hostConn;
    (async () => {
      while (conn.alive && !g.stopped) {
        let packet;
        try {
          packet = await conn.wire.recvPacket(HEARTBEAT_TIMEOUT);
        } catch (e) {
          break;
        }
        if (packet == null) break;
        try {
          this._processPacketAsClient(g, packet);
        } catch (e) {
          /* ignore */
        }
      }
      this._hostDisconnected(g, conn);
    })();
  }

  _hostDisconnected(g, conn) {
    conn.alive = false;
    conn.channel.destroy();
    if (g.hostConn === conn) {
      g.hostConn = null;
      g.peers.clear();
      this.emit("connectionLost", g);
      this.emit("peersChanged", g);
    }
  }

  _processPacketAsClient(g, packet) {
    if ((packet.type === "chat" || packet.type === "file_message") && packet.message) {
      const msg = packet.message;
      if (!GA.verifyMessage(this.identity, g.groupId, msg)) return;
      if (!g.messages.some((m) => m.id === msg.id)) {
        g.messages.push(msg.markedFromMe(this.identity.deviceId));
        g.messages.sort((a, b) => a.timestamp - b.timestamp);
        this.emit("messagesChanged", g);
      }
    } else if (packet.type === "announce" && packet.peer) {
      if (packet.peer.id !== this.identity.deviceId) {
        g.peers.set(packet.peer.id, packet.peer);
        this.emit("peersChanged", g);
        this.meshAddPeer(g, packet.peer);
      }
    } else if (packet.type === "peer_left" && packet.peer) {
      g.peers.delete(packet.peer.id);
      this.emit("peersChanged", g);
    } else if (packet.type === "group_update") {
      this._handleGroupUpdateAsClient(g, packet);
    } else if (packet.type === "kick_member") {
      this._handleKickAsClient(g, packet);
    } else if (packet.type === "delete_message" && packet.messageId != null) {
      const target = g.messages.find((m) => m.id === packet.messageId);
      if (
        target &&
        (packet.senderId == null || packet.senderId !== target.senderId)
      ) {
        return;
      }
      if (
        !GA.verifyDelete(
          this.identity,
          g.groupId,
          packet.senderId || "",
          packet.messageId,
          packet.senderPubId,
          packet.senderSig
        )
      ) {
        return;
      }
      g.messages = g.messages.filter((m) => m.id !== packet.messageId);
      this.recordTombstone(g, packet.messageId);
      this.emit("messagesChanged", g);
    } else if (packet.type === "typing" && packet.senderId && packet.active != null) {
      this.emit("typingChanged", g, packet.senderId, Boolean(packet.active));
    } else if (packet.type === "edit_message" && packet.messageId && packet.newContent != null) {
      const target = g.messages.find((m) => m.id === packet.messageId);
      if (!target || packet.senderId !== target.senderId) return;
      if (
        !GA.verifyMessageFields(
          this.identity,
          g.groupId,
          packet.senderId,
          packet.messageId,
          target.timestamp,
          packet.newContent,
          packet.senderPubId,
          packet.senderSig
        )
      ) {
        return;
      }
      target.content = packet.newContent;
      target.edited = true;
      if (packet.senderPubId && packet.senderSig) {
        target.senderPubId = packet.senderPubId;
        target.senderSig = packet.senderSig;
      }
      this.emit("messagesChanged", g);
      this.emit("messageEdited", g, packet.messageId, packet.newContent, packet.senderId);
    } else if (packet.type === "reaction" && packet.messageId && packet.emoji) {
      // parity with the relay path: a reaction to a message this member does
      // not hold is ignored (the UI would have nothing to attach it to)
      if (!g.messages.some((m) => m.id === packet.messageId)) return;
      this.emit("reactionChanged", g, packet.messageId, packet.emoji, packet.senderId, Boolean(packet.active));
    } else if (packet.type === "pin_message" && packet.messageId) {
      if (!g.messages.some((m) => m.id === packet.messageId)) return;
      this.emit("pinChanged", g, packet.messageId, packet.senderId, Boolean(packet.active));
    } else if (
      packet.type === "read_receipt" &&
      packet.upToId &&
      packet.readerId &&
      packet.groupId === g.groupId
    ) {
      this.emit("readReceipt", g, packet.readerId, packet.upToId);
    } else if (packet.type === "ping") {
      if (g.hostConn && g.hostConn.alive) {
        try {
          g.hostConn.wire.sendPacket(new M.NetworkPacket({ type: "pong" }));
        } catch (e) {
          /* ignore */
        }
      }
    }
  }

  _handleGroupUpdateAsClient(g, packet) {
    const creator = g.creatorId;
    if (!creator || !packet.senderId || packet.senderId !== creator) {
      this._disconnectFromHost(g);
      return;
    }
    if (
      !GA.verifyGroupUpdate(
        this.identity,
        g.groupId,
        creator,
        packet.groupName || "",
        packet.announcement || "",
        packet.senderPubId,
        packet.senderSig
      )
    ) {
      this._disconnectFromHost(g);
      return;
    }
    let changed = false;
    const newName = (packet.groupName || "").trim();
    if (newName && newName !== g.name) {
      g.name = newName;
      changed = true;
    }
    if (packet.announcement != null) {
      g.announcement = packet.announcement;
      changed = true;
    }
    if (changed) this.emit("groupInfoChanged", g);
    this.meshBroadcastAdmin(g, packet);
  }

  _handleKickAsClient(g, packet) {
    const creator = g.creatorId;
    if (!creator || !packet.senderId || packet.senderId !== creator) {
      this._disconnectFromHost(g);
      return;
    }
    if (
      !GA.verifyKick(
        this.identity,
        g.groupId,
        creator,
        packet.targetId || "",
        packet.senderPubId,
        packet.senderSig
      )
    ) {
      this._disconnectFromHost(g);
      return;
    }
    const target = packet.targetId;
    if (!target) return;
    if (target === this.identity.deviceId) {
      this.emit("kicked", g);
      return;
    }
    g.peers.delete(target);
    this.meshRemovePeer(g, target);
    this.meshBroadcastAdmin(g, packet);
    this.emit("peersChanged", g);
  }

  _disconnectFromHost(g) {
    if (g.hostConn) {
      g.hostConn.channel.destroy();
    }
  }

  sendChatMessage(g, content) {
    if (!U.isValidContent(content)) return null;
    const message = new M.ChatMessage({
      id: U.uuid(),
      content,
      timestamp: U.nowMs(),
      senderId: this.identity.deviceId,
      senderName: this.myName(),
      isFromMe: true,
    });
    GA.signMessage(this.identity, g.groupId, message);
    g.messages.push(message);
    this._enqueueSend(g, new M.NetworkPacket({ type: "chat", message }));
    this.meshBroadcastMessage(g, message);
    this.emit("messagesChanged", g);
    return message;
  }

  async sendFileMessage(g, filePath) {
    let stat;
    try {
      stat = fs.statSync(filePath);
    } catch (e) {
      return null;
    }
    const fileSize = stat.size;
    const fileName = U.baseName(filePath);
    if (!U.isValidContent(fileName) || fileSize > 512 * 1024 * 1024) return null;
    const fileServer = new OutgoingFileServer(U.uuid(), filePath, fileSize, C.randomBytes(C.KEY_LEN));
    let port;
    try {
      port = await fileServer.start();
    } catch (e) {
      return null;
    }
    const fileKeyB64 = fileServer.fileKey.toString("base64");
    const fileInfo = new M.FileInfo(
      fileServer.fileId,
      fileName,
      fileSize,
      U.getLocalIpAddress(),
      port,
      fileKeyB64,
      U.detectMediaKind(fileName)
    );
    const message = new M.ChatMessage({
      id: fileServer.fileId,
      content: fileName,
      timestamp: U.nowMs(),
      senderId: this.identity.deviceId,
      senderName: this.myName(),
      isFromMe: true,
      fileInfo,
    });
    GA.signMessage(this.identity, g.groupId, message);
    g.messages.push(message);
    this._enqueueSend(g, new M.NetworkPacket({ type: "file_message", message }), () => {
      fileServer.close();
      g.messages = g.messages.filter((m) => m.id !== fileServer.fileId);
      this.emit("messagesChanged", g);
    });
    this.meshBroadcastMessage(g, message);
    this.emit("messagesChanged", g);
    return message;
  }

  sendTyping(g, active) {
    this._enqueueSend(
      g,
      new M.NetworkPacket({
        type: "typing",
        groupId: g.groupId,
        senderId: this.identity.deviceId,
        active: Boolean(active),
      })
    );
    this.meshBroadcastTyping(g, this.identity.deviceId, Boolean(active));
  }

  sendDelete(g, messageId) {
    const target = g.messages.find((m) => m.id === messageId);
    if (!target || target.senderId !== this.identity.deviceId) return false;
    g.messages = g.messages.filter((m) => m.id !== messageId);
    this.recordTombstone(g, messageId);
    this.emit("messagesChanged", g);
    const packet = new M.NetworkPacket({
      type: "delete_message",
      messageId,
      senderId: target.senderId,
    });
    GA.signPacket(
      this.identity,
      packet,
      GA.deleteParts(g.groupId, target.senderId, messageId)
    );
    this._enqueueSend(g, packet);
    this.meshBroadcastDelete(g, messageId);
    return true;
  }

  sendEdit(g, messageId, newContent) {
    if (!U.isValidContent(newContent)) return false;
    const target = g.messages.find((m) => m.id === messageId);
    if (!target || target.senderId !== this.identity.deviceId) return false;
    const [pub, sig] = GA.signParts(
      this.identity,
      GA.messageFieldsParts(g.groupId, this.identity.deviceId, messageId, target.timestamp, newContent)
    );
    if (!pub) return false;
    target.content = newContent;
    target.edited = true;
    target.senderPubId = pub;
    target.senderSig = sig;
    this.emit("messagesChanged", g);
    this._enqueueSend(
      g,
      new M.NetworkPacket({
        type: "edit_message",
        groupId: g.groupId,
        messageId,
        senderId: this.identity.deviceId,
        newContent,
        senderPubId: pub,
        senderSig: sig,
      })
    );
    this.meshBroadcastEdit(g, messageId, newContent);
    return true;
  }

  sendReaction(g, messageId, emoji, active) {
    emoji = U.sanitizeEmoji(emoji);
    if (!emoji) return false;
    if (!g.messages.some((m) => m.id === messageId)) return false;
    const packet = new M.NetworkPacket({
      type: "reaction",
      groupId: g.groupId,
      messageId,
      senderId: this.identity.deviceId,
      emoji,
      active: Boolean(active),
    });
    this._enqueueSend(g, packet);
    this.meshBroadcastPacket(g, packet);
    return true;
  }

  sendPin(g, messageId, active) {
    if (!g.messages.some((m) => m.id === messageId)) return false;
    const packet = new M.NetworkPacket({
      type: "pin_message",
      groupId: g.groupId,
      messageId,
      senderId: this.identity.deviceId,
      active: Boolean(active),
    });
    this._enqueueSend(g, packet);
    this.meshBroadcastPacket(g, packet);
    return true;
  }

  sendGroupReadReceipt(g, upToId) {
    if (!upToId || g.lastReceiptSent === upToId) return;
    g.lastReceiptSent = upToId;
    const packet = new M.NetworkPacket({
      type: "read_receipt",
      groupId: g.groupId,
      upToId,
      readerId: this.identity.deviceId,
    });
    this._enqueueSend(g, packet);
    this.meshBroadcastPacket(g, packet);
  }

  sendGroupUpdate(g, newName, announcement) {
    if (!g.isHost) return false;
    const name = (newName || "").trim();
    if (!name && announcement == null) return false;
    if (name) g.name = name;
    if (announcement != null) g.announcement = announcement;
    const packet = new M.NetworkPacket({
      type: "group_update",
      groupId: g.groupId,
      senderId: this.identity.deviceId,
      groupName: name || null,
      announcement,
    });
    GA.signPacket(
      this.identity,
      packet,
      GA.groupUpdateParts(g.groupId, this.identity.deviceId, name, announcement || "")
    );
    this._broadcastToClients(g, packet);
    this.meshBroadcastAdmin(g, packet);
    this.emit("groupInfoChanged", g);
    return true;
  }

  kickMember(g, targetId) {
    if (!g.isHost || !targetId) return false;
    const conn = g.clients.get(targetId);
    const known = g.peers.has(targetId);
    g.clients.delete(targetId);
    g.peers.delete(targetId);
    if (!known && !conn) return false;
    const packet = new M.NetworkPacket({
      type: "kick_member",
      groupId: g.groupId,
      senderId: this.identity.deviceId,
      targetId,
    });
    GA.signPacket(
      this.identity,
      packet,
      GA.kickParts(g.groupId, this.identity.deviceId, targetId)
    );
    if (conn) {
      try {
        conn.wire.sendPacket(packet);
      } catch (e) {
        /* ignore */
      }
      setTimeout(() => conn.channel.destroy(), 500);
    }
    this._broadcastToClients(g, packet, targetId);
    this.emit("peersChanged", g);
    return true;
  }

  _enqueueSend(g, packet, onFailed = null) {
    g.sendQueue.push({ packet, onFailed });
    this._pumpSend(g);
  }

  _pumpSend(g) {
    if (g.pumping) return;
    g.pumping = true;
    (async () => {
      while (g.sendQueue.length > 0) {
        const item = g.sendQueue.shift();
        try {
          if (item.packet.targetId != null) {
            if (g.isHost) {
              const conn = g.clients.get(item.packet.targetId);
              if (conn) conn.wire.sendPacket(item.packet);
            } else if (g.hostConn && g.hostConn.alive) {
              g.hostConn.wire.sendPacket(item.packet);
            } else {
              throw new Error("no host connection");
            }
          } else if (g.isHost) {
            this._broadcastToClients(g, item.packet);
          } else if (g.hostConn && g.hostConn.alive) {
            g.hostConn.wire.sendPacket(item.packet);
          } else {
            throw new Error("no host connection");
          }
        } catch (e) {
          if (item.onFailed) {
            try {
              item.onFailed();
            } catch (e2) {
              /* ignore */
            }
          }
        }
      }
      g.pumping = false;
    })();
  }

  _applyDeletedIds(g, ids) {
    if (!ids || !ids.length) return;
    const idSet = new Set(ids);
    const gone = g.messages.filter((m) => idSet.has(m.id)).map((m) => m.id);
    if (gone.length) {
      g.messages = g.messages.filter((m) => !idSet.has(m.id));
      this.emit("messagesChanged", g);
    }
    this.emit("deletedIdsReceived", g, ids);
  }

  recordTombstone(g, messageId) {
    if (!g.tombstones.includes(messageId)) {
      g.tombstones.push(messageId);
      if (g.tombstones.length > TOMBSTONE_CAP) {
        g.tombstones.splice(0, g.tombstones.length - TOMBSTONE_CAP);
      }
    }
  }

  meshAddPeer(g, peer) {
    if (!g || g.isHost) return;
    if (!peer.ipAddress || !(peer.port >= 1 && peer.port <= 65535)) return;
    const mine = this.identity.deviceId;
    if (peer.id === mine || !peer.id) return;
    if (!g.meshPeers.has(peer.id) && g.meshPeers.size >= MAX_PEERS_PER_GROUP) return;
    g.meshPeers.set(peer.id, peer);
    if (g.hostPeer && peer.id === g.hostPeer.id) return;
    if (mine >= peer.id) return;
    if (g.meshLinks.has(peer.id)) return;
    if (g.meshDialing.has(peer.id)) return;
    g.meshDialing.add(peer.id);
    this._meshDialLoop(g, peer).finally(() => {
      g.meshDialing.delete(peer.id);
    });
  }

  meshRemovePeer(g, peerId) {
    if (!g) return;
    g.meshPeers.delete(peerId);
    const link = g.meshLinks.get(peerId);
    if (link) {
      link.alive = false;
      link.channel.destroy();
      g.meshLinks.delete(peerId);
    }
  }

  meshSyncPeers(g, peers) {
    if (!g || g.isHost) return;
    const mine = this.identity.deviceId;
    const keep = new Set(peers.filter((p) => p.id !== mine).map((p) => p.id));
    for (const pid of Array.from(g.meshPeers.keys())) {
      if (!keep.has(pid)) this.meshRemovePeer(g, pid);
    }
    for (const peer of peers) this.meshAddPeer(g, peer);
  }

  async _meshDialLoop(g, peer) {
    let failures = 0;
    for (;;) {
      if (g.stopped) return;
      // the peer record can be re-announced with a fresher address
      const target = g.meshPeers.get(peer.id) || peer;
      const existing = g.meshLinks.get(peer.id);
      if (existing && existing.alive) {
        if (!(await this._meshSleep(g, RETRY_INTERVAL))) return;
        continue;
      }
      const link = await this._meshTryConnect(g, target);
      if (link == null) {
        failures += 1;
        if (failures >= MAX_DIAL_ATTEMPTS) return;
        if (!(await this._meshSleep(g, Math.min(RETRY_INTERVAL * failures, 60000)))) return;
        continue;
      }
      // a link that DID come up resets the counter: a flaky-but-real member
      // is retried patiently across drops (Python parity)
      failures = 0;
      g.meshLinks.set(peer.id, link);
      if (g.messages.length) {
        this._meshSendHistory(link, g, g.messages.slice(-HISTORY_CAP));
      }
      this._meshStartSender(link);
      this._meshPingLoop(link);
      // runs for the link's lifetime: when it returns the link dropped and
      // the loop redials after a pause, so a dead mesh link is re-established
      // without waiting for a new announce (Python parity)
      await this._meshReadLoop(g, link);
      if (!(await this._meshSleep(g, RETRY_INTERVAL))) return;
    }
  }

  async _meshSleep(g, ms) {
    const deadline = Date.now() + ms;
    while (true) {
      if (g.stopped) return false;
      const remaining = deadline - Date.now();
      if (remaining <= 0) return true;
      await U.sleep(Math.min(500, remaining));
    }
  }

  async _meshTryConnect(g, peer) {
    let socket = null;
    try {
      socket = await connectSocket(peer.ipAddress, peer.port, 8000);
      const channel = new W.LineChannel(socket);
      const wire = new W.Wire(channel);
      await W.Handshake.initiate(wire, W.MODE_MESH, g.groupId, g.password);
      wire.sendPacket(
        new M.NetworkPacket({ type: "mesh_hello", groupId: g.groupId, peer: this.myPeerFor(g) })
      );
      const ack = await wire.recvPacket(15000);
      if (ack == null || ack.type !== "mesh_ack" || !ack.peer) {
        channel.destroy();
        return null;
      }
      return {
        peerId: peer.id,
        peerName: ack.peer.name,
        channel,
        wire,
        alive: true,
        sendQueue: [],
        pumping: false,
      };
    } catch (e) {
      if (socket) socket.destroy();
      return null;
    }
  }

  async handleMeshHello(g, channel, wire, packet) {
    const groupId = packet.groupId;
    const peer = packet.peer;
    if (!g || g.isHost || groupId !== g.groupId || !peer) {
      channel.destroy();
      return;
    }
    this._meshRegisterLink(g, peer, channel, wire);
  }

  _meshRegisterLink(g, peer, channel, wire) {
    const link = {
      peerId: peer.id,
      peerName: peer.name,
      channel,
      wire,
      alive: true,
      sendQueue: [],
      pumping: false,
    };
    if (!g.meshPeers.has(peer.id) && g.meshPeers.size >= MAX_PEERS_PER_GROUP) {
      channel.destroy();
      return;
    }
    g.meshPeers.set(peer.id, peer);
    const existing = g.meshLinks.get(peer.id);
    if (existing && existing.alive) {
      channel.destroy();
      return;
    }
    if (existing) {
      existing.alive = false;
      existing.channel.destroy();
    }
    g.meshLinks.set(peer.id, link);
    const history = g.messages.slice(-HISTORY_CAP);
    try {
      wire.sendPacket(
        new M.NetworkPacket({ type: "mesh_ack", groupId: g.groupId, peer: this.myPeerFor(g) })
      );
    } catch (e) {
      if (g.meshLinks.get(peer.id) === link) {
        g.meshLinks.delete(peer.id);
      }
      link.alive = false;
      channel.destroy();
      return;
    }
    if (history.length) {
      this._meshSendHistory(link, g, history);
    }
    this._meshStartSender(link);
    this._meshReadLoop(g, link);
    this._meshPingLoop(link);
  }

  _meshLinkWrite(link, packet) {
    if (!link.sendQueue || !link.alive) return;
    if (link.sendQueue.length >= MESH_SEND_QUEUE_CAP) {
      link.alive = false;
      link.channel.destroy();
      return;
    }
    link.sendQueue.push(packet);
    this._meshStartSender(link);
  }

  _meshStartSender(link) {
    if (link.pumping) return;
    link.pumping = true;
    (async () => {
      while (link.alive && link.sendQueue.length > 0) {
        const packet = link.sendQueue.shift();
        try {
          link.wire.sendPacket(packet);
        } catch (e) {
          link.alive = false;
          link.channel.destroy();
          break;
        }
      }
      link.pumping = false;
    })();
  }

  _meshSendHistory(link, g, history) {
    let batch = [];
    let estimated = 0;
    const flush = () => {
      if (!batch.length) return;
      try {
        link.wire.sendPacket(
          new M.NetworkPacket({ type: "history_reply", groupId: g.groupId, messages: batch })
        );
      } catch (e) {
        /* ignore */
      }
      batch = [];
      estimated = 0;
    };
    for (const msg of history) {
      const size = msg.content.length * 4 + 1024;
      if (estimated > 0 && estimated + size > HISTORY_CHUNK_BYTES) flush();
      batch.push(msg);
      estimated += size;
    }
    flush();
    const deletedIds = g.tombstones.slice(-TOMBSTONE_CAP);
    if (deletedIds.length) {
      try {
        link.wire.sendPacket(
          new M.NetworkPacket({
            type: "history_reply",
            groupId: g.groupId,
            deletedIds,
          })
        );
      } catch (e) {
        /* ignore */
      }
    }
  }

  async _meshReadLoop(g, link) {
    while (link.alive && !g.stopped) {
      let packet;
      try {
        packet = await link.wire.recvPacket(HEARTBEAT_TIMEOUT);
      } catch (e) {
        break;
      }
      if (packet == null) break;
      try {
        this._meshProcessPacket(g, link, packet);
      } catch (e) {
        /* ignore */
      }
    }
    this._meshLinkClosed(g, link);
  }

  _meshLinkClosed(g, link) {
    link.alive = false;
    link.channel.destroy();
    if (g.meshLinks.get(link.peerId) === link) {
      g.meshLinks.delete(link.peerId);
    }
  }

  _meshPingLoop(link) {
    const timer = setInterval(() => {
      if (!link.alive) {
        clearInterval(timer);
        return;
      }
      try {
        link.wire.sendPacket(new M.NetworkPacket({ type: "ping" }));
      } catch (e) {
        clearInterval(timer);
      }
    }, HEARTBEAT_INTERVAL);
  }

  _meshProcessPacket(g, link, packet) {
    if ((packet.type === "mesh_chat" || packet.type === "file_message") && packet.message) {
      const msg = packet.message;
      if (msg.senderId !== link.peerId || !U.isValidContent(msg.content)) return;
      if (!GA.verifyMessage(this.identity, g.groupId, msg)) return;
      this._meshHandleIncoming(g, [msg]);
    } else if (packet.type === "delete_message") {
      if (packet.messageId && packet.senderId) {
        this._meshHandleDelete(g, packet.messageId, packet.senderId, packet.senderPubId, packet.senderSig);
      }
    } else if (packet.type === "edit_message" && packet.messageId && packet.newContent != null) {
      this._meshHandleEdit(g, link, packet);
    } else if (packet.type === "reaction" && packet.messageId && packet.emoji) {
      if (packet.senderId === link.peerId) {
        this.emit("reactionChanged", g, packet.messageId, packet.emoji, packet.senderId, Boolean(packet.active));
      }
    } else if (packet.type === "pin_message" && packet.messageId) {
      if (packet.senderId === link.peerId) {
        this.emit("pinChanged", g, packet.messageId, packet.senderId, Boolean(packet.active));
      }
    } else if (
      packet.type === "read_receipt" &&
      packet.upToId &&
      packet.readerId === link.peerId
    ) {
      this.emit("readReceipt", g, packet.readerId, packet.upToId);
    } else if (
      packet.type === "typing" &&
      packet.active != null &&
      packet.senderId === link.peerId
    ) {
      this.emit("typingChanged", g, packet.senderId, Boolean(packet.active));
    } else if (packet.type === "history_reply") {
      this._meshHandleHistory(g, link, packet);
    } else if (packet.type === "mesh_announce" && packet.peer) {
      this.meshAddPeer(g, packet.peer);
    } else if (packet.type === "group_update" || packet.type === "kick_member") {
      this._meshHandleAdmin(g, link, packet);
    } else if (packet.type === "ping") {
      try {
        link.wire.sendPacket(new M.NetworkPacket({ type: "pong" }));
      } catch (e) {
        /* ignore */
      }
    }
  }

  _meshHandleHistory(g, link, packet) {
    const incoming = packet.messages || [];
    const validHistory = incoming.filter(
      (m) =>
        m.senderId &&
        U.isValidContent(m.content) &&
        GA.verifyMessage(this.identity, g.groupId, m)
    );
    const edits = [];
    const stillNew = [];
    for (const m of validHistory) {
      const local = g.messages.find((x) => x.id === m.id);
      if (local == null) {
        stillNew.push(m);
      } else if (
        m.senderId === link.peerId &&
        local.senderId === m.senderId &&
        local.content !== m.content
      ) {
        local.content = m.content;
        local.edited = true;
        if (m.senderPubId && m.senderSig) {
          local.senderPubId = m.senderPubId;
          local.senderSig = m.senderSig;
        }
        edits.push(m);
      }
    }
    if (stillNew.length) {
      this._meshHandleIncoming(g, stillNew);
    }
    for (const m of edits) {
      this.emit("messagesChanged", g);
      this.emit("messageEdited", g, m.id, m.content, m.senderId);
    }
    if (packet.deletedIds && packet.deletedIds.length) {
      const ids = U.sanitizeDeletedIds(packet.deletedIds);
      if (ids.length) {
        const idSet = new Set(ids);
        g.messages = g.messages.filter((m) => !idSet.has(m.id));
        this.emit("messagesChanged", g);
        this.emit("deletedIdsReceived", g, ids);
      }
    }
  }

  _meshHandleIncoming(g, incoming) {
    const mine = this.identity.deviceId;
    const ids = new Set(g.messages.map((m) => m.id));
    const newOnes = incoming.filter((m) => !ids.has(m.id)).map((m) => m.markedFromMe(mine));
    if (!newOnes.length) return;
    g.messages.push(...newOnes);
    g.messages.sort((a, b) => a.timestamp - b.timestamp);
    if (g.messages.length > HISTORY_CAP) {
      g.messages.splice(0, g.messages.length - HISTORY_CAP);
    }
    this.emit("messagesChanged", g);
  }

  _meshHandleDelete(g, messageId, senderId, senderPubId, senderSig) {
    const target = g.messages.find((m) => m.id === messageId);
    if (!target) return;
    if (target.senderId !== senderId) return;
    if (!GA.verifyDelete(this.identity, g.groupId, senderId, messageId, senderPubId, senderSig)) {
      return;
    }
    g.messages = g.messages.filter((m) => m.id !== messageId);
    this.recordTombstone(g, messageId);
    this.emit("messagesChanged", g);
  }

  _meshHandleEdit(g, link, packet) {
    if (packet.senderId !== link.peerId) return;
    const target = g.messages.find((m) => m.id === packet.messageId);
    if (!target || target.senderId !== packet.senderId) return;
    const pub = packet.senderPubId || target.senderPubId;
    if (!pub) return;
    if (
      !GA.verifyMessageFields(
        this.identity,
        g.groupId,
        packet.senderId,
        packet.messageId,
        target.timestamp,
        packet.newContent,
        pub,
        packet.senderSig
      )
    ) {
      return;
    }
    target.content = packet.newContent;
    target.edited = true;
    target.senderPubId = pub;
    target.senderSig = packet.senderSig;
    this.emit("messagesChanged", g);
    this.emit("messageEdited", g, packet.messageId, packet.newContent, packet.senderId);
  }

  _meshHandleAdmin(g, link, packet) {
    const creator = g.creatorId;
    if (!creator || !packet.senderId || packet.senderId !== creator) {
      link.alive = false;
      link.channel.destroy();
      return;
    }
    let verified;
    if (packet.type === "group_update") {
      verified = GA.verifyGroupUpdate(
        this.identity,
        g.groupId,
        creator,
        packet.groupName || "",
        packet.announcement || "",
        packet.senderPubId,
        packet.senderSig
      );
    } else {
      verified = GA.verifyKick(
        this.identity,
        g.groupId,
        creator,
        packet.targetId || "",
        packet.senderPubId,
        packet.senderSig
      );
    }
    if (!verified) {
      link.alive = false;
      link.channel.destroy();
      return;
    }
    if (packet.type === "kick_member") {
      const target = packet.targetId;
      if (!target) return;
      if (target !== this.identity.deviceId) {
        this.meshRemovePeer(g, target);
      }
    }
    this._meshApplyAdmin(g, packet);
  }

  _meshApplyAdmin(g, packet) {
    if (packet.type === "group_update") {
      let changed = false;
      const newName = (packet.groupName || "").trim();
      if (newName && newName !== g.name) {
        g.name = newName;
        changed = true;
      }
      if (packet.announcement != null) {
        g.announcement = packet.announcement;
        changed = true;
      }
      if (changed) this.emit("groupInfoChanged", g);
    } else if (packet.type === "kick_member") {
      const target = packet.targetId;
      if (target === this.identity.deviceId) {
        this.emit("kicked", g);
      } else {
        g.peers.delete(target);
        this.emit("peersChanged", g);
      }
    }
  }

  meshBroadcastMessage(g, msg) {
    if (g.isHost) return;
    if (!g.messages.some((m) => m.id === msg.id)) {
      g.messages.push(msg.clone());
      g.messages.sort((a, b) => a.timestamp - b.timestamp);
      if (g.messages.length > HISTORY_CAP) {
        g.messages.splice(0, g.messages.length - HISTORY_CAP);
      }
    }
    const packet = new M.NetworkPacket({ type: "mesh_chat", groupId: g.groupId, message: msg });
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  meshBroadcastDelete(g, messageId) {
    if (g.isHost) return;
    const myId = this.identity.deviceId;
    const had = g.messages.some((m) => m.id === messageId);
    g.messages = g.messages.filter((m) => m.id !== messageId);
    const packet = new M.NetworkPacket({
      type: "delete_message",
      messageId,
      senderId: myId,
    });
    GA.signPacket(this.identity, packet, GA.deleteParts(g.groupId, myId, messageId));
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  meshBroadcastEdit(g, messageId, newContent) {
    if (g.isHost) return;
    const myId = this.identity.deviceId;
    const target = g.messages.find((m) => m.id === messageId);
    if (!target || target.senderId !== myId) return;
    const [pub, sig] = GA.signParts(
      this.identity,
      GA.messageFieldsParts(g.groupId, myId, messageId, target.timestamp, newContent)
    );
    if (!pub) return;
    target.content = newContent;
    target.edited = true;
    target.senderPubId = pub;
    target.senderSig = sig;
    const packet = new M.NetworkPacket({
      type: "edit_message",
      groupId: g.groupId,
      messageId,
      senderId: myId,
      newContent,
      senderPubId: pub,
      senderSig: sig,
    });
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  meshBroadcastTyping(g, senderId, active) {
    if (g.isHost) return;
    const packet = new M.NetworkPacket({
      type: "typing",
      groupId: g.groupId,
      senderId,
      active: Boolean(active),
    });
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  meshBroadcastPacket(g, packet) {
    if (g.isHost) return;
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  meshBroadcastAdmin(g, packet) {
    if (g.isHost) return;
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  meshAnnouncePeer(g, peer) {
    if (g.isHost) return;
    this.meshAddPeer(g, peer);
    const packet = new M.NetworkPacket({
      type: "mesh_announce",
      groupId: g.groupId,
      peer,
    });
    for (const link of g.meshLinks.values()) {
      this._meshLinkWrite(link, packet);
    }
  }

  enterMesh(g, history, savedPeers) {
    if (g.isHost) return;
    for (const peer of savedPeers || []) {
      this.meshAddPeer(g, peer);
    }
    for (const peer of g.peers.values()) {
      this.meshAddPeer(g, peer);
    }
    if (history && history.length) {
      const ids = new Set(g.messages.map((m) => m.id));
      for (const m of history) {
        if (!ids.has(m.id)) {
          g.messages.push(m);
        }
      }
      g.messages.sort((a, b) => a.timestamp - b.timestamp);
    }
  }

  downloadFile(fileInfo, targetPath, options = {}) {
    if (typeof options === "function") options = { onProgress: options };
    return downloadFileOffer(fileInfo, targetPath, options);
  }
}

function connectSocket(host, port, timeoutMs) {
  return new Promise((resolve, reject) => {
    const s = net.connect({ host, port }, () => resolve(s));
    s.setTimeout(timeoutMs, () => {
      s.destroy();
      reject(new Error("timeout"));
    });
    s.on("error", (err) => reject(err));
  });
}

module.exports = { GroupApp };
