"use strict";

const fs = require("fs");
const path = require("path");

const C = require("./crypto");

const GROUP_SIGN_DOMAIN = "lc-group-v1";

function sha256HexUpper(b64) {
  return C.sha256(C.fromB64(b64)).toString("hex").slice(0, 16).toUpperCase();
}

class DeviceIdentity {
  constructor(dataDir) {
    this.dataDir = dataDir;
    fs.mkdirSync(dataDir, { recursive: true });
    this.filePath = path.join(dataDir, "identity.json");
    this.pair = null;
    this.peers = {};
    this.deviceId = "";
    this.hardwareId = "";
    this.enforced = new Set();
    this._load();
  }

  _load() {
    let doc = null;
    try {
      doc = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
    } catch (e) {
      doc = null;
    }
    let pair = null;
    if (doc && doc.private && doc.public) {
      try {
        const priv = C.fromB64(doc.private);
        pair = C.ECKeyPair.fromPrivateDer(priv);
        if (pair.publicB64 !== doc.public) pair = null;
      } catch (e) {
        pair = null;
      }
    }
    if (pair == null) {
      if (doc) {
        // not silent: peers that knew the old key will flag a TOFU mismatch
        console.warn(
          `[identity] ${this.filePath} unreadable (corrupt key); generating a NEW identity`
        );
      }
      pair = C.ECKeyPair.generate();
      this.deviceId = doc && doc.deviceId ? String(doc.deviceId) : UuidLike();
      this.hardwareId = doc && doc.hardwareId ? String(doc.hardwareId) : UuidLike();
      this.peers = {};
      this._save();
    } else {
      this.deviceId = String(doc.deviceId || "");
      this.hardwareId = String(doc.hardwareId || "");
      this.peers = Object.assign({}, doc.peers || {});
      if (!this.deviceId) this.deviceId = UuidLike();
      if (!this.hardwareId) this.hardwareId = UuidLike();
    }
    this.pair = pair;
  }

  _save() {
    const doc = {
      scheme: "plain",
      private: this.pair.privateB64,
      public: this.pair.publicB64,
      peers: this.peers,
      deviceId: this.deviceId,
      hardwareId: this.hardwareId,
    };
    const tmp = this.filePath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(doc), "utf8");
    fs.renameSync(tmp, this.filePath);
  }

  get publicB64() {
    return this.pair.publicB64;
  }

  fingerprint() {
    return sha256HexUpper(this.pair.publicB64);
  }

  peerFingerprint(identB64) {
    try {
      return sha256HexUpper(identB64);
    } catch (e) {
      return "????";
    }
  }

  checkPeer(peerId, identB64, remember = true) {
    if (!peerId) return true;
    const known = this.peers[peerId];
    if (known == null) {
      if (remember) {
        this.peers[peerId] = identB64;
        this._save();
      }
      return true;
    }
    return known === identB64;
  }

  hasPeer(peerId) {
    if (!peerId) return false;
    return Object.prototype.hasOwnProperty.call(this.peers, peerId);
  }

  peerIdent(peerId) {
    if (!peerId) return "";
    return this.peers[peerId] || "";
  }

  forgetPeer(peerId) {
    if (!peerId) return;
    if (Object.prototype.hasOwnProperty.call(this.peers, peerId)) {
      delete this.peers[peerId];
      this._save();
    }
  }

  sign(data) {
    return this.pair.sign(data);
  }
}

function UuidLike() {
  return require("./util").uuid();
}

function groupSenderKey(groupId, senderId) {
  return `group|${groupId}|${senderId}`;
}

function part(p) {
  const raw = Buffer.from(String(p), "utf8");
  return `${raw.length}:${String(p)}`;
}

function transcriptHash(parts) {
  const payload = [GROUP_SIGN_DOMAIN, ...parts.map((p) => part(p))].join("|");
  return C.sha256(Buffer.from(payload, "utf8"));
}

function contentDigest(content) {
  return C.sha256(Buffer.from(String(content), "utf8")).toString("hex");
}

function messageParts(groupId, msg) {
  return [
    "msg",
    String(groupId),
    String(msg.senderId),
    String(msg.id),
    String(Math.trunc(msg.timestamp)),
    contentDigest(msg.content),
  ];
}

function messageFieldsParts(groupId, senderId, messageId, timestamp, content) {
  return [
    "msg",
    String(groupId),
    String(senderId),
    String(messageId),
    String(Math.trunc(timestamp)),
    contentDigest(content),
  ];
}

function deleteParts(groupId, senderId, messageId) {
  return ["del", String(groupId), String(senderId), String(messageId)];
}

function groupUpdateParts(groupId, senderId, groupName, announcement) {
  return ["gupd", String(groupId), String(senderId), String(groupName || ""), String(announcement || "")];
}

function kickParts(groupId, senderId, targetId) {
  return ["kick", String(groupId), String(senderId), String(targetId)];
}

function signParts(identity, parts) {
  if (!identity) return ["", ""];
  try {
    return [identity.publicB64, C.toB64(identity.sign(transcriptHash(parts)))];
  } catch (e) {
    return ["", ""];
  }
}

function signMessage(identity, groupId, message) {
  const [pub, sig] = signParts(identity, messageParts(groupId, message));
  if (pub) {
    message.senderPubId = pub;
    message.senderSig = sig;
  }
}

function signPacket(identity, packet, parts) {
  const [pub, sig] = signParts(identity, parts);
  if (pub) {
    packet.senderPubId = pub;
    packet.senderSig = sig;
  }
}

function check(identity, groupId, senderId, pubB64, sigB64, parts) {
  const key = groupSenderKey(groupId, senderId);
  if (!pubB64 || !sigB64) {
    return !identity.enforced.has(key);
  }
  if (!C.verifyB64(pubB64, transcriptHash(parts), sigB64)) {
    return false;
  }
  if (!identity.checkPeer(key, pubB64)) {
    return false;
  }
  identity.enforced.add(key);
  return true;
}

function verifyMessage(identity, groupId, message) {
  return check(
    identity,
    groupId,
    message.senderId,
    message.senderPubId,
    message.senderSig,
    messageParts(groupId, message)
  );
}

function verifyMessageFields(identity, groupId, senderId, messageId, timestamp, content, pubB64, sigB64) {
  if (!pubB64 || !sigB64) return false;
  return check(
    identity,
    groupId,
    senderId,
    pubB64,
    sigB64,
    messageFieldsParts(groupId, senderId, messageId, timestamp, content)
  );
}

function verifyDelete(identity, groupId, senderId, messageId, pubB64, sigB64) {
  return check(
    identity,
    groupId,
    senderId,
    pubB64,
    sigB64,
    deleteParts(groupId, senderId, messageId)
  );
}

function verifyGroupUpdate(identity, groupId, senderId, groupName, announcement, pubB64, sigB64) {
  return check(
    identity,
    groupId,
    senderId,
    pubB64,
    sigB64,
    groupUpdateParts(groupId, senderId, groupName, announcement)
  );
}

function verifyKick(identity, groupId, senderId, targetId, pubB64, sigB64) {
  return check(
    identity,
    groupId,
    senderId,
    pubB64,
    sigB64,
    kickParts(groupId, senderId, targetId)
  );
}

function memberVerified(identity, groupId, senderId) {
  if (!groupId || !senderId) return false;
  return identity.hasPeer(groupSenderKey(groupId, senderId));
}

function memberFingerprint(identity, groupId, senderId) {
  if (!groupId || !senderId) return "";
  const ident = identity.peerIdent(groupSenderKey(groupId, senderId));
  if (!ident) return "";
  return identity.peerFingerprint(ident);
}

module.exports = {
  DeviceIdentity,
  GA: {
    transcriptHash,
    contentDigest,
    messageParts,
    messageFieldsParts,
    deleteParts,
    groupUpdateParts,
    kickParts,
    signParts,
    signMessage,
    signPacket,
    verifyMessage,
    verifyMessageFields,
    verifyDelete,
    verifyGroupUpdate,
    verifyKick,
    memberVerified,
    memberFingerprint,
    groupSenderKey,
  },
  transcriptHash,
  contentDigest,
  messageParts,
  messageFieldsParts,
  deleteParts,
  groupUpdateParts,
  kickParts,
  signParts,
  signMessage,
  signPacket,
  verifyMessage,
  verifyMessageFields,
  verifyDelete,
  verifyGroupUpdate,
  verifyKick,
  memberVerified,
  memberFingerprint,
  groupSenderKey,
};
