"use strict";

const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

function makeTmpDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

async function waitFor(fn, timeoutMs = 10000, stepMs = 25) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (fn()) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, stepMs));
  }
}

function httpRequest(port, method, reqPath, body, cookie, token) {
  return new Promise((resolve, reject) => {
    const headers = {};
    if (cookie) headers.Cookie = cookie;
    if (token) headers["X-LC-Token"] = token;
    let payload = null;
    if (body !== undefined && body !== null) {
      if (Buffer.isBuffer(body)) {
        payload = body;
      } else {
        payload = Buffer.from(JSON.stringify(body), "utf8");
        headers["Content-Type"] = "application/json";
      }
      headers["Content-Length"] = payload.length;
    }
    const req = http.request(
      { host: "127.0.0.1", port, path: reqPath, method, headers },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          let json = null;
          try {
            json = JSON.parse(text);
          } catch (e) {
            /* not json */
          }
          resolve({ status: res.statusCode, headers: res.headers, json, text });
        });
      }
    );
    req.on("error", reject);
    if (payload) req.write(payload);
    req.end();
  });
}

// Minimal RFC 6455 client, just enough to drive the server's WS endpoint
// (client frames must be masked; server frames are not).
class WsClient {
  static connect(port, auth) {
    const opts = typeof auth === "string" ? { cookie: auth } : auth || {};
    return new Promise((resolve, reject) => {
      const key = crypto.randomBytes(16).toString("base64");
      const req = http.request({
        host: "127.0.0.1",
        port,
        path: opts.token ? `/?token=${encodeURIComponent(opts.token)}` : "/",
        headers: {
          Connection: "Upgrade",
          Upgrade: "websocket",
          "Sec-WebSocket-Key": key,
          "Sec-WebSocket-Version": "13",
          ...(opts.cookie ? { Cookie: opts.cookie } : {}),
          ...(opts.token ? { "X-LC-Token": opts.token } : {}),
        },
      });
      req.on("upgrade", (res, socket, head) => {
        const client = new WsClient(socket);
        // frames arriving with the 101 response are delivered as `head`,
        // not through later socket "data" events
        if (head && head.length) client._onData(head);
        resolve(client);
      });
      req.on("response", (res) => {
        reject(new Error(`upgrade rejected with ${res.statusCode}`));
      });
      req.on("error", reject);
      req.end();
    });
  }

  constructor(socket) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.docs = [];
    this.waiters = [];
    this.closed = false;
    socket.on("data", (chunk) => this._onData(chunk));
    socket.on("close", () => {
      this.closed = true;
    });
    socket.on("error", () => {
      this.closed = true;
    });
  }

  _onData(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const frame = this._parseFrame();
      if (!frame) return;
      if (frame.opcode === 0x8) {
        this.close();
        return;
      }
      if (frame.opcode !== 0x1) continue;
      let doc;
      try {
        doc = JSON.parse(frame.payload.toString("utf8"));
      } catch (e) {
        continue;
      }
      this.docs.push(doc);
      const waiters = this.waiters.splice(0);
      for (const w of waiters) w();
    }
  }

  _parseFrame() {
    const buf = this.buffer;
    if (buf.length < 2) return null;
    const opcode = buf[0] & 0x0f;
    let len = buf[1] & 0x7f;
    let offset = 2;
    if (len === 126) {
      if (buf.length < 4) return null;
      len = buf.readUInt16BE(2);
      offset = 4;
    } else if (len === 127) {
      if (buf.length < 10) return null;
      len = Number(buf.readBigUInt64BE(2));
      offset = 10;
    }
    if (buf.length < offset + len) return null;
    const payload = buf.subarray(offset, offset + len);
    this.buffer = buf.subarray(offset + len);
    return { opcode, payload };
  }

  send(obj) {
    const payload = Buffer.from(JSON.stringify(obj), "utf8");
    const len = payload.length;
    const mask = crypto.randomBytes(4);
    let header;
    if (len < 126) {
      header = Buffer.from([0x81, 0x80 | len]);
    } else if (len < 65536) {
      header = Buffer.alloc(4);
      header[0] = 0x81;
      header[1] = 0x80 | 126;
      header.writeUInt16BE(len, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x81;
      header[1] = 0x80 | 127;
      header.writeBigUInt64BE(BigInt(len), 2);
    }
    const masked = Buffer.alloc(len);
    for (let i = 0; i < len; i++) masked[i] = payload[i] ^ mask[i & 3];
    try {
      this.socket.write(Buffer.concat([header, mask, masked]));
    } catch (e) {
      /* ignore */
    }
  }

  seen(predicate) {
    return this.docs.find(predicate) || null;
  }

  async next(predicate, timeoutMs = 5000) {
    const found = this.seen(predicate);
    if (found) return found;
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const wake = new Promise((resolve) => this.waiters.push(resolve));
      const timer = new Promise((resolve) => setTimeout(resolve, Math.max(10, deadline - Date.now())));
      await Promise.race([wake, timer]);
      if (this.closed && !this.seen(predicate)) {
        throw new Error("ws closed while waiting for message");
      }
      const hit = this.seen(predicate);
      if (hit) return hit;
      if (Date.now() >= deadline) {
        throw new Error(`timeout waiting for ws message; docs: ${JSON.stringify(this.docs)}`);
      }
    }
  }

  close() {
    try {
      this.socket.destroy();
    } catch (e) {
      /* ignore */
    }
  }
}

module.exports = { freePort, makeTmpDir, waitFor, httpRequest, WsClient };
