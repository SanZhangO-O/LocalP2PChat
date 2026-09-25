"use strict";

const crypto = require("crypto");

const GCM_NONCE_LEN = 12;
const GCM_TAG_LEN = 16;
const KEY_LEN = 32;
const PBKDF2_ITERATIONS = 210000;
const INFO_SESSION = Buffer.from("localchat-session-v1", "utf8");
const INFO_DIRECT = Buffer.from("lc-direct-v1", "utf8");

function sha256(data) {
  return crypto.createHash("sha256").update(data).digest();
}

function hmacSha256(key, data) {
  return crypto.createHmac("sha256", key).update(data).digest();
}

function hkdfSha256(ikm, salt, info, outLen) {
  const okm = crypto.hkdfSync("sha256", ikm, salt, info, outLen);
  return Buffer.from(okm);
}

function pbkdf2Sha1(password, salt, iterations, outLen) {
  return new Promise((resolve, reject) => {
    crypto.pbkdf2(Buffer.from(password, "utf8"), salt, iterations, outLen, "sha1", (err, key) => {
      if (err) reject(err);
      else resolve(key);
    });
  });
}

function constantTimeEquals(a, b) {
  if (!Buffer.isBuffer(a)) a = Buffer.from(a);
  if (!Buffer.isBuffer(b)) b = Buffer.from(b);
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

function randomBytes(n) {
  return crypto.randomBytes(n);
}

function aesGcmEncrypt(key, plaintext) {
  const nonce = crypto.randomBytes(GCM_NONCE_LEN);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, nonce, {
    authTagLength: GCM_TAG_LEN,
  });
  const ct = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  return Buffer.concat([nonce, ct, tag]);
}

function aesGcmDecrypt(key, blob) {
  if (!Buffer.isBuffer(blob) || blob.length <= GCM_NONCE_LEN + GCM_TAG_LEN) {
    throw new Error("ciphertext too short");
  }
  const nonce = blob.subarray(0, GCM_NONCE_LEN);
  const tag = blob.subarray(blob.length - GCM_TAG_LEN);
  const ct = blob.subarray(GCM_NONCE_LEN, blob.length - GCM_TAG_LEN);
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, nonce);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ct), decipher.final()]);
}

function toB64(data) {
  return Buffer.from(data).toString("base64");
}

function fromB64(s) {
  return Buffer.from(String(s).replace(/\s+/g, ""), "base64");
}

function hexStr(data) {
  return Buffer.from(data).toString("hex");
}

function canonicalSharedSecret(raw) {
  let start = 0;
  while (start < raw.length - 1 && raw[start] === 0) start += 1;
  return raw.subarray(start);
}

class ECKeyPair {
  constructor(privateKeyObject) {
    this.privateKey = privateKeyObject;
    this.publicKey = crypto.createPublicKey(privateKeyObject);
  }

  static generate() {
    const { privateKey } = crypto.generateKeyPairSync("ec", { namedCurve: "prime256v1" });
    return new ECKeyPair(privateKey);
  }

  static fromPrivateDer(der) {
    return new ECKeyPair(
      crypto.createPrivateKey({ key: der, format: "der", type: "pkcs8" })
    );
  }

  static publicFromB64(b64) {
    return crypto.createPublicKey({ key: fromB64(b64), format: "der", type: "spki" });
  }

  get publicB64() {
    return this.publicKey.export({ format: "der", type: "spki" }).toString("base64");
  }

  get privateB64() {
    return this.privateKey.export({ format: "der", type: "pkcs8" }).toString("base64");
  }

  shared(peerPublicB64) {
    const peer = ECKeyPair.publicFromB64(peerPublicB64);
    const raw = crypto.diffieHellman({ privateKey: this.privateKey, publicKey: peer });
    return canonicalSharedSecret(raw);
  }

  sign(data) {
    return crypto.sign("sha256", data, this.privateKey);
  }
}

function decodePub(b64) {
  return ECKeyPair.publicFromB64(b64);
}

function decodePriv(b64) {
  return ECKeyPair.fromPrivateDer(fromB64(b64));
}

function verifyB64(publicB64, data, sigB64) {
  try {
    const pub = decodePub(publicB64);
    return crypto.verify("sha256", data, pub, fromB64(sigB64));
  } catch (e) {
    return false;
  }
}

function verify(algorithm, data, publicKeyObject, sig) {
  try {
    return crypto.verify(algorithm, data, publicKeyObject, sig);
  } catch (e) {
    return false;
  }
}

module.exports = {
  GCM_NONCE_LEN,
  GCM_TAG_LEN,
  KEY_LEN,
  PBKDF2_ITERATIONS,
  INFO_SESSION,
  INFO_DIRECT,
  sha256,
  hmacSha256,
  hkdfSha256,
  pbkdf2Sha1,
  constantTimeEquals,
  randomBytes,
  aesGcmEncrypt,
  aesGcmDecrypt,
  toB64,
  fromB64,
  hexStr,
  ECKeyPair,
  decodePub,
  decodePriv,
  verifyB64,
  verify,
};
