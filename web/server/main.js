"use strict";

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const U = require("./util");
const { Account } = require("./account");
const { WebApp } = require("./webapp");

const SCRYPT_KEYLEN = 64;
const SESSION_TTL_MS = 7 * 24 * 3600 * 1000;
const LOGIN_WINDOW_MS = 5 * 60 * 1000;
const LOGIN_MAX_ATTEMPTS = 10;
const MAX_JSON_BODY = 64 * 1024;

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
    portBase: U.TCP_PORT,
    httpPort: 8090,
    httpHost: "127.0.0.1",
    data: path.join(__dirname, "..", "data"),
    publicDir: path.join(__dirname, "..", "public"),
  };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--port-base") args.portBase = portArg(argv[++i], args.portBase, a);
    else if (a === "--http") args.httpPort = portArg(argv[++i], args.httpPort, a);
    else if (a === "--http-host") args.httpHost = argv[++i];
    else if (a === "--data") args.data = path.resolve(argv[++i]);
    else if (a === "--public") args.publicDir = path.resolve(argv[++i]);
  }
  return args;
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
  constructor(dataDir, portBase) {
    this.dataDir = dataDir;
    this.portBase = portBase;
    this.filePath = path.join(dataDir, "accounts.json");
    this.accounts = new Map();
    this.sessions = new Map();
    this.loginAttempts = new Map();
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

  nextPort() {
    const used = new Set(Array.from(this.accounts.values()).map((a) => a.tcpPort));
    let port = this.portBase;
    while (used.has(port)) port += 1;
    return port;
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
      tcpPort: this.nextPort(),
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
}

class ChatServer {
  constructor(args) {
    this.args = args;
    this.dataDir = args.data;
    fs.mkdirSync(args.data, { recursive: true });
    this.registry = new AccountRegistry(args.data, args.portBase);
    this.running = new Map();
    this.web = new WebApp({
      publicDir: args.publicDir,
      port: args.httpPort,
      host: args.httpHost,
      onAuth: (req) => {
        const rec = this.registry.sessionAccount(this._sessionToken(req));
        return rec ? rec.id : null;
      },
      onWsMessage: (conn, text) => this.onWsMessage(conn, text),
      apiGet: (pathname, params, req, res, accountId) =>
        this.handleApiGet(pathname, params, req, res, accountId),
      apiPost: (pathname, params, req, res, accountId) =>
        this.handleApiPost(pathname, params, req, res, accountId),
    });
  }

  account(record) {
    let account = this.running.get(record.id);
    if (!account) {
      account = new Account(this, record);
      this.running.set(record.id, account);
    }
    return account;
  }

  async start() {
    for (const record of this.registry.accounts.values()) {
      try {
        await this.account(record).start();
      } catch (e) {
        console.error(`\u8d26\u53f7 ${record.username} \u542f\u52a8\u5931\u8d25:`, e.message || e);
      }
    }
    await this.web.start();
    console.log("LocalChat Web \u670d\u52a1\u5668");
    console.log(`  \u754c\u9762: http://${this.args.httpHost}:${this.args.httpPort} (${this.registry.accounts.size} \u4e2a\u8d26\u53f7)`);
    for (const record of this.registry.accounts.values()) {
      console.log(`  \u8d26\u53f7 ${record.username}: \u534f\u8bae\u7aef\u53e3 TCP ${record.tcpPort}`);
    }
  }

  stop() {
    for (const account of this.running.values()) account.stop();
    this.web.stop();
  }

  onWsMessage(conn, text) {
    let msg;
    try {
      msg = JSON.parse(text);
    } catch (e) {
      return;
    }
    const record = this.registry.get(conn.accountId);
    if (!record) return;
    const account = this.account(record);
    account.handleAction(conn, msg.action, msg).catch((e) => {
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
      const account = this.account(this.registry.get(accountId));
      account.serveFile(res, pathname.slice("/files/".length));
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
      await this.account(record).start();
      this._json(res, 200, { ok: true, username: record.username, port: record.tcpPort });
      return null;
    }
    if (pathname === "/api/register") {
      const body = await this._readJsonBody(req);
      const isAuthed = Boolean(accountId);
      if (!this.registry.firstRun && !isAuthed) {
        this._json(res, 403, { ok: false, message: "\u9700\u767b\u5f55\u540e\u624d\u80fd\u65b0\u5efa\u8d26\u53f7" });
        return null;
      }
      const result = this.registry.create(String(body.username || ""), String(body.password || ""));
      if (!result.ok) {
        this._json(res, 400, result);
        return null;
      }
      const token = this.registry.createSession(result.record.id);
      this._setSessionCookie(res, token);
      await this.account(result.record).start();
      this._json(res, 200, { ok: true, username: result.record.username, port: result.record.tcpPort });
      return null;
    }
    if (pathname === "/api/logout") {
      this.registry.dropSession(this._sessionToken(req));
      this._clearSessionCookie(res);
      this._json(res, 200, { ok: true });
      return null;
    }
    if (!accountId) {
      this._json(res, 401, { error: "\u672a\u767b\u5f55" });
      return null;
    }
    if (pathname === "/api/upload") {
      const account = this.account(this.registry.get(accountId));
      account.handleUpload(params, req, res);
      return null;
    }
    return { ok: false };
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
