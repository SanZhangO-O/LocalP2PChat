"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

const U = require("./util");
const { WebApp } = require("./webapp");
const { ChatEngine } = require("./chat");

const SCRYPT_KEYLEN = 64;
const SESSION_TTL_MS = 7 * 24 * 3600 * 1000;
const LOGIN_WINDOW_MS = 5 * 60 * 1000;
const LOGIN_MAX_ATTEMPTS = 10;
const REGISTER_WINDOW_MS = 5 * 60 * 1000;
const REGISTER_MAX_ATTEMPTS = 20;
const MAX_JSON_BODY = 64 * 1024;
const MAX_UPLOAD_BYTES = 512 * 1024 * 1024;

function portArg(value, fallback, flag) {
  const parsed = parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    console.warn(`\u5ffd\u7565\u65e0\u6548\u53c2\u6570 ${flag} ${value}\uff0c\u4f7f\u7528\u9ed8\u8ba4\u503c ${fallback}`);
    return fallback;
  }
  return parsed;
}

function parseArgs(argv) {
  const args = {
    httpPort: 8090,
    httpHost: "127.0.0.1",
    data: path.join(__dirname, "..", "data"),
    publicDir: path.join(__dirname, "..", "public"),
    open: false,
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--http") args.httpPort = portArg(argv[++i], args.httpPort, a);
    else if (a === "--http-host") args.httpHost = argv[++i];
    else if (a === "--data") args.data = path.resolve(argv[++i]);
    else if (a === "--public") args.publicDir = path.resolve(argv[++i]);
    else if (a === "--open") args.open = true;
  }
  return args;
}

function openInBrowser(url) {
  try {
    if (process.platform === "win32") {
      spawn("cmd.exe", ["/c", "start", "", url], { detached: true, stdio: "ignore" }).unref();
    } else if (process.platform === "darwin") {
      spawn("open", [url], { detached: true, stdio: "ignore" }).unref();
    } else {
      spawn("xdg-open", [url], { detached: true, stdio: "ignore" }).unref();
    }
  } catch (e) {
    /* browser auto-open is best effort */
  }
}

function hashPassword(password, salt) {
  return crypto.scryptSync(String(password), Buffer.from(salt, "hex"), SCRYPT_KEYLEN).toString("hex");
}

function verifyPassword(password, salt, expectedHash) {
  const actual = hashPassword(password, salt);
  const a = Buffer.from(actual, "hex");
  const b = Buffer.from(expectedHash, "hex");
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

function validUsername(name) {
  return typeof name === "string" && /^[A-Za-z0-9_\-\u4e00-\u9fa5]{1,32}$/.test(name);
}

function validPassword(pw) {
  return typeof pw === "string" && pw.length >= 6 && pw.length <= 128;
}

class AccountRegistry {
  constructor(dataDir) {
    this.dataDir = dataDir;
    this.filePath = path.join(dataDir, "accounts.json");
    this.accounts = new Map();
    this.sessions = new Map();
    this.loginAttempts = new Map();
    this.registerAttempts = new Map();
    this.load();
  }

  load() {
    let list = [];
    try {
      list = JSON.parse(fs.readFileSync(this.filePath, "utf8"));
    } catch (e) {
      list = [];
    }
    for (const rec of list) {
      this.accounts.set(rec.id, rec);
    }
  }

  save() {
    const list = Array.from(this.accounts.values());
    const tmp = this.filePath + ".tmp";
    fs.writeFileSync(tmp, JSON.stringify(list, null, 2), "utf8");
    fs.renameSync(tmp, this.filePath);
  }

  get firstRun() {
    return this.accounts.size === 0;
  }

  get(id) {
    return this.accounts.get(id) || null;
  }

  create(username, password) {
    if (!validUsername(username)) {
      return { ok: false, message: "\u7528\u6237\u540d\u9700 1-32 \u4f4d\uff08\u4e2d\u82f1\u6587\u3001\u6570\u5b57\u3001_- \uff09" };
    }
    if (!validPassword(password)) {
      return { ok: false, message: "\u5bc6\u7801\u81f3\u5c11 6 \u4f4d" };
    }
    const exists = Array.from(this.accounts.values()).some(
      (a) => a.username.toLowerCase() === username.toLowerCase()
    );
    if (exists) {
      return { ok: false, message: "\u7528\u6237\u540d\u5df2\u5b58\u5728" };
    }
    const salt = crypto.randomBytes(16).toString("hex");
    const record = {
      id: U.uuid(),
      username,
      salt,
      passHash: hashPassword(password, salt),
      nickname: username,
      createdAt: Date.now(),
    };
    this.accounts.set(record.id, record);
    this.save();
    return { ok: true, record };
  }

  verify(username, password) {
    const rec = Array.from(this.accounts.values()).find(
      (a) => a.username === String(username || "")
    );
    if (!rec) {
      hashPassword(password || "", "00");
      return null;
    }
    if (!verifyPassword(password || "", rec.salt, rec.passHash)) return null;
    return rec;
  }

  createSession(accountId) {
    const token = crypto.randomBytes(32).toString("hex");
    this.sessions.set(token, { accountId, expires: Date.now() + SESSION_TTL_MS });
    this._sweepSessions();
    return token;
  }

  sessionAccount(token) {
    if (!token) return null;
    const session = this.sessions.get(token);
    if (!session) return null;
    if (Date.now() > session.expires) {
      this.sessions.delete(token);
      return null;
    }
    return this.accounts.get(session.accountId) || null;
  }

  dropSession(token) {
    if (token) this.sessions.delete(token);
  }

  _sweepSessions() {
    const now = Date.now();
    for (const [token, session] of this.sessions) {
      if (now > session.expires) this.sessions.delete(token);
    }
  }

  allowLogin(ip) {
    if (!ip) return true;
    const now = Date.now();
    let entry = this.loginAttempts.get(ip);
    if (!entry || now > entry.resetAt) {
      entry = { count: 0, resetAt: now + LOGIN_WINDOW_MS };
      this.loginAttempts.set(ip, entry);
    }
    if (entry.count >= LOGIN_MAX_ATTEMPTS) return false;
    return true;
  }

  noteLoginFailure(ip) {
    if (!ip) return;
    const entry = this.loginAttempts.get(ip);
    if (entry) entry.count += 1;
  }

  // every attempt counts (successful sign-ups included): mass account
  // creation is exactly the spam this limit exists for
  allowRegister(ip) {
    if (!ip) return true;
    const now = Date.now();
    let entry = this.registerAttempts.get(ip);
    if (!entry || now > entry.resetAt) {
      entry = { count: 0, resetAt: now + REGISTER_WINDOW_MS };
      this.registerAttempts.set(ip, entry);
    }
    entry.count += 1;
    return entry.count <= REGISTER_MAX_ATTEMPTS;
  }
}

class ChatServer {
  constructor(args) {
    this.args = args;
    this.dataDir = args.data;
    fs.mkdirSync(args.data, { recursive: true });
    this.registry = new AccountRegistry(args.data);
    this.engine = new ChatEngine(this, args.data);
    this.onlineCounts = new Map(); // accountId -> open ws connections
    this.web = new WebApp({
      publicDir: args.publicDir,
      port: args.httpPort,
      host: args.httpHost,
      onAuth: (req, url) => {
        const rec = this.registry.sessionAccount(this._pickToken(req, url && url.searchParams));
        return rec ? rec.id : null;
      },
      onWsOpen: (conn) => this.onWsOpen(conn),
      onWsClose: (conn) => this.onWsClose(conn),
      onWsMessage: (conn, text) => this.onWsMessage(conn, text),
      apiGet: (pathname, params, req, res, accountId) =>
        this.handleApiGet(pathname, params, req, res, accountId),
      apiPost: (pathname, params, req, res, accountId) =>
        this.handleApiPost(pathname, params, req, res, accountId),
    });
  }

  isOnline(accountId) {
    return (this.onlineCounts.get(accountId) || 0) > 0;
  }

  onWsOpen(conn) {
    this.onlineCounts.set(conn.accountId, (this.onlineCounts.get(conn.accountId) || 0) + 1);
    this.engine.presenceChanged();
    conn.send(JSON.stringify({ snapshot: this.engine.snapshotFor(conn.accountId) }));
  }

  onWsClose(conn) {
    const left = (this.onlineCounts.get(conn.accountId) || 1) - 1;
    if (left <= 0) this.onlineCounts.delete(conn.accountId);
    else this.onlineCounts.set(conn.accountId, left);
    this.engine.presenceChanged();
  }

  async start() {
    const port = await this.web.start();
    console.log("LocalChat Web \u670d\u52a1\u5668\u5df2\u542f\u52a8");
    console.log(`  \u672c\u673a\u8bbf\u95ee:   http://localhost:${port}`);
    const lan = U.lanAddresses();
    if (this.args.httpHost === "0.0.0.0" && lan.length) {
      console.log("  \u5c40\u57df\u7f51\u5185\u5176\u4ed6\u7528\u6237\u7528\u6d4f\u89c8\u5668\u6253\u5f00:");
      for (const ip of lan) {
        console.log(`    http://${ip}:${port}`);
      }
    } else {
      console.log(`  \u754c\u9762\u5730\u5740:   http://${this.args.httpHost}:${port}`);
    }
    console.log(`  \u6570\u636e\u76ee\u5f55:   ${this.dataDir} (${this.registry.accounts.size} \u4e2a\u8d26\u53f7)`);
    console.log("  \u9996\u6b21\u4f7f\u7528\u5728\u9875\u9762\u4e0a\u521b\u5efa\u7b2c\u4e00\u4e2a\u8d26\u53f7\uff1bCtrl+C \u505c\u6b62\u670d\u52a1");
    if (this.args.open) openInBrowser(`http://localhost:${port}`);
    return port;
  }

  stop() {
    this.web.stop();
  }

  onWsMessage(conn, text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch (e) {
      return;
    }
    if (!conn.accountId) return;
    this.engine.handleAction(conn, msg.action, msg).catch((e) => {
      try {
        conn.send(JSON.stringify({ error: String(e.message || e), action: msg.action }));
      } catch (e2) {
        /* ignore */
      }
    });
  }

  _sessionToken(req) {
    const header = req.headers.cookie || "";
    for (const part of header.split(";")) {
      const idx = part.indexOf("=");
      if (idx < 0) continue;
      if (part.slice(0, idx).trim() === "lc_session") {
        return part.slice(idx + 1).trim();
      }
    }
    return null;
  }

  // One browser can hold several accounts: the active one is the cookie
  // session, switched accounts authenticate via token header / query param.
  _pickToken(req, searchParams) {
    const header = req.headers["x-lc-token"];
    if (header) return String(header);
    if (searchParams && typeof searchParams.get === "function") {
      const queryToken = searchParams.get("token");
      if (queryToken) return queryToken;
    }
    return this._sessionToken(req);
  }

  _clientIp(req) {
    return (req.socket && req.socket.remoteAddress) || "";
  }

  _setSessionCookie(res, token) {
    res.setHeader(
      "Set-Cookie",
      `lc_session=${token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=${Math.floor(SESSION_TTL_MS / 1000)}`
    );
  }

  _clearSessionCookie(res) {
    res.setHeader("Set-Cookie", "lc_session=; HttpOnly; Path=/; SameSite=Lax; Max-Age=0");
  }

  _json(res, code, obj) {
    res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify(obj));
  }

  _readJsonBody(req) {
    return new Promise((resolve, reject) => {
      let size = 0;
      const chunks = [];
      req.on("data", (chunk) => {
        size += chunk.length;
        if (size > MAX_JSON_BODY) {
          req.destroy();
          reject(new Error("body too large"));
          return;
        }
        chunks.push(chunk);
      });
      req.on("end", () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}"));
        } catch (e) {
          resolve({});
        }
      });
      req.on("error", reject);
    });
  }

  async handleApiGet(pathname, params, req, res, accountId) {
    if (pathname === "/api/whoami") {
      const record = accountId ? this.registry.get(accountId) : null;
      return {
        authenticated: Boolean(record),
        firstRun: this.registry.firstRun,
        username: record ? record.username : null,
      };
    }
    if (!accountId) {
      this._json(res, 401, { error: "\u672a\u767b\u5f55" });
      return null;
    }
    if (pathname.startsWith("/files/")) {
      this.engine.serveFile(res, pathname.slice("/files/".length));
      return null;
    }
    return { ok: true };
  }

  async handleApiPost(pathname, params, req, res, accountId) {
    if (pathname === "/api/login") {
      const body = await this._readJsonBody(req);
      if (!this.registry.allowLogin(this._clientIp(req))) {
        this._json(res, 429, { ok: false, message: "\u5c1d\u8bd5\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5" });
        return null;
      }
      const record = this.registry.verify(body.username, body.password);
      if (!record) {
        this.registry.noteLoginFailure(this._clientIp(req));
        this._json(res, 401, { ok: false, message: "\u7528\u6237\u540d\u6216\u5bc6\u7801\u9519\u8bef" });
        return null;
      }
      const token = this.registry.createSession(record.id);
      this._setSessionCookie(res, token);
      this._json(res, 200, { ok: true, username: record.username, token });
      return null;
    }
    if (pathname === "/api/register") {
      const body = await this._readJsonBody(req);
      // open registration: the login page offers a sign-up button, per-IP
      // rate limiting keeps spam in check
      if (!this.registry.allowRegister(this._clientIp(req))) {
        this._json(res, 429, { ok: false, message: "\u5c1d\u8bd5\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5" });
        return null;
      }
      const result = this.registry.create(String(body.username || ""), String(body.password || ""));
      if (!result.ok) {
        this._json(res, 400, result);
        return null;
      }
      const isAuthed = Boolean(accountId);
      const token = this.registry.createSession(result.record.id);
      // signing up from the login page logs the new account in; a logged-in
      // user registering another account keeps their own session
      if (!isAuthed) this._setSessionCookie(res, token);
      this.engine.pushSnapshotAll();
      this._json(res, 200, { ok: true, username: result.record.username, token });
      return null;
    }
    if (pathname === "/api/logout") {
      const cookieToken = this._sessionToken(req);
      const token = this._pickToken(req, params);
      this.registry.dropSession(token);
      // only clear the browser cookie when the dropped session is the one
      // the cookie points at (removing a switched account must not log the
      // active account out)
      if (!cookieToken || token === cookieToken) {
        this._clearSessionCookie(res);
      }
      this._json(res, 200, { ok: true });
      return null;
    }
    if (!accountId) {
      this._json(res, 401, { error: "\u672a\u767b\u5f55" });
      return null;
    }
    if (pathname === "/api/upload") {
      this.handleUpload(params, req, res);
      return null;
    }
    return { ok: false };
  }

  handleUpload(params, req, res) {
    const name = U.sanitizeFileName(params.get("name") || "file");
    const uploadId = `${U.uuid()}_${name}`;
    const target = this.engine._uploadPath(uploadId);
    const out = fs.createWriteStream(target);
    let size = 0;
    let responded = false;
    const respond = (code, body) => {
      if (responded) return;
      responded = true;
      try {
        res.writeHead(code, { "Content-Type": "application/json; charset=utf-8" });
        res.end(JSON.stringify(body));
      } catch (e) {
        /* ignore */
      }
    };
    const discardPartial = () => {
      try {
        out.destroy();
      } catch (e) {
        /* ignore */
      }
      try {
        fs.unlinkSync(target);
      } catch (e) {
        /* ignore */
      }
    };
    let rejected = false;
    req.on("data", (chunk) => {
      if (rejected) return;
      size += chunk.length;
      if (size > MAX_UPLOAD_BYTES) {
        rejected = true;
        respond(413, { error: "file too large" });
        req.unpipe(out);
        discardPartial();
        // keep consuming and discarding the rest: resetting the socket here
        // would destroy the 413 before the client can read it
        req.resume();
      }
    });
    req.pipe(out);
    out.on("finish", () => respond(200, { uploadId, size }));
    out.on("error", () => respond(500, { error: "upload failed" }));
  }
}

async function main() {
  const args = parseArgs(process.argv);
  const server = new ChatServer(args);
  process.on("SIGINT", () => {
    server.stop();
    process.exit(0);
  });
  await server.start();
}

if (require.main === module) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}

module.exports = { ChatServer, AccountRegistry, parseArgs, hashPassword, verifyPassword };
