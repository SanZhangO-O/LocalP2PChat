"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const { ChatServer } = require("../server/main");
const { makeTmpDir, httpRequest, waitFor, WsClient } = require("./helpers");

const THUMB = "\ud83d\udc4d";

async function startServer(dataDir) {
  const server = new ChatServer({
    data: dataDir,
    publicDir: path.join(__dirname, "..", "public"),
    httpPort: 0,
    httpHost: "127.0.0.1",
  });
  const port = await server.start();
  return { server, port };
}

function cookieOf(res) {
  const set = res.headers["set-cookie"];
  assert.ok(set && set.length, "login must set a session cookie");
  return set[0].split(";")[0];
}

async function register(port, username, password, cookie) {
  return httpRequest(port, "POST", "/api/register", { username, password }, cookie);
}

async function loginWs(port, username, password) {
  const res = await httpRequest(port, "POST", "/api/login", { username, password });
  assert.strictEqual(res.status, 200, `login ${username} must succeed`);
  const ws = await WsClient.connect(port, cookieOf(res));
  await ws.next((doc) => doc.snapshot);
  return ws;
}

// predicates below must inspect the LATEST snapshot: earlier snapshots are
// kept in the client docs and would match "group is gone"-style checks
function latestSnapshot(client) {
  const snaps = client.docs.filter((d) => d.snapshot);
  return snaps.length ? snaps[snaps.length - 1].snapshot : null;
}

test("server chat: accounts, direct chat, receipts, groups, files, persistence", async () => {
  const dataDir = makeTmpDir("lc-e2e-");
  const { server: srv1, port } = await startServer(dataDir);

  try {
    // -- first run + account management ------------------------------------
    const who0 = await httpRequest(port, "GET", "/api/whoami");
    assert.strictEqual(who0.json.firstRun, true);
    const unauthWs = WsClient.connect(port, null);
    await assert.rejects(unauthWs, /401/, "ws without session must be rejected");

    const regA = await register(port, "alice", "password1");
    assert.strictEqual(regA.status, 200);
    const aliceCookie = cookieOf(regA);
    const regB = await register(port, "bob", "password2", aliceCookie);
    assert.strictEqual(regB.status, 200, "logged-in user may create accounts");
    // open registration: the login page offers a sign-up button before login
    const openReg = await register(port, "dave", "password3");
    assert.strictEqual(openReg.status, 200, "anonymous sign-up must work");
    const daveCookie = cookieOf(openReg);
    const whoDave = await httpRequest(port, "GET", "/api/whoami", undefined, daveCookie);
    assert.strictEqual(whoDave.json.username, "dave", "signing up logs the new account in");
    const dupReg = await register(port, "alice", "password9");
    assert.strictEqual(dupReg.status, 400, "duplicate sign-up must be rejected");
    const badLogin = await httpRequest(port, "POST", "/api/login", {
      username: "alice",
      password: "wrong-password",
    });
    assert.strictEqual(badLogin.status, 401);

    const alice = await loginWs(port, "alice", "password1");
    const bob = await loginWs(port, "bob", "password2");

    // -- presence -----------------------------------------------------------
    const presence = await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.users.find((u) => u.username === "alice" && u.online)
    );
    assert.ok(presence, "alice must appear online to bob");

    // -- direct chat ---------------------------------------------------------
    alice.send({ action: "startDirect", username: "bob" });
    const started = await alice.next((doc) => doc.startedDirect);
    assert.ok(started.startedDirect.chatKey.startsWith("direct:"));
    const chatKey = started.startedDirect.chatKey;

    const directed = await bob.next(
      (doc) => doc.snapshot && doc.snapshot.directs.some((d) => d.key === chatKey)
    );
    assert.strictEqual(directed.snapshot.directs[0].name, "alice");

    alice.send({ action: "sendChat", chatKey, content: "hello bob" });
    const got1 = await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.some((m) => m.content === "hello bob")
    );
    const msg1 = got1.snapshot.chats[chatKey].messages.find((m) => m.content === "hello bob");
    assert.strictEqual(msg1.senderName, "alice");
    assert.strictEqual(msg1.read, false);

    bob.send({ action: "sendChat", chatKey, content: "hi alice" });
    await alice.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.some((m) => m.content === "hi alice")
    );

    // typing indicator
    bob.send({ action: "sendTyping", chatKey, active: true });
    const typing = await alice.next(
      (doc) => doc.typing && doc.typing.chatKey === chatKey && doc.typing.active
    );
    assert.strictEqual(typing.typing.senderName, "bob");
    bob.send({ action: "sendTyping", chatKey, active: false });

    // read receipt on open
    bob.send({ action: "openChat", chatKey });
    await alice.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.find((m) => m.id === msg1.id && m.read)
    );

    // edit (author only)
    alice.send({ action: "editMessage", chatKey, messageId: msg1.id, content: "hello bob (edited)" });
    const edited = await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.some(
          (m) => m.id === msg1.id && m.edited && m.content === "hello bob (edited)"
        )
    );
    assert.ok(edited);
    bob.send({ action: "editMessage", chatKey, messageId: msg1.id, content: "forge" });
    const rejected = await bob.next((doc) => doc.error && doc.action === "editMessage");
    assert.match(rejected.error, /\u81ea\u5df1/);

    // reactions + pin
    bob.send({ action: "react", chatKey, messageId: msg1.id, emoji: THUMB, active: true });
    await alice.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.find(
          (m) => m.id === msg1.id && (m.reactions[THUMB] || []).length === 1
        )
    );
    alice.send({ action: "pin", chatKey, messageId: msg1.id, active: true });
    await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.find((m) => m.id === msg1.id && m.pinned)
    );

    // delete removes for both
    bob.send({ action: "sendChat", chatKey, content: "to be removed" });
    const rm = await alice.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.some((m) => m.content === "to be removed")
    );
    const rmId = rm.snapshot.chats[chatKey].messages.find((m) => m.content === "to be removed").id;
    bob.send({ action: "deleteMessage", chatKey, messageId: rmId });
    assert.strictEqual(
      await waitFor(() => {
        const snap = latestSnapshot(alice);
        return snap && snap.chats[chatKey] && !snap.chats[chatKey].messages.some((m) => m.id === rmId);
      }),
      true,
      "deleted message must disappear for both sides"
    );

    // -- nickname propagates -------------------------------------------------
    alice.send({ action: "setNickname", name: "Alice A" });
    await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.users.find((u) => u.username === "alice" && u.name === "Alice A")
    );

    // -- group chat ------------------------------------------------------------
    alice.send({ action: "createGroup", name: "team", members: ["bob"] });
    const created = await alice.next((doc) => doc.createdGroup);
    const groupId = created.createdGroup.groupId;
    await bob.next(
      (doc) => doc.snapshot && doc.snapshot.groups.some((g) => g.groupId === groupId)
    );

    // only the creator may update group info
    bob.send({ action: "groupUpdate", groupId, name: "bob team" });
    await bob.next((doc) => doc.error && doc.action === "groupUpdate");
    alice.send({ action: "groupUpdate", groupId, name: "team2", announcement: "welcome" });
    await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.groups.some((g) => g.groupId === groupId && g.name === "team2")
    );

    bob.send({ action: "sendChat", chatKey: groupId, content: "group hello" });
    await alice.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[groupId] &&
        doc.snapshot.chats[groupId].messages.some((m) => m.content === "group hello")
    );

    // group read receipts accumulate readers
    alice.send({ action: "openChat", chatKey: groupId });
    await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[groupId] &&
        doc.snapshot.chats[groupId].messages.find(
          (m) => m.content === "group hello" && (m.readers || []).length === 1
        )
    );

    // invite + kick
    const regC = await register(port, "carol", "password3", aliceCookie);
    assert.strictEqual(regC.status, 200);
    const carol = await loginWs(port, "carol", "password3");
    alice.send({ action: "inviteMember", groupId, username: "carol" });
    await carol.next(
      (doc) => doc.snapshot && doc.snapshot.groups.some((g) => g.groupId === groupId)
    );
    alice.send({ action: "kickMember", groupId, targetId: carol.docs.find((d) => d.snapshot).snapshot.profile.userId });
    const kickedEvent = await carol.next((doc) => typeof doc.event === "string");
    assert.match(kickedEvent.event, /\u79fb\u51fa/);
    assert.strictEqual(
      await waitFor(() => {
        const snap = latestSnapshot(carol);
        return snap && !snap.groups.some((g) => g.groupId === groupId);
      }),
      true,
      "kicked member must lose the group"
    );

    // -- files ------------------------------------------------------------------
    const payload = Buffer.from("file-bytes-\u00e9\u4f60\u597d", "utf8");
    const up = await httpRequest(
      port,
      "POST",
      `/api/upload?name=${encodeURIComponent("notes.txt")}`,
      payload,
      aliceCookie
    );
    assert.strictEqual(up.status, 200);
    alice.send({ action: "sendFile", chatKey, uploadId: up.json.uploadId });
    const fileSnap = await bob.next(
      (doc) =>
        doc.snapshot &&
        doc.snapshot.chats[chatKey] &&
        doc.snapshot.chats[chatKey].messages.some((m) => m.fileInfo)
    );
    const fileMsg = fileSnap.snapshot.chats[chatKey].messages.find((m) => m.fileInfo);
    assert.strictEqual(fileMsg.fileInfo.fileName, "notes.txt");
    assert.strictEqual(fileMsg.fileInfo.fileSize, payload.length);
    const dl = await httpRequest(port, "GET", `/files/${fileMsg.fileInfo.fileId}`, undefined, aliceCookie);
    assert.strictEqual(dl.status, 200);
    assert.deepStrictEqual(Buffer.from(dl.text, "utf8"), payload);
    const dlAnon = await httpRequest(port, "GET", `/files/${fileMsg.fileInfo.fileId}`);
    assert.strictEqual(dlAnon.status, 401);

    // -- persistence across restart ---------------------------------------------
    alice.close();
    bob.close();
    carol.close();
    srv1.stop();

    const { server: srv2, port: port2 } = await startServer(dataDir);
    try {
      const alice2 = await loginWs(port2, "alice", "password1");
      const snap = (await alice2.next((doc) => doc.snapshot)).snapshot;
      assert.strictEqual(snap.profile.username, "alice");
      assert.strictEqual(snap.profile.name, "Alice A", "nickname persists");
      const direct = snap.chats[chatKey];
      assert.ok(direct, "direct conversation must survive restart");
      const editedMsg = direct.messages.find((m) => m.id === msg1.id);
      assert.strictEqual(editedMsg.content, "hello bob (edited)");
      assert.strictEqual(editedMsg.edited, true);
      assert.deepStrictEqual(editedMsg.reactions[THUMB], [snap.users.find((u) => u.username === "bob").id]);
      assert.strictEqual(editedMsg.pinned, true);
      assert.ok(direct.messages.some((m) => m.fileInfo), "file message must survive restart");
      const group = snap.groups.find((g) => g.groupId === groupId);
      assert.ok(group, "group must survive restart");
      assert.strictEqual(group.announcement, "welcome");
      assert.ok(!snap.chats[groupId].messages.some((m) => m.content.includes("enc1:")));
      assert.ok(snap.chats[groupId].messages.some((m) => m.content === "group hello"));
      alice2.close();
    } finally {
      srv2.stop();
    }

    // last live snapshot sanity: shape carries users/groups/chats
    const aliceSeen = latestSnapshot(alice);
    assert.ok(aliceSeen && Array.isArray(aliceSeen.users));
    assert.ok(aliceSeen.chats[chatKey]);
  } finally {
    srv1.stop();
  }
});

test("group lifecycle: creator leaving dissolves the group for everyone", async () => {
  const dataDir = makeTmpDir("lc-e2e2-");
  const { server: srv, port } = await startServer(dataDir);
  try {
    const regA = await register(port, "alice", "password1");
    const aliceCookie = cookieOf(regA);
    await register(port, "bob", "password2", aliceCookie);
    const alice = await loginWs(port, "alice", "password1");
    const bob = await loginWs(port, "bob", "password2");

    alice.send({ action: "createGroup", name: "temp", members: ["bob"] });
    const created = await alice.next((doc) => doc.createdGroup);
    const groupId = created.createdGroup.groupId;
    await bob.next((doc) => doc.snapshot && doc.snapshot.groups.some((g) => g.groupId === groupId));

    alice.send({ action: "leaveGroup", groupId });
    const dissolveEvent = await bob.next(
      (doc) => typeof doc.event === "string" && doc.event.includes("\u89e3\u6563")
    );
    assert.ok(dissolveEvent);
    assert.strictEqual(
      await waitFor(() => {
        const snap = latestSnapshot(bob);
        return snap && !snap.groups.some((g) => g.groupId === groupId) && !snap.chats[groupId];
      }),
      true,
      "dissolved group must disappear from every member"
    );

    // a non-creator leaving keeps the group
    alice.send({ action: "createGroup", name: "keep", members: ["bob"] });
    // the earlier "createdGroup" reply for the dissolved group is still in the
    // docs, so match the new group by its unique name
    const created2 = await alice.next(
      (doc) => doc.createdGroup && doc.createdGroup.name === "keep"
    );
    await bob.next(
      (doc) => doc.snapshot && doc.snapshot.groups.some((g) => g.groupId === created2.createdGroup.groupId)
    );
    bob.send({ action: "leaveGroup", groupId: created2.createdGroup.groupId });
    assert.strictEqual(
      await waitFor(() => {
        const snap = latestSnapshot(alice);
        const g = snap && snap.groups.find((x) => x.groupId === created2.createdGroup.groupId);
        return g && g.members.length === 1;
      }),
      true,
      "group must survive a non-creator leaving"
    );
    alice.close();
    bob.close();
  } finally {
    srv.stop();
  }
});

test("multiple accounts per machine: additive register and token switching", async () => {
  const dataDir = makeTmpDir("lc-e2e3-");
  const { server: srv, port } = await startServer(dataDir);
  try {
    const regA = await register(port, "alice", "password1");
    assert.strictEqual(regA.status, 200);
    const aliceCookie = cookieOf(regA);
    const tokenA = regA.json.token;
    assert.ok(tokenA, "login responses must carry a switchable token");

    // the first account is the cookie session; both auth paths agree
    const whoCookie = await httpRequest(port, "GET", "/api/whoami", undefined, aliceCookie);
    assert.strictEqual(whoCookie.json.username, "alice");
    const whoToken = await httpRequest(port, "GET", "/api/whoami", undefined, undefined, tokenA);
    assert.strictEqual(whoToken.json.username, "alice");

    // registering a second account must NOT kick the current session
    const regB = await register(port, "bob", "password2", aliceCookie);
    assert.strictEqual(regB.status, 200);
    const tokenB = regB.json.token;
    const stillAlice = await httpRequest(port, "GET", "/api/whoami", undefined, aliceCookie);
    assert.strictEqual(stillAlice.json.username, "alice", "register must keep the current session");
    const asBob = await httpRequest(port, "GET", "/api/whoami", undefined, undefined, tokenB);
    assert.strictEqual(asBob.json.username, "bob");

    // a switched token also satisfies the register gate
    const regC = await httpRequest(
      port,
      "POST",
      "/api/register",
      { username: "carol", password: "password3" },
      undefined,
      tokenB
    );
    assert.strictEqual(regC.status, 200);

    // ws as the switched account via query token
    const bobWs = await WsClient.connect(port, { token: tokenB });
    const snap = await bobWs.next((doc) => doc.snapshot);
    assert.strictEqual(snap.snapshot.profile.username, "bob");
    bobWs.close();

    // uploads work over the token path (no cookie involved)
    const payload = Buffer.from("multi-account-file", "utf8");
    const up = await httpRequest(
      port,
      "POST",
      `/api/upload?name=${encodeURIComponent("m.txt")}`,
      payload,
      undefined,
      tokenB
    );
    assert.strictEqual(up.status, 200);

    // removing (logging out) the switched account leaves the cookie session intact
    const out = await httpRequest(port, "POST", "/api/logout", undefined, undefined, tokenB);
    assert.strictEqual(out.status, 200);
    const bobGone = await httpRequest(port, "GET", "/api/whoami", undefined, undefined, tokenB);
    assert.strictEqual(bobGone.json.authenticated, false);
    const aliceStays = await httpRequest(port, "GET", "/api/whoami", undefined, aliceCookie);
    assert.strictEqual(aliceStays.json.username, "alice");

    // re-login mints a fresh working token
    const relogin = await httpRequest(port, "POST", "/api/login", { username: "bob", password: "password2" });
    assert.strictEqual(relogin.status, 200);
    const tokenB2 = relogin.json.token;
    const bobBack = await httpRequest(port, "GET", "/api/whoami", undefined, undefined, tokenB2);
    assert.strictEqual(bobBack.json.username, "bob");

    // open registration is rate limited per IP (every attempt counts)
    let limited = false;
    for (let i = 0; i < 30 && !limited; i++) {
      const r = await register(port, `spam${i}`, "password1");
      if (r.status === 429) limited = true;
    }
    assert.strictEqual(limited, true, "register rate limit must trigger");
  } finally {
    srv.stop();
  }
});
