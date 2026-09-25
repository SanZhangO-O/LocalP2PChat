"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

const PYTHON = process.env.PYTHON || "python";

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

class PyDriver {
  constructor(args, options = {}) {
    this.args = args;
    this.lines = [];
    this.waiters = [];
    this.stderr = [];
    this.proc = spawn(PYTHON, [path.join(__dirname, "py_driver.py"), ...args], {
      cwd: __dirname,
      env: { ...process.env, ...(options.env || {}) },
      stdio: ["pipe", "pipe", "pipe"],
    });
    let buffer = "";
    this.proc.stdout.setEncoding("utf8");
    this.proc.stdout.on("data", (chunk) => {
      buffer += chunk;
      let idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (!line) continue;
        let doc;
        try {
          doc = JSON.parse(line);
        } catch (e) {
          this.lines.push({ event: "unparsed", line });
          continue;
        }
        this.lines.push(doc);
        const waiters = this.waiters.splice(0);
        for (const w of waiters) w.check();
      }
    });
    this.proc.stderr.setEncoding("utf8");
    this.proc.stderr.on("data", (chunk) => this.stderr.push(chunk));
  }

  writeStdin(obj) {
    this.proc.stdin.write(JSON.stringify(obj) + "\n");
  }

  find(event, predicate = () => true) {
    return this.lines.find((l) => l.event === event && predicate(l));
  }

  async wait(event, predicate = () => true, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const found = this.find(event, predicate);
      if (found) return found;
      if (this.proc.exitCode !== null) {
        throw new Error(
          `driver exited (code ${this.proc.exitCode}) waiting for "${event}"; stderr: ${this.stderr.join("")}`
        );
      }
      const checkPromise = new Promise((resolve) => {
        const check = () => {
          const found = this.find(event, predicate);
          if (found) resolve(true);
        };
        this.waiters.push({ check });
        setTimeout(() => {
          const idx = this.waiters.findIndex((w) => w.check === check);
          if (idx >= 0) this.waiters.splice(idx, 1);
          resolve(false);
        }, Math.max(10, deadline - Date.now()));
      });
      const got = await checkPromise;
      if (got) return this.find(event, predicate);
      if (Date.now() >= deadline) {
        throw new Error(
          `timeout waiting for "${event}"; lines: ${JSON.stringify(this.lines)}; stderr: ${this.stderr.join("")}`
        );
      }
    }
  }

  stop() {
    try {
      this.proc.kill();
    } catch (e) {
      /* ignore */
    }
  }
}

async function waitFor(fn, timeoutMs = 10000, stepMs = 25) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    if (fn()) return true;
    if (Date.now() >= deadline) return false;
    await new Promise((resolve) => setTimeout(resolve, stepMs));
  }
}

module.exports = { PYTHON, PyDriver, freePort, makeTmpDir, waitFor };
