"use strict";

const crypto = require("crypto");

const GCM_NONCE_LEN = 12;
const GCM_TAG_LEN = 16;
const KEY_LEN = 32;

function randomBytes(n) {
  return crypto.randomBytes(n);
}

// layout: nonce(12B) || ciphertext || tag(16B); the tag comes from
// cipher.getAuthTag() (Node has no Cipheriv.getTag)
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

module.exports = {
  GCM_NONCE_LEN,
  GCM_TAG_LEN,
  KEY_LEN,
  randomBytes,
  aesGcmEncrypt,
  aesGcmDecrypt,
  toB64,
  fromB64,
};
