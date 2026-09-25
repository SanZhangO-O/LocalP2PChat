"use strict";

const net = require("net");
const W = require("./wire");
const M = require("./models");

const MAX_ACTIVE_CONNECTIONS = 256;
const HANDSHAKE_RATE_LIMIT = 60;
const HANDSHAKE_RATE_WINDOW = 60000;

class SharedListener {
  constructor(port, identity, groupApp, directManager) {
    this.port = port;
    this.identity = identity;
    this.groupApp = groupApp;
    this.directManager = directManager;
    this.server = null;
    this.activeHandlers = 0;
    this.handshakeAttempts = new Map();
  }

  start() {
    return new Promise((resolve, reject) => {
      this.server = net.createServer((socket) => {
        socket.setNoDelay(true);
        if (!this._allowHandshake(socket)) {
          socket.destroy();
          return;
        }
        if (this.activeHandlers >= MAX_ACTIVE_CONNECTIONS) {
          socket.destroy();
          return;
        }
        this.activeHandlers += 1;
        this._handle(socket).finally(() => {
          this.activeHandlers = Math.max(0, this.activeHandlers - 1);
        });
      });
      this.server.on("error", reject);
      this.server.listen(this.port, "0.0.0.0", () => resolve(this.port));
    });
  }

  stop() {
    if (this.server) {
      try {
        this.server.close();
      } catch (e) {
        /* ignore */
      }
    }
  }

  _allowHandshake(socket) {
    let ip = "";
    try {
      ip = socket.remoteAddress || "";
    } catch (e) {
      ip = "";
    }
    if (!ip) return true;
    const now = Date.now();
    let stamps = this.handshakeAttempts.get(ip);
    if (!stamps) {
      stamps = [];
      this.handshakeAttempts.set(ip, stamps);
    }
    stamps = stamps.filter((ts) => now - ts < HANDSHAKE_RATE_WINDOW);
    for (const [key, list] of this.handshakeAttempts) {
      if (key !== ip && list.every((ts) => now - ts >= HANDSHAKE_RATE_WINDOW)) {
        this.handshakeAttempts.delete(key);
      }
    }
    this.handshakeAttempts.set(ip, stamps);
    if (stamps.length >= HANDSHAKE_RATE_LIMIT) return false;
    stamps.push(now);
    return true;
  }

  _close(socket) {
    try {
      socket.destroy();
    } catch (e) {
      /* ignore */
    }
  }

  async _handle(socket) {
    const channel = new W.LineChannel(socket);
    try {
      const firstLine = await channel.readLine(15000);
      if (firstLine == null) {
        this._close(socket);
        return;
      }
      let start;
      try {
        start = M.NetworkPacket.fromJson(firstLine);
      } catch (e) {
        this._close(socket);
        return;
      }
      if (start.type === "file_download") {
        this._close(socket);
        return;
      }
      if (start.type !== W.HS_START) {
        this._close(socket);
        return;
      }
      const wire = new W.Wire(channel);
      const mode = start.hsMode;
      if (mode === W.MODE_DIRECT) {
        const peerIdent = await W.Handshake.acceptDirect(wire, this.identity, start, null);
        if (peerIdent == null) {
          this._close(socket);
          return;
        }
        let hello;
        try {
          hello = await wire.recvPacket(15000);
        } catch (e) {
          this._close(socket);
          return;
        }
        if (hello == null || hello.type !== W.DIRECT_HELLO) {
          this._close(socket);
          return;
        }
        await this.directManager.handleDirectHello(socket, channel, wire, hello, peerIdent);
        return;
      }
      if (mode === W.MODE_MESH) {
        const secured = await W.Handshake.accept(wire, start, (m, gid) =>
          this.groupApp.passwordFor(m, gid)
        );
        if (secured == null) {
          this._close(socket);
          return;
        }
        let hello;
        try {
          hello = await wire.recvPacket(15000);
        } catch (e) {
          this._close(socket);
          return;
        }
        if (hello == null || hello.type !== "mesh_hello") {
          this._close(socket);
          return;
        }
        if (hello.groupId !== start.groupId) {
          this._close(socket);
          return;
        }
        let g = null;
        for (const group of this.groupApp.groups.values()) {
          if (!group.isHost && group.groupId === hello.groupId) {
            g = group;
            break;
          }
        }
        if (!g) {
          this._close(socket);
          return;
        }
        await this.groupApp.handleMeshHello(g, channel, wire, hello);
        return;
      }
      if (mode !== W.MODE_QUERY && mode !== W.MODE_JOIN) {
        this._close(socket);
        return;
      }
      const secured = await W.Handshake.accept(wire, start, (m, gid) =>
        this.groupApp.passwordFor(m, gid)
      );
      if (secured == null) {
        this._close(socket);
        return;
      }
      let packet;
      try {
        packet = await wire.recvPacket(15000);
      } catch (e) {
        this._close(socket);
        return;
      }
      if (packet == null || packet.type !== mode) {
        this._close(socket);
        return;
      }
      if (packet.groupId !== start.groupId) {
        this._close(socket);
        return;
      }
      const hostGroup = this.groupApp.resolveByNumericId(start.groupId);
      if (hostGroup) {
        const handled = await this.groupApp.handleQueryOrJoinHost(channel, wire, start, packet);
        if (!handled) this._close(socket);
        return;
      }
      const memberGroup = this.groupApp.memberGroupByJoinId(start.groupId);
      if (memberGroup) {
        const handled = await this.groupApp.handleQueryOrJoinMember(
          channel,
          wire,
          start,
          packet
        );
        if (!handled) this._close(socket);
        return;
      }
      try {
        wire.sendPacket(new M.NetworkPacket({ type: "join_rejected" }));
      } catch (e) {
        /* ignore */
      }
      this._close(socket);
    } catch (e) {
      this._close(socket);
    }
  }
}

module.exports = { SharedListener };
