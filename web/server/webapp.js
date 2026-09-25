"use strict";

const http = require("http");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const MAX_WS_MESSAGE = 4 * 1024 * 1024;
const MAX_WS_FRAME = 64 * 1024 * 1024;

class WebSocketConn {
  constructor(socket) {
    this.socket = socket;
    this.onmessage = null;
    this.onclose = null;
    this.buffer = Buffer.alloc(0);
    this.fragments = [];
    this.fragmentBytes = 0;
    this.fragOpcode = 0;
    this.closed = false;
    socket.on("data", (chunk) => this._onData(chunk));
    socket.on("error", () => this._closed());
    socket.on("close", () => this._closed());
    socket.on("end", () => this._closed());
  }

  _closed() {
    if (this.closed) return;
    this.closed = true;
    if (this.onclose) this.onclose();
  }

  _onData(chunk) {
    if (this.closed) return;
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      const frame = this._parseFrame();
      if (frame == null) return;
      if (frame.protocolError) {
        this._protocolClose();
        return;
      }
      if (frame.opcode === 0x8) {
        this._sendFrame(0x8, frame.payload);
        this._closed();
        return;
      }
      if (frame.opcode === 0x9) {
        this._sendFrame(0xa, frame.payload);
        continue;
      }
      if (frame.opcode === 0xa) continue;
      if (frame.opcode === 0x1 || frame.opcode === 0x2) {
        if (frame.fin) {
          this._deliver(frame.payload);
        } else {
          this.fragments = [frame.payload];
          this.fragmentBytes = frame.payload.length;
          this.fragOpcode = frame.opcode;
          if (this.fragmentBytes > MAX_WS_MESSAGE) return this._protocolClose();
        }
      } else if (frame.opcode === 0x0) {
        this.fragmentBytes += frame.payload.length;
        if (this.fragmentBytes > MAX_WS_MESSAGE) return this._protocolClose();
        this.fragments.push(frame.payload);
        if (frame.fin) {
          const full = Buffer.concat(this.fragments);
          this.fragments = [];
          this.fragmentBytes = 0;
          this._deliver(full);
        }
      }
    }
  }

  _protocolClose() {
    const payload = Buffer.alloc(2);
    payload.writeUInt16BE(1002, 0);
    this._sendFrame(0x8, payload);
    try {
      this.socket.destroy();
    } catch (e) {
      /* ignore */
    }
    this._closed();
  }

  _deliver(payload) {
    if (this.onmessage) {
      try {
        this.onmessage(payload.toString("utf8"));
      } catch (e) {
        /* ignore */
      }
    }
  }

  _parseFrame() {
    const buf = this.buffer;
    if (buf.length < 2) return null;
    const first = buf[0];
    const second = buf[1];
    const fin = (first & 0x80) !== 0;
    const rsv = (first & 0x70) !== 0;
    const opcode = first & 0x0f;
    const masked = (second & 0x80) !== 0;
    // RFC 6455: no extensions negotiated, every client frame must be masked,
    // control frames must be final and <= 125 bytes
    if (rsv || !masked) return { protocolError: true };
    let len = second & 0x7f;
    let offset = 2;
    if (len === 126) {
      if (buf.length < 4) return null;
      len = buf.readUInt16BE(2);
      offset = 4;
    } else if (len === 127) {
      if (buf.length < 10) return null;
      const big = buf.readBigUInt64BE(2);
      if (big > BigInt(MAX_WS_FRAME)) return { protocolError: true };
      len = Number(big);
      offset = 10;
    }
    if ((opcode & 0x8) !== 0 && (!fin || len > 125)) return { protocolError: true };
    let mask = null;
    {
      if (buf.length < offset + 4) return null;
      mask = buf.subarray(offset, offset + 4);
      offset += 4;
    }
    if (buf.length < offset + len) return null;
    let payload = buf.subarray(offset, offset + len);
    this.buffer = buf.subarray(offset + len);
    if (mask) {
      const out = Buffer.alloc(payload.length);
      for (let i = 0; i < payload.length; i++) {
        out[i] = payload[i] ^ mask[i & 3];
      }
      payload = out;
    }
    return { fin, opcode, payload };
  }

  _sendFrame(opcode, payload) {
    if (this.closed) return;
    const len = payload.length;
    let header;
    if (len < 126) {
      header = Buffer.from([0x80 | opcode, len]);
    } else if (len < 65536) {
      header = Buffer.alloc(4);
      header[0] = 0x80 | opcode;
      header[1] = 126;
      header.writeUInt16BE(len, 2);
    } else {
      header = Buffer.alloc(10);
      header[0] = 0x80 | opcode;
      header[1] = 127;
      header.writeBigUInt64BE(BigInt(len), 2);
    }
    try {
      this.socket.write(Buffer.concat([header, payload]));
    } catch (e) {
      this._closed();
    }
  }

  send(text) {
    this._sendFrame(0x1, Buffer.from(text, "utf8"));
  }

  close() {
    if (this.closed) return;
    try {
      this._sendFrame(0x8, Buffer.alloc(0));
      this.socket.end();
    } catch (e) {
      /* ignore */
    }
    this._closed();
  }
}

function acceptKey(key) {
  return crypto.createHash("sha1").update(key + WS_GUID).digest("base64");
}

class WebApp {
  constructor(options) {
    this.publicDir = options.publicDir;
    this.port = options.port;
    this.host = options.host || "127.0.0.1";
    this.onAuth = options.onAuth || (() => null);
    this.onWsMessage = options.onWsMessage || (() => {});
    this.onWsClose = options.onWsClose || (() => {});
    this.apiGet = options.apiGet || (async () => null);
    this.apiPost = options.apiPost || (async () => null);
    this.server = null;
    this.connections = new Set();
  }

  broadcast(obj, accountId) {
    const text = JSON.stringify(obj);
    for (const conn of this.connections) {
      if (accountId != null && conn.accountId !== accountId) continue;
      try {
        conn.send(text);
      } catch (e) {
        /* ignore */
      }
    }
  }

  start() {
    return new Promise((resolve, reject) => {
      this.server = http.createServer((req, res) => {
        this._handleRequest(req, res).catch(() => {
          try {
            res.writeHead(500);
            res.end("internal error");
          } catch (e) {
            /* ignore */
          }
        });
      });
      this.server.on("upgrade", (req, socket) => {
        const key = req.headers["sec-websocket-key"];
        if (
          !key ||
          String(req.headers.upgrade || "").toLowerCase() !== "websocket"
        ) {
          socket.destroy();
          return;
        }
        const accountId = this.onAuth(req);
        if (!accountId) {
          socket.write("HTTP/1.1 401 Unauthorized\r\nConnection: close\r\n\r\n");
          socket.destroy();
          return;
        }
        socket.write(
          "HTTP/1.1 101 Switching Protocols\r\n" +
            "Upgrade: websocket\r\n" +
            "Connection: Upgrade\r\n" +
            `Sec-WebSocket-Accept: ${acceptKey(key)}\r\n\r\n`
        );
        socket.setNoDelay(true);
        const conn = new WebSocketConn(socket);
        conn.accountId = accountId;
        conn.onmessage = (text) => this.onWsMessage(conn, text);
        conn.onclose = () => {
          this.connections.delete(conn);
          this.onWsClose(conn);
        };
        this.connections.add(conn);
      });
      this.server.on("error", reject);
      this.server.listen(this.port, this.host, () => {
        // port 0 means "any free port": report what was actually bound
        const bound = this.server.address();
        resolve(bound ? bound.port : this.port);
      });
    });
  }

  stop() {
    for (const conn of Array.from(this.connections)) conn.close();
    if (this.server) {
      try {
        this.server.close();
      } catch (e) {
        /* ignore */
      }
    }
  }

  _serveStatic(res, urlPath) {
    let rel = urlPath === "/" ? "/index.html" : urlPath;
    rel = rel.split("?")[0];
    const root = path.normalize(this.publicDir);
    const filePath = path.normalize(path.join(root, rel));
    // containment must be a real prefix (a sibling directory like
    // "<publicDir>2" must not pass a naive startsWith check)
    if (filePath !== root && !filePath.startsWith(root + path.sep)) {
      res.writeHead(403);
      res.end();
      return;
    }
    fs.readFile(filePath, (err, data) => {
      if (err) {
        res.writeHead(404);
        res.end("not found");
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      const types = {
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".json": "application/json; charset=utf-8",
      };
      res.writeHead(200, { "Content-Type": types[ext] || "application/octet-stream" });
      res.end(data);
    });
  }

  async _handleRequest(req, res) {
    const url = new URL(req.url, "http://localhost");
    let pathname;
    try {
      pathname = decodeURIComponent(url.pathname);
    } catch (e) {
      res.writeHead(400);
      res.end("bad request");
      return;
    }
    if (pathname.startsWith("/api/") || pathname.startsWith("/files/")) {
      const accountId = this.onAuth(req);
      const publicPaths = ["/api/login", "/api/register", "/api/whoami"];
      if (!accountId && !publicPaths.includes(pathname)) {
        res.writeHead(401, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify({ error: "unauthorized" }));
        return;
      }
      if (req.method === "GET") {
        const result = await this.apiGet(pathname, url.searchParams, req, res, accountId);
        if (result !== null && result !== undefined) {
          res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify(result));
        }
        return;
      }
      if (req.method === "POST" || req.method === "PUT") {
        const result = await this.apiPost(pathname, url.searchParams, req, res, accountId);
        if (result !== null && result !== undefined) {
          res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
          res.end(JSON.stringify(result));
        }
        return;
      }
      res.writeHead(405);
      res.end();
      return;
    }
    this._serveStatic(res, pathname);
  }
}

module.exports = { WebApp, WebSocketConn, acceptKey };
