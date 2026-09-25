"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const net = require("node:net");
const path = require("node:path");

const C = require("../server/crypto");
const M = require("../server/models");
const U = require("../server/util");
const W = require("../server/wire");
const { DeviceIdentity, GA } = require("../server/identity");
const { GroupApp } = require("../server/group");
const { DirectChatManager } = require("../server/direct");
const { SharedListener } = require("../server/tcpserver");
const { OutgoingFileServer, downloadFileOffer } = require("../server/files");
const { PyDriver, freePort, makeTmpDir, waitFor } = require("./helpers");

function sha256File(filePath) {
  return C.sha256(fs.readFileSync(filePath)).toString("hex");
}

test("password handshake + encrypted packets: node client vs python server", async () => {
  const port = await freePort();
  const driver = new PyDriver(["password_server", String(port), "pw12345"]);
  try {
    const listening = await driver.wait("listening");
    const sock = await new Promise((resolve, reject) => {
      const s = net.connect(listening.port, "127.0.0.1", () => resolve(s));
      s.on("error", reject);
    });
    const channel = new W.LineChannel(sock);
    const wire = new W.Wire(channel);
    await W.Handshake.initiate(wire, W.MODE_JOIN, "group-1", "pw12345");
    const msg = new M.ChatMessage({
      id: "m-1",
      content: "hello-from-node",
      timestamp: U.nowMs(),
      senderId: "node-sender-1",
      senderName: "Node",
    });
    wire.sendPacket(new M.NetworkPacket({ type: "chat", message: msg }));
    const echo = await wire.recvPacket(15000);
    assert.strictEqual(echo.type, "chat");
    assert.strictEqual(echo.message.content, "hello-from-node");
    wire.sendPacket(new M.NetworkPacket({ type: "quit" }));
    sock.destroy();
    await driver.wait("secured");
  } finally {
    driver.stop();
  }
});

test("wrong password is rejected with the wire error message", async () => {
  const port = await freePort();
  const driver = new PyDriver(["wrong_password_server", String(port), "rightpw"]);
  try {
    const listening = await driver.wait("listening");
    const sock = await new Promise((resolve, reject) => {
      const s = net.connect(listening.port, "127.0.0.1", () => resolve(s));
      s.on("error", reject);
    });
    const channel = new W.LineChannel(sock);
    const wire = new W.Wire(channel);
    await assert.rejects(
      () => W.Handshake.initiate(wire, W.MODE_QUERY, "group-1", "wrongpw"),
      (err) => err instanceof W.WireException
    );
    sock.destroy();
    await driver.wait("rejected");
  } finally {
    driver.stop();
  }
});

test("direct handshake: node dials python, hello/ack, chat + read receipt, seq enforced", async () => {
  const port = await freePort();
  const dataDir = makeTmpDir("lc-web-direct-");
  const identity = new DeviceIdentity(dataDir);
  identity.deviceId = "node-device-1";
  const driver = new PyDriver(["direct_server", String(port), "basic"]);
  try {
    const listening = await driver.wait("listening");
    const sock = await new Promise((resolve, reject) => {
      const s = net.connect(listening.port, "127.0.0.1", () => resolve(s));
      s.on("error", reject);
    });
    const channel = new W.LineChannel(sock);
    const wire = new W.Wire(channel);
    await W.Handshake.initiateDirect(wire, identity, "py-device-1", null);
    assert.strictEqual(
      identity.peers["py-device-1"],
      listening.ident,
      "node must TOFU-remember the python identity"
    );
    wire.sendPacket(
      new M.NetworkPacket({
        type: W.DIRECT_HELLO,
        peer: new M.Peer("node-device-1", "Web Node", "127.0.0.1", 12345),
      })
    );
    const ack = await wire.recvPacket(15000);
    assert.strictEqual(ack.type, W.DIRECT_ACK);
    assert.strictEqual(ack.peer.id, "py-device-1");
    const chat = new M.ChatMessage({
      id: "dc-1",
      content: "direct hello",
      timestamp: U.nowMs(),
      senderId: "node-device-1",
      senderName: "Web Node",
    });
    wire.sendPacket(new M.NetworkPacket({ type: "chat", message: chat }));
    const receipt = await wire.recvPacket(15000);
    assert.strictEqual(receipt.type, "read_receipt");
    assert.strictEqual(receipt.upToId, "dc-1");
    assert.strictEqual(receipt.readerId, "py-device-1");
    await driver.wait("packet", (l) => l.type === "chat");
    sock.destroy();
  } finally {
    driver.stop();
  }
});

test("direct TOFU: same claimed id with a changed key must be refused", async () => {
  const port = await freePort();
  const dataDir = makeTmpDir("lc-web-tofu-");
  const driver = new PyDriver(["direct_server", String(port), "tofu"]);
  try {
    const listening = await driver.wait("listening");

    async function dial(identity) {
      const sock = await new Promise((resolve, reject) => {
        const s = net.connect(listening.port, "127.0.0.1", () => resolve(s));
        s.on("error", reject);
      });
      const channel = new W.LineChannel(sock);
      const wire = new W.Wire(channel);
      return { sock, channel, wire };
    }

    const identityA = new DeviceIdentity(path.join(dataDir, "a"));
    identityA.deviceId = "node-device-1";
    const a = await dial(identityA);
    await W.Handshake.initiateDirect(a.wire, identityA, null, null);
    a.wire.sendPacket(
      new M.NetworkPacket({
        type: W.DIRECT_HELLO,
        peer: new M.Peer("node-device-1", "Web Node", "127.0.0.1", 1),
      })
    );
    const ackA = await a.wire.recvPacket(15000);
    assert.strictEqual(ackA.type, W.DIRECT_ACK);
    a.sock.destroy();
    await driver.wait("bound");

    const identityB = new DeviceIdentity(path.join(dataDir, "b"));
    identityB.deviceId = "node-device-1";
    assert.notStrictEqual(identityB.publicB64, identityA.publicB64);
    const b = await dial(identityB);
    await W.Handshake.initiateDirect(b.wire, identityB, null, null);
    b.wire.sendPacket(
      new M.NetworkPacket({
        type: W.DIRECT_HELLO,
        peer: new M.Peer("node-device-1", "Web Node", "127.0.0.1", 1),
      })
    );
    await driver.wait("tofu_rejected");
    b.sock.destroy();
  } finally {
    driver.stop();
  }
});

test("node member joins a python-hosted group and both relay chat", async () => {
  const dataDir = makeTmpDir("lc-web-member-");
  const identity = new DeviceIdentity(dataDir);
  identity.deviceId = "node-device-2";
  const driver = new PyDriver(["group_host"]);
  try {
    const hostInfo = await driver.wait("host");
    const gapp = new GroupApp(identity, await freePort());
    gapp.setProfile("Web Node");
    const result = await gapp.joinGroup("127.0.0.1", hostInfo.port, hostInfo.joinId, "pw12345");
    assert.strictEqual(result.ok, true, `join failed: ${result.message}`);
    const g = result.group;
    assert.strictEqual(g.name, "\u6d4b\u8bd5\u7fa4");
    const received = new Promise((resolve) => {
      gapp.events.messagesChanged = (state) => {
        if (state.messages.some((m) => m.senderId !== identity.deviceId)) resolve(state);
      };
    });
    gapp.sendChatMessage(g, "hello group from node");
    const state = await received;
    const fromHost = state.messages.find((m) => m.senderId !== identity.deviceId);
    assert.ok(fromHost, "host chat must arrive at the node member");
    assert.strictEqual(fromHost.senderName, "PyHost");
    assert.strictEqual(fromHost.content, "\u6765\u81ea Python \u4e3b\u673a\u7684\u6d88\u606f");
    await driver.wait("done", undefined, 15000);
    gapp.shutdown();
  } finally {
    driver.stop();
  }
});

test("python member joins a node-hosted group and both relay chat", async () => {
  const dataDir = makeTmpDir("lc-web-host-");
  const identity = new DeviceIdentity(dataDir);
  const gapp = new GroupApp(identity, await freePort());
  gapp.setProfile("Web Node");
  const g = gapp.createHost("\u7f51\u7edc\u7fa4", "pw12345");
  assert.strictEqual(g.joinId.length, 8);
  const direct = new DirectChatManager(identity);
  const listener = new SharedListener(gapp.port, identity, gapp, direct);
  await listener.start();
  const driver = new PyDriver(["group_member", String(gapp.port), g.joinId]);
  try {
    const fromMember = new Promise((resolve) => {
      gapp.events.messagesChanged = (state) => {
        const m = state.messages.find(
          (x) => x.senderId !== identity.deviceId && x.content === "\u6210\u5458\u6d88\u606f\u6765\u81ea Python"
        );
        if (m) resolve(m);
      };
    });
    await driver.wait("joined", undefined, 20000);
    await waitFor(() => g.clients.size > 0);
    assert.ok(g.clients.size > 0, "python member must appear in the host roster");
    gapp.sendChatMessage(g, "welcome broadcast");
    const msg = await fromMember;
    assert.ok(msg, "member chat must reach the node host");
    assert.ok(msg.senderSig, "node-hosted group messages must be signed");
    await driver.wait("done", undefined, 15000);
  } finally {
    driver.stop();
    listener.stop();
    gapp.shutdown();
    direct.shutdown();
  }
});

test("mesh link: node and python exchange mesh_chat and history_reply", async () => {
  const dataDir = makeTmpDir("lc-web-mesh-");
  const identity = new DeviceIdentity(dataDir);
  identity.deviceId = "node-mesh-1";
  const port = await freePort();
  const driver = new PyDriver(["mesh_peer", String(port), "mesh-group-1"]);
  try {
    const listening = await driver.wait("listening");
    const gapp = new GroupApp(identity, await freePort());
    gapp.setProfile("Web Node");
    const g = gapp.registerMemberGroup({
      groupId: "mesh-group-1",
      name: "Mesh",
      joinId: "mesh-group-1",
      password: "meshpw",
    });
    const gotHistory = new Promise((resolve) => {
      gapp.events.messagesChanged = (state) => {
        if (state.messages.some((m) => m.id === "py-hist-1")) resolve(state);
      };
    });
    gapp.meshAddPeer(g, new M.Peer("py-mesh-1", "PyMesh", "127.0.0.1", listening.port));
    const state = await gotHistory;
    const hist = state.messages.find((m) => m.id === "py-hist-1");
    assert.strictEqual(hist.content, "\u7f51\u72b6\u5386\u53f2\u6d88\u606f");
    const gotLive = new Promise((resolve) => {
      gapp.events.messagesChanged = (s2) => {
        const m = s2.messages.find(
          (x) => x.senderId === "py-mesh-1" && x.content === "mesh hello from node"
        );
        if (m) resolve(m);
      };
    });
    const live = new M.ChatMessage({
      id: "node-mesh-msg-1",
      content: "mesh hello from node",
      timestamp: U.nowMs(),
      senderId: identity.deviceId,
      senderName: "Web Node",
    });
    GA.signMessage(identity, "mesh-group-1", live);
    gapp.meshBroadcastMessage(g, live);
    await gotLive;
    await driver.wait("done", undefined, 15000);
    gapp.shutdown();
  } finally {
    driver.stop();
  }
});

test("node downloads a file served by python (per-file-key GCM frames)", async () => {
  const port = await freePort();
  const tmpDir = makeTmpDir("lc-web-dl-");
  const driver = new PyDriver(["file_server", String(port)], { env: { LC_TMPDIR: tmpDir } });
  try {
    const info = await driver.wait("listening");
    const fileInfo = new M.FileInfo(
      info.fileId,
      "py_served.bin",
      info.size,
      "127.0.0.1",
      info.port,
      Buffer.from(info.key, "hex").toString("base64")
    );
    const target = path.join(tmpDir, "node_downloaded.bin");
    const result = await downloadFileOffer(fileInfo, target, null);
    assert.strictEqual(result.ok, true, `download failed: ${result.message}`);
    assert.strictEqual(sha256File(target), info.sha);
    await driver.wait("done", undefined, 15000);
  } finally {
    driver.stop();
  }
});

test("python downloads a file served by node", async () => {
  const tmpDir = makeTmpDir("lc-web-dl2-");
  const original = path.join(tmpDir, "node_served.bin");
  const payload = Buffer.alloc(300 * 1024 + 54321);
  for (let i = 0; i < payload.length; i += 4096) {
    payload[i] = i % 251;
  }
  fs.writeFileSync(original, payload);
  const fileKey = C.sha256(Buffer.from("file-key-seed", "utf8"));
  const fileServer = new OutgoingFileServer("node-file-1", original, payload.length, fileKey);
  const bound = await fileServer.start();
  const driver = new PyDriver(["file_client", String(bound), tmpDir]);
  try {
    driver.writeStdin({ key: fileKey.toString("hex") });
    const done = await driver.wait("done", undefined, 30000);
    assert.strictEqual(done.sha256, C.sha256(payload).toString("hex"));
  } finally {
    driver.stop();
    fileServer.close();
  }
});
