"use strict";

const M = require("./models");
const C = require("./crypto");
const U = require("./util");

const HS_START = "hs_start";
const HS_ACK = "hs_ack";
const HS_CONFIRM = "hs_confirm";
const HS_OK = "hs_ok";
const HS_REJECT = "hs_reject";

const MODE_QUERY = "query";
const MODE_JOIN = "join";
const MODE_MESH = "mesh";
const MODE_DIRECT = "direct";

const DIRECT_HELLO = "direct_hello";
const DIRECT_ACK = "direct_ack";
const DIRECT_PENDING = "direct_pending";

const NONCE_CACHE_SIZE = 4096;

class WireException extends Error {}

function renderGroupId(groupId) {
  return groupId == null ? "null" : groupId;
}

class LineChannel {
  constructor(socket) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.queue = [];
    this.waiters = [];
    this.closed = false;
    this.error = null;
    socket.on("data", (chunk) => this._onData(chunk));
    socket.on("error", (err) => this._onError(err));
    socket.on("close", () => this._onClose());
    socket.on("end", () => this._onClose());
  }

  _onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const idx = this.buffer.indexOf(0x0a);
      if (idx < 0) {
        const partial = this.buffer.toString("utf8");
        if (partial.length > U.MAX_LINE_LENGTH) {
          this._fail(new WireException("line too long"));
        }
        return;
      }
      let line = this.buffer.subarray(0, idx).toString("utf8");
      this.buffer = this.buffer.subarray(idx + 1);
      line = line.replace(/\r$/, "");
      if (line.length > U.MAX_LINE_LENGTH) {
        this._fail(new WireException("line too long"));
        return;
      }
      this._push(line);
    }
  }

  _push(line) {
    const waiter = this.waiters.shift();
    if (waiter) {
      clearTimeout(waiter.timer);
      waiter.resolve(line);
    } else {
      this.queue.push(line);
    }
  }

  _fail(err) {
    this.error = err;
    this.closed = true;
    const waiters = this.waiters.splice(0);
    for (const w of waiters) {
      clearTimeout(w.timer);
      w.reject(err);
    }
    this._teardown();
  }

  _onError(err) {
    this._fail(err instanceof WireException ? err : new WireException(String(err)));
  }

  _onClose() {
    this.closed = true;
    const waiters = this.waiters.splice(0);
    for (const w of waiters) {
      clearTimeout(w.timer);
      w.resolve(null);
    }
    this._teardown();
  }

  _teardown() {
    this.socket.removeAllListeners("data");
    try {
      this.socket.destroy();
    } catch (e) {
      /* ignore */
    }
  }

  writeLine(line) {
    if (this.closed) throw new WireException("connection closed");
    this.socket.write(Buffer.from(line + "\n", "utf8"));
  }

  readLine(timeoutMs = 15000) {
    if (this.queue.length > 0) {
      return Promise.resolve(this.queue.shift());
    }
    if (this.error) return Promise.reject(this.error);
    if (this.closed) return Promise.resolve(null);
    return new Promise((resolve, reject) => {
      const waiter = {
        resolve,
        reject,
        timer: setTimeout(() => {
          const idx = this.waiters.indexOf(waiter);
          if (idx >= 0) this.waiters.splice(idx, 1);
          reject(new WireException("read timeout"));
        }, timeoutMs),
      };
      this.waiters.push(waiter);
    });
  }

  gracefulClose() {
    try {
      this.socket.end();
    } catch (e) {
      /* ignore */
    }
    setTimeout(() => {
      try {
        this.socket.destroy();
      } catch (e) {
        /* ignore */
      }
    }, 250);
  }

  destroy() {
    this.closed = true;
    this._teardown();
  }
}

class Wire {
  constructor(channel) {
    this.channel = channel;
    this.key = null;
    this.sendSeq = 0;
    this.recvSeq = 0;
    this.recvSeqEnforced = false;
    this.seenNonces = new Map();
  }

  activate(sessionKey) {
    this.key = sessionKey;
  }

  get isSecure() {
    return this.key != null;
  }

  sendPacket(packet) {
    if (this.key == null) throw new WireException("wire not secured yet");
    this.sendSeq += 1;
    const dict = packet.toDict();
    dict.seq = this.sendSeq;
    const json = JSON.stringify(dict);
    const line = C.toB64(C.aesGcmEncrypt(this.key, Buffer.from(json, "utf8")));
    if (line.length > U.MAX_LINE_LENGTH) throw new WireException("encrypted line exceeds cap");
    this.channel.writeLine(line);
  }

  feedLine(line) {
    if (!line) return null;
    if (this.key == null) throw new WireException("wire not secured yet");
    let blob;
    try {
      blob = C.fromB64(line);
    } catch (e) {
      throw new WireException("malformed encrypted line");
    }
    if (blob.length > C.GCM_NONCE_LEN) {
      const nonce = blob.subarray(0, C.GCM_NONCE_LEN);
      const nonceKey = nonce.toString("binary");
      if (this.seenNonces.has(nonceKey)) {
        throw new WireException("replayed packet (nonce reuse)");
      }
      this.seenNonces.set(nonceKey, true);
      while (this.seenNonces.size > NONCE_CACHE_SIZE) {
        const first = this.seenNonces.keys().next().value;
        this.seenNonces.delete(first);
      }
    }
    let plain;
    try {
      plain = C.aesGcmDecrypt(this.key, blob);
    } catch (e) {
      throw new WireException("decrypt failed (tampered or wrong key)");
    }
    let packet;
    try {
      packet = M.NetworkPacket.fromJson(plain.toString("utf8"));
    } catch (e) {
      if (e instanceof WireException) throw e;
      throw new WireException("malformed packet JSON");
    }
    if (packet.seq == null) {
      if (this.recvSeqEnforced) {
        throw new WireException("packet sequence violation (missing seq)");
      }
      return packet;
    }
    const expected = this.recvSeq + 1;
    if (packet.seq !== expected) {
      throw new WireException(
        `packet sequence violation (got ${packet.seq}, want ${expected})`
      );
    }
    this.recvSeq = packet.seq;
    this.recvSeqEnforced = true;
    return packet;
  }

  async recvPacket(timeoutMs = 60000) {
    const line = await this.channel.readLine(timeoutMs);
    return this.feedLine(line);
  }

  _assertHandshakePhase() {
    if (this.key != null) {
      throw new WireException("plaintext IO is forbidden after activate()");
    }
  }

  sendRaw(packet) {
    this._assertHandshakePhase();
    this.channel.writeLine(packet.toJson());
  }

  sendRawReject(message) {
    this.sendRaw(new M.NetworkPacket({ type: HS_REJECT, errorMessage: message }));
  }

  async recvRaw(timeoutMs = 15000) {
    this._assertHandshakePhase();
    const line = await this.channel.readLine(timeoutMs);
    if (line == null) return null;
    try {
      return M.NetworkPacket.fromJson(line);
    } catch (e) {
      return null;
    }
  }
}

const Handshake = {
  async initiate(wire, mode, groupId, password) {
    const eph = C.ECKeyPair.generate();
    const ephC = eph.publicB64;
    wire.sendRaw(
      new M.NetworkPacket({ type: HS_START, hsMode: mode, groupId, eph: ephC })
    );
    const ack = await wire.recvRaw();
    if (ack == null) throw new WireException("\u5bf9\u65b9\u65e0\u54cd\u5e94");
    if (ack.type === HS_REJECT) throw new WireException(ack.errorMessage || "\u8fde\u63a5\u88ab\u62d2\u7edd");
    if (ack.type !== HS_ACK || !ack.eph) throw new WireException("\u65e0\u6548\u7684\u63e1\u624b\u54cd\u5e94");
    const ephS = ack.eph;
    try {
      C.decodePub(ephS);
    } catch (e) {
      throw new WireException("\u65e0\u6548\u7684\u63e1\u624b\u5bc6\u94a5");
    }
    const transcript = `${mode}|${renderGroupId(groupId)}|${ephC}|${ephS}`;
    const salt = C.sha256(Buffer.from(transcript, "utf8"));
    const pwKey = await C.pbkdf2Sha1(password || "", salt, C.PBKDF2_ITERATIONS, C.KEY_LEN);
    const shared = eph.shared(ephS);
    const clientMac = C.toB64(
      C.hmacSha256(pwKey, Buffer.from(`lc-client|${transcript}`, "utf8"))
    );
    wire.sendRaw(new M.NetworkPacket({ type: HS_CONFIRM, mac: clientMac }));
    const ok = await wire.recvRaw();
    if (ok == null) throw new WireException("\u5bf9\u65b9\u65e0\u54cd\u5e94");
    if (ok.type === HS_REJECT) throw new WireException(ok.errorMessage || "\u8fde\u63a5\u88ab\u62d2\u7edd");
    if (ok.type !== HS_OK || !ok.mac) throw new WireException("\u63e1\u624b\u786e\u8ba4\u65e0\u6548");
    const expected = C.hmacSha256(pwKey, Buffer.from(`lc-server|${transcript}`, "utf8"));
    let providedMac;
    try {
      providedMac = C.fromB64(ok.mac);
    } catch (e) {
      throw new WireException("\u65e0\u6548\u7684\u63e1\u624b\u786e\u8ba4");
    }
    if (!C.constantTimeEquals(providedMac, expected)) {
      throw new WireException("\u5bf9\u65b9\u5bc6\u7801\u9a8c\u8bc1\u5931\u8d25");
    }
    wire.activate(C.hkdfSha256(Buffer.concat([shared, pwKey]), salt, C.INFO_SESSION, C.KEY_LEN));
    return wire;
  },

  async accept(wire, start, passwordFor) {
    const reject = (message) => {
      try {
        wire.sendRawReject(message);
      } catch (e) {
        /* ignore */
      }
    };
    const mode = start.hsMode;
    if (!mode || !start.eph) {
      reject("\u65e0\u6548\u7684\u63e1\u624b");
      return null;
    }
    try {
      C.decodePub(start.eph);
    } catch (e) {
      reject("\u65e0\u6548\u7684\u63e1\u624b\u5bc6\u94a5");
      return null;
    }
    let password = null;
    try {
      password = await passwordFor(mode, start.groupId);
    } catch (e) {
      password = null;
    }
    if (password == null) {
      reject("\u8be5\u8bbe\u5907\u4e0d\u5b58\u5728\u6b64\u7fa4\u7ec4");
      return null;
    }
    const eph = C.ECKeyPair.generate();
    const ephC = start.eph;
    const ephS = eph.publicB64;
    wire.sendRaw(new M.NetworkPacket({ type: HS_ACK, eph: ephS }));
    const transcript = `${mode}|${renderGroupId(start.groupId)}|${ephC}|${ephS}`;
    const salt = C.sha256(Buffer.from(transcript, "utf8"));
    const pwKey = await C.pbkdf2Sha1(password || "", salt, C.PBKDF2_ITERATIONS, C.KEY_LEN);
    const confirm = await wire.recvRaw();
    if (confirm == null || confirm.type !== HS_CONFIRM || !confirm.mac) {
      reject("\u9700\u8981\u7fa4\u7ec4\u5bc6\u7801");
      return null;
    }
    const expected = C.hmacSha256(pwKey, Buffer.from(`lc-client|${transcript}`, "utf8"));
    let provided;
    try {
      provided = C.fromB64(confirm.mac);
    } catch (e) {
      provided = null;
    }
    if (provided == null || !C.constantTimeEquals(provided, expected)) {
      reject("\u7fa4\u7ec4\u5bc6\u7801\u9519\u8bef");
      return null;
    }
    const serverMac = C.toB64(
      C.hmacSha256(pwKey, Buffer.from(`lc-server|${transcript}`, "utf8"))
    );
    wire.sendRaw(new M.NetworkPacket({ type: HS_OK, mac: serverMac }));
    const shared = eph.shared(ephC);
    wire.activate(C.hkdfSha256(Buffer.concat([shared, pwKey]), salt, C.INFO_SESSION, C.KEY_LEN));
    return { mode, groupId: start.groupId };
  },

  async initiateDirect(wire, identity, expectedPeerId, onIdentityMismatch) {
    if (!identity) throw new WireException("\u672c\u673a\u8eab\u4efd\u672a\u521d\u59cb\u5316");
    const eph = C.ECKeyPair.generate();
    const ephA = eph.publicB64;
    const identA = identity.publicB64;
    wire.sendRaw(
      new M.NetworkPacket({ type: HS_START, hsMode: MODE_DIRECT, eph: ephA, ident: identA })
    );
    const ack = await wire.recvRaw();
    if (ack == null) throw new WireException("\u5bf9\u65b9\u65e0\u54cd\u5e94");
    if (ack.type === HS_REJECT) throw new WireException(ack.errorMessage || "\u8fde\u63a5\u88ab\u62d2\u7edd");
    if (ack.type !== HS_ACK || !ack.eph || !ack.ident || !ack.sig) {
      throw new WireException("\u65e0\u6548\u7684\u63e1\u624b\u54cd\u5e94");
    }
    const ephB = ack.eph;
    const identB = ack.ident;
    let peerIdentPub;
    try {
      peerIdentPub = C.decodePub(identB);
    } catch (e) {
      throw new WireException("\u5bf9\u65b9\u8eab\u4efd\u5bc6\u94a5\u65e0\u6548");
    }
    const transcriptHash = C.sha256(
      Buffer.from(`lc-direct-v1|${ephA}|${ephB}`, "utf8")
    );
    let theirSig;
    try {
      theirSig = C.fromB64(ack.sig);
    } catch (e) {
      throw new WireException("\u65e0\u6548\u7684\u7b7e\u540d");
    }
    if (!C.verify("sha256", transcriptHash, peerIdentPub, theirSig)) {
      throw new WireException("\u5bf9\u65b9\u8eab\u4efd\u7b7e\u540d\u9a8c\u8bc1\u5931\u8d25");
    }
    if (expectedPeerId && !identity.checkPeer(expectedPeerId, identB)) {
      if (onIdentityMismatch) {
        try {
          onIdentityMismatch();
        } catch (e) {
          /* ignore */
        }
      }
      throw new WireException("\u5bf9\u65b9\u8eab\u4efd\u53d1\u751f\u53d8\u5316\uff0c\u53ef\u80fd\u5b58\u5728\u4e2d\u95f4\u4eba");
    }
    const sessionKey = C.hkdfSha256(eph.shared(ephB), transcriptHash, C.INFO_DIRECT, C.KEY_LEN);
    const mySig = C.toB64(identity.sign(transcriptHash));
    wire.sendRaw(new M.NetworkPacket({ type: HS_CONFIRM, sig: mySig }));
    wire.activate(sessionKey);
    return identB;
  },

  async acceptDirect(wire, identity, start, expectedPeerId, onIdentityMismatch) {
    if (!identity) {
      try {
        wire.sendRawReject("\u5bf9\u65b9\u8eab\u4efd\u65e0\u6548");
      } catch (e) {
        /* ignore */
      }
      return null;
    }
    if (start.hsMode !== MODE_DIRECT || !start.eph || !start.ident) {
      try {
        wire.sendRawReject("\u65e0\u6548\u7684\u63e1\u624b");
      } catch (e) {
        /* ignore */
      }
      return null;
    }
    const eph = C.ECKeyPair.generate();
    const ephA = start.eph;
    const ephB = eph.publicB64;
    const transcriptHash = C.sha256(Buffer.from(`lc-direct-v1|${ephA}|${ephB}`, "utf8"));
    const sig = C.toB64(identity.sign(transcriptHash));
    wire.sendRaw(
      new M.NetworkPacket({ type: HS_ACK, eph: ephB, ident: identity.publicB64, sig })
    );
    const confirm = await wire.recvRaw();
    if (confirm == null || confirm.type !== HS_CONFIRM || !confirm.sig) return null;
    let initiatorIdent;
    try {
      initiatorIdent = C.decodePub(start.ident);
    } catch (e) {
      return null;
    }
    let theirSig;
    try {
      theirSig = C.fromB64(confirm.sig);
    } catch (e) {
      return null;
    }
    if (!C.verify("sha256", transcriptHash, initiatorIdent, theirSig)) return null;
    if (
      expectedPeerId &&
      !identity.checkPeer(expectedPeerId, start.ident, false)
    ) {
      if (onIdentityMismatch) {
        try {
          onIdentityMismatch();
        } catch (e) {
          /* ignore */
        }
      }
      return null;
    }
    const sessionKey = C.hkdfSha256(eph.shared(ephA), transcriptHash, C.INFO_DIRECT, C.KEY_LEN);
    wire.activate(sessionKey);
    return start.ident;
  }
};

module.exports = {
  WireException,
  LineChannel,
  Wire,
  Handshake,
  renderGroupId,
  HS_START,
  HS_ACK,
  HS_CONFIRM,
  HS_OK,
  HS_REJECT,
  MODE_QUERY,
  MODE_JOIN,
  MODE_MESH,
  MODE_DIRECT,
  DIRECT_HELLO,
  DIRECT_ACK,
  DIRECT_PENDING,
  NONCE_CACHE_SIZE,
};
