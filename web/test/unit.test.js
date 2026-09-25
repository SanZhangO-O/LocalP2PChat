"use strict";

const test = require("node:test");
const assert = require("node:assert");

const C = require("../server/crypto");
const M = require("../server/models");
const U = require("../server/util");
const GA = require("../server/identity");

test("sha256 known vector", () => {
  assert.strictEqual(
    C.sha256(Buffer.from("abc", "utf8")).toString("hex"),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
  );
});

test("hmac-sha256 known vector (RFC 4231 case 2)", () => {
  const key = Buffer.from("Jefe", "utf8");
  const data = Buffer.from(
    "what do ya want for nothing?",
    "utf8"
  );
  assert.strictEqual(
    C.hmacSha256(key, data).toString("hex"),
    "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
  );
});

test("hkdf-sha256 known vector (RFC 5869 test case 1)", () => {
  const ikm = Buffer.alloc(22, 0x0b);
  const salt = Buffer.from("000102030405060708090a0b0c", "hex");
  const info = Buffer.from("f0f1f2f3f4f5f6f7f8f9", "hex");
  const okm = C.hkdfSha256(ikm, salt, info, 42);
  assert.strictEqual(
    okm.toString("hex"),
    "3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865"
  );
});

test("aes-gcm roundtrip produces nonce||ct||tag layout", () => {
  const key = C.randomBytes(32);
  const plain = Buffer.from("hello localchat", "utf8");
  const blob = C.aesGcmEncrypt(key, plain);
  assert.strictEqual(blob.length, 12 + plain.length + 16);
  assert.deepStrictEqual(C.aesGcmDecrypt(key, blob), plain);
  assert.throws(() => C.aesGcmDecrypt(key, Buffer.from(blob)), /.*$/);
  const tampered = Buffer.from(blob);
  tampered[tampered.length - 1] ^= 1;
  assert.throws(() => C.aesGcmDecrypt(key, tampered));
});

test("ec key pair: spki/pkcs8 der encodings, sign/verify, ecdh", () => {
  const a = C.ECKeyPair.generate();
  const b = C.ECKeyPair.generate();
  const data = Buffer.from("transcript", "utf8");
  const sig = a.sign(data);
  assert.strictEqual(C.verify("sha256", data, a.publicKey, sig), true);
  assert.strictEqual(C.verify("sha256", Buffer.from("other"), a.publicKey, sig), false);
  const sharedA = a.shared(b.publicB64);
  const sharedB = b.shared(a.publicB64);
  assert.strictEqual(sharedA.length >= 1 && sharedA.length <= 32, true);
  assert.deepStrictEqual(sharedA, sharedB);
  const restored = C.ECKeyPair.fromPrivateDer(C.fromB64(a.privateB64));
  assert.strictEqual(restored.publicB64, a.publicB64);
});

test("pbkdf2-sha1 vector", async () => {
  const key = await C.pbkdf2Sha1("password", Buffer.from("salt", "utf8"), 4096, 32);
  assert.strictEqual(
    key.toString("hex"),
    "4b007901b765489abead49d926f721d065a429c1e7164ba4adfd58c12b0a17fe"
  );
});

test("numeric group id is stable and 8 digits", () => {
  const id1 = U.numericGroupIdOf("\u6d4b\u8bd5\u7fa4", "fp-123");
  const id2 = U.numericGroupIdOf("\u6d4b\u8bd5\u7fa4", "fp-123");
  const other = U.numericGroupIdOf("\u6d4b\u8bd5\u7fa4", "fp-456");
  assert.strictEqual(id1, id2);
  assert.notStrictEqual(id1, other);
  assert.strictEqual(id1.length, 8);
  assert.ok(/^[0-9]{8}$/.test(id1));
});

test("group signing transcripts match the python/kotlin format", () => {
  const parts = GA.messageFieldsParts("g1", "s1", "m1", 1700000000000, "hello");
  assert.deepStrictEqual(parts, [
    "msg",
    "g1",
    "s1",
    "m1",
    "1700000000000",
    C.sha256(Buffer.from("hello", "utf8")).toString("hex"),
  ]);
  const hash1 = GA.transcriptHash(parts);
  const part = (p) => `${Buffer.byteLength(p)}:${p}`;
  const payload =
    "lc-group-v1|" +
    ["msg", "g1", "s1", "m1", "1700000000000", parts[5]].map(part).join("|");
  assert.deepStrictEqual(hash1, C.sha256(Buffer.from(payload, "utf8")));
  const del = GA.deleteParts("g1", "s1", "m1");
  assert.deepStrictEqual(del, ["del", "g1", "s1", "m1"]);
});

test("chat message wire roundtrip omits defaults like kotlinx", () => {
  const msg = new M.ChatMessage({
    id: "m-1",
    content: "hi",
    timestamp: 123,
    senderId: "s",
    senderName: "n",
  });
  assert.deepStrictEqual(msg.toDict(), {
    id: "m-1",
    content: "hi",
    timestamp: 123,
    senderId: "s",
    senderName: "n",
  });
  const rich = new M.ChatMessage({
    id: "m-2",
    content: "hello",
    timestamp: 456,
    senderId: "s2",
    senderName: "n2",
    fileInfo: new M.FileInfo("f-1", "img.png", 10, "1.2.3.4", 1000, "KEY", "image"),
    replyTo: "m-1",
    replyPreview: "hi",
    replySender: "n",
    quoteSender: "s",
    mentions: ["all"],
    edited: true,
  });
  const dict = rich.toDict();
  assert.strictEqual(dict.fileInfo.kind, "image");
  assert.strictEqual(dict.quote.quotedId, "m-1");
  assert.strictEqual(dict.quote.quotedSender, "s");
  assert.ok(!("quotedKind" in dict.quote));
  assert.deepStrictEqual(dict.mentions, ["all"]);
  assert.strictEqual(dict.edited, true);
  const back = M.ChatMessage.fromDict(JSON.parse(JSON.stringify(dict)));
  assert.strictEqual(back.replyTo, "m-1");
  assert.strictEqual(back.quoteSender, "s");
  assert.strictEqual(back.fileInfo.kind, "image");
  assert.strictEqual(back.edited, true);
});

test("packet parse rejects malformed required fields", () => {
  assert.throws(() => M.NetworkPacket.fromDict({}));
  assert.throws(() => M.NetworkPacket.fromDict({ type: "chat" }));
  assert.throws(() => M.NetworkPacket.fromDict({ type: "error" }));
  assert.throws(() =>
    M.NetworkPacket.fromDict({ type: "read_receipt", upToId: "x" })
  );
  assert.throws(() =>
    M.NetworkPacket.fromDict({ type: "typing", senderId: "s", active: "true" })
  );
  assert.throws(() =>
    M.NetworkPacket.fromDict({ type: "file_download", fileId: "f" })
  );
  const ok = M.NetworkPacket.fromDict({
    type: "file_download",
    fileId: "f",
    token: "t",
    offset: "128",
  });
  assert.strictEqual(ok.offset, 128);
});

test("chat message requires id and senderId; edited must be boolean", () => {
  assert.throws(() => M.ChatMessage.fromDict({ id: "x", timestamp: 1, senderName: "n" }));
  assert.throws(() =>
    M.ChatMessage.fromDict({
      id: "x",
      content: "c",
      timestamp: 1,
      senderId: "s",
      senderName: "n",
      edited: "true",
    })
  );
});

test("sanitizers match python behaviour", () => {
  assert.strictEqual(U.sanitizeFileName("..\\a\\b\\file.txt "), "file.txt");
  assert.strictEqual(U.sanitizeFileName(""), "file");
  assert.strictEqual(U.sanitizeRelativePath("a\\..\\b/c.txt"), "a/b/c.txt");
  assert.strictEqual(Array.from(U.sanitizeEmoji("\u00e9\u4f60\u597d")).length, 3);
  const cps = Array.from(U.sanitizeEmoji("a\u0000b"));
  assert.ok(!cps.includes("\u0000"));
  assert.strictEqual(U.makeQuotePreview("x".repeat(130)).length, 121);
  assert.ok(U.makeQuotePreview("x".repeat(130)).endsWith("\u2026"));
  assert.deepStrictEqual(U.sanitizeDeletedIds(["a", "a", "", "b".repeat(200), "c"]), [
    "a",
    "c",
  ]);
  assert.strictEqual(U.detectMediaKind("clip.MP4"), "video");
  assert.strictEqual(U.detectMediaKind("song.ogg"), "audio");
  assert.strictEqual(U.detectMediaKind("pic.webp"), "image");
  assert.strictEqual(U.detectMediaKind("doc.pdf"), "file");
  assert.strictEqual(U.normalizeMediaKind("future"), "file");
});

test("file download token is deterministic base64 hmac", () => {
  const key = Buffer.alloc(32, 7);
  const t1 = require("../server/files").fileDownloadToken(key, "file-1");
  const t2 = require("../server/files").fileDownloadToken(key, "file-1");
  assert.strictEqual(t1, t2);
  assert.strictEqual(typeof t1, "string");
});

test("identity tofu store persists and enforces", () => {
  const { makeTmpDir } = require("./helpers");
  const dir = makeTmpDir("lc-ident-");
  const { DeviceIdentity } = require("../server/identity");
  const id1 = new DeviceIdentity(dir);
  const fp = id1.fingerprint();
  assert.strictEqual(fp.length, 16);
  assert.ok(/^[0-9A-F]{16}$/.test(fp));
  assert.strictEqual(id1.checkPeer("p1", "KEY-A"), true);
  const id2 = new DeviceIdentity(dir);
  assert.strictEqual(id2.fingerprint(), fp, "identity must persist across loads");
  assert.strictEqual(id2.checkPeer("p1", "KEY-A", false), true);
  assert.strictEqual(id2.checkPeer("p1", "KEY-B", false), false);
});
