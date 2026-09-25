"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { AccountRegistry, hashPassword, verifyPassword, parseArgs } = require("../server/main");
const { makeTmpDir } = require("./helpers");

test("scrypt password hash/verify roundtrip and rejection", () => {
  const salt = "aabbccdd00112233";
  const hash = hashPassword("\u5bc6\u7801abc123", salt);
  assert.strictEqual(hash.length, 128);
  assert.strictEqual(verifyPassword("\u5bc6\u7801abc123", salt, hash), true);
  assert.strictEqual(verifyPassword("wrong", salt, hash), false);
  assert.strictEqual(verifyPassword("\u5bc6\u7801abc123", "00", hash), false);
});

test("registry: create, unique usernames, port allocation", () => {
  const dir = makeTmpDir("lc-registry-");
  const registry = new AccountRegistry(dir, 9999);
  assert.strictEqual(registry.firstRun, true);
  const first = registry.create("alice", "password1");
  assert.strictEqual(first.ok, true);
  assert.strictEqual(first.record.tcpPort, 9999);
  assert.strictEqual(registry.firstRun, false);
  const dup = registry.create("Alice", "password2");
  assert.strictEqual(dup.ok, false, "case-insensitive duplicate must be rejected");
  const badName = registry.create("a".repeat(40), "password2");
  assert.strictEqual(badName.ok, false);
  const badPass = registry.create("bob", "123");
  assert.strictEqual(badPass.ok, false);
  const second = registry.create("bob", "password2");
  assert.strictEqual(second.ok, true);
  assert.strictEqual(second.record.tcpPort, 10000, "ports must be allocated per account");
  assert.strictEqual(registry.verify("alice", "password1").id, first.record.id);
  assert.strictEqual(registry.verify("alice", "nope"), null);
  assert.strictEqual(registry.verify("carol", "password1"), null);
});

test("registry persists accounts and sessions across instances", () => {
  const dir = makeTmpDir("lc-registry2-");
  const a = new AccountRegistry(dir, 9999);
  const rec = a.create("carol", "password9").record;
  const token = a.createSession(rec.id);
  assert.strictEqual(a.sessionAccount(token).id, rec.id);
  a.dropSession(token);
  assert.strictEqual(a.sessionAccount(token), null);

  const b = new AccountRegistry(dir, 9999);
  assert.strictEqual(b.accounts.size, 1);
  assert.strictEqual(b.verify("carol", "password9").id, rec.id);
  assert.strictEqual(b.sessionAccount(token), null, "sessions must not survive a restart");
});

test("registry file layout stays inside the data dir", () => {
  const dir = makeTmpDir("lc-registry3-");
  new AccountRegistry(dir, 9999).create("dave", "password8");
  assert.ok(fs.existsSync(path.join(dir, "accounts.json")));
});

test("parseArgs reads server flags", () => {
  const args = parseArgs(["node", "main.js", "--port-base", "20000", "--http", "9000", "--http-host", "0.0.0.0"]);
  assert.strictEqual(args.portBase, 20000);
  assert.strictEqual(args.httpPort, 9000);
  assert.strictEqual(args.httpHost, "0.0.0.0");
  assert.strictEqual(args.portBase !== undefined, true);
});
