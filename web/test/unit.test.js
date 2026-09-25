"use strict";

const test = require("node:test");
const assert = require("node:assert");

const C = require("../server/crypto");
const U = require("../server/util");
const { Store, SecretBox } = require("../server/store");
const { makeTmpDir } = require("./helpers");

test("aes-gcm roundtrip produces nonce||ct||tag layout", () => {
  const key = C.randomBytes(32);
  const plain = Buffer.from("hello localchat", "utf8");
  const blob = C.aesGcmEncrypt(key, plain);
  assert.strictEqual(blob.length, 12 + plain.length + 16);
  assert.deepStrictEqual(C.aesGcmDecrypt(key, blob), plain);
  const tampered = Buffer.from(blob);
  tampered[tampered.length - 1] ^= 1;
  assert.throws(() => C.aesGcmDecrypt(key, tampered));
  assert.throws(() => C.aesGcmDecrypt(key, Buffer.alloc(10)));
});

test("secret box encrypts at rest with enc1: prefix and roundtrips", () => {
  const dir = makeTmpDir("lc-box-");
  const box = new SecretBox(dir);
  const secret = "\u79d8\u5bc6\u6d88\u606f secret";
  const stored = box.protect(secret);
  assert.ok(stored.startsWith("enc1:"));
  assert.notStrictEqual(stored, secret);
  assert.strictEqual(box.unprotect(stored), secret);
  const reloaded = new SecretBox(dir);
  assert.strictEqual(reloaded.unprotect(stored), secret, "key must persist");
  assert.strictEqual(box.unprotect("plain"), "plain");
  assert.strictEqual(box.unprotect("enc1:garbage"), "");
});

test("store persists chats encrypted and reloads decrypted", () => {
  const dir = makeTmpDir("lc-store-");
  const store = new Store(dir);
  store.saveChat("direct:a|b", [
    { id: "m1", content: "\u4f60\u597d", timestamp: 1, senderId: "a", senderName: "A" },
  ]);
  const raw = require("node:fs").readFileSync(store.chatPath("direct:a|b"), "utf8");
  assert.ok(raw.includes("enc1:"), "content must be encrypted at rest");
  assert.ok(!raw.includes("\u4f60\u597d"));
  const loaded = store.loadChatDecrypted("direct:a|b");
  assert.strictEqual(loaded.length, 1);
  assert.strictEqual(loaded[0].content, "\u4f60\u597d");
  store.deleteChat("direct:a|b");
  assert.deepStrictEqual(store.loadChat("direct:a|b"), []);
});

test("sanitizers match cross-end behaviour", () => {
  assert.strictEqual(U.sanitizeFileName("..\\a\\b\\file.txt "), "file.txt");
  assert.strictEqual(U.sanitizeFileName(""), "file");
  assert.strictEqual(Array.from(U.sanitizeEmoji("\u00e9\u4f60\u597d")).length, 3);
  const cps = Array.from(U.sanitizeEmoji("a\u0000b"));
  assert.ok(!cps.includes("\u0000"));
  assert.strictEqual(U.detectMediaKind("clip.MP4"), "video");
  assert.strictEqual(U.detectMediaKind("song.ogg"), "audio");
  assert.strictEqual(U.detectMediaKind("pic.webp"), "image");
  assert.strictEqual(U.detectMediaKind("doc.pdf"), "file");
});

test("content validation counts code points, not utf16 units", () => {
  const astralHeavy = "\ud83d\ude00".repeat(U.MAX_CONTENT_LENGTH);
  assert.strictEqual(U.isValidContent(astralHeavy), true);
  assert.strictEqual(U.isValidContent(astralHeavy + "x"), false);
  assert.strictEqual(U.isValidContent("   "), false);
  assert.strictEqual(U.isValidContent("hi"), true);
});

test("direct conversation keys are order independent", () => {
  assert.strictEqual(U.directKey("b", "a"), U.directKey("a", "b"));
  assert.ok(U.directKey("a", "b").startsWith("direct:"));
});
