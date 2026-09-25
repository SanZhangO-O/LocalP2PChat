"use strict";

const fs = require("fs");
const net = require("net");
const path = require("path");

const M = require("./models");
const C = require("./crypto");
const U = require("./util");

const MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024;
const CHUNK_SIZE = 64 * 1024;
const MAX_CHUNK_WIRE = CHUNK_SIZE + 12 + 16;
const GCM_MIN_FRAME = 12 + 16;
const FILE_DL_TOKEN_PREFIX = "lc-file-dl-v1:";
const FILE_SERVER_TTL = 15 * 60.0;

function fileDownloadToken(fileKey, fileId) {
  return C.toB64(
    C.hmacSha256(fileKey, Buffer.from(FILE_DL_TOKEN_PREFIX + fileId, "ascii"))
  );
}

function packFrame(blob) {
  const header = Buffer.alloc(4);
  header.writeUInt32BE(blob.length, 0);
  return Buffer.concat([header, blob]);
}

function serveDownload(conn, req, fileId, filePath, fileSize, fileKey) {
  let settled = false;
  let stream = null;
  // end() flushes pending writes and sends the FIN; destroying right away can
  // discard the last frames (and the EOF marker) mid-flight
  const complete = (eof) => {
    if (settled) return;
    settled = true;
    try {
      if (eof) conn.end(Buffer.alloc(4));
      else conn.end();
    } catch (e) {
      /* ignore */
    }
    const timer = setTimeout(() => {
      try {
        conn.destroy();
      } catch (e) {
        /* ignore */
      }
    }, 10000);
    if (timer.unref) timer.unref();
    conn.once("close", () => clearTimeout(timer));
  };
  const abort = () => {
    if (settled) return;
    settled = true;
    if (stream) {
      try {
        stream.destroy();
      } catch (e) {
        /* ignore */
      }
    }
    try {
      conn.destroy();
    } catch (e) {
      /* ignore */
    }
  };
  try {
    if (req.type !== "file_download" || req.fileId !== fileId) return complete(false);
    if (req.offset == null || req.offset < 0) return complete(false);
    if (fileSize >= 0 && req.offset > fileSize) return complete(false);
    if (!req.token) return complete(false);
    const expected = C.hmacSha256(fileKey, Buffer.from(FILE_DL_TOKEN_PREFIX + fileId, "ascii"));
    let provided;
    try {
      provided = C.fromB64(req.token);
    } catch (e) {
      return complete(false);
    }
    if (!C.constantTimeEquals(provided, expected)) return complete(false);
    const meta = new M.NetworkPacket({
      type: "file_meta",
      fileInfo: new M.FileInfo(fileId, path.basename(filePath), fileSize, "", 0),
    });
    const metaLine = C.toB64(C.aesGcmEncrypt(fileKey, Buffer.from(meta.toJson(), "utf8")));
    conn.write(Buffer.from(metaLine + "\n", "utf8"));
    conn.on("error", abort);
    conn.on("close", () => {
      if (stream) {
        try {
          stream.destroy();
        } catch (e) {
          /* ignore */
        }
      }
    });
    stream = fs.createReadStream(filePath, { start: req.offset });
    stream.on("data", (chunk) => {
      let pos = 0;
      while (pos < chunk.length) {
        const slice = chunk.subarray(pos, Math.min(pos + CHUNK_SIZE, chunk.length));
        pos += slice.length;
        if (!conn.write(packFrame(C.aesGcmEncrypt(fileKey, slice)))) {
          stream.pause();
          conn.once("drain", () => stream.resume());
        }
      }
    });
    stream.on("end", () => complete(true));
    stream.on("error", abort);
  } catch (e) {
    abort();
  }
}

class OutgoingFileServer {
  constructor(fileId, filePath, fileSize, fileKey) {
    this.fileId = fileId;
    this.filePath = filePath;
    this.fileSize = fileSize;
    this.fileKey = fileKey;
    this.closed = false;
    this.server = null;
    this.timer = null;
  }

  start() {
    return new Promise((resolve, reject) => {
      const server = net.createServer((conn) => {
        conn.setNoDelay(true);
        let buffer = Buffer.alloc(0);
        const onLine = (line) => {
          let req;
          try {
            req = M.NetworkPacket.fromJson(line);
          } catch (e) {
            cleanup();
            return;
          }
          cleanup();
          serveDownload(conn, req, this.fileId, this.filePath, this.fileSize, this.fileKey);
        };
        const cleanup = () => {
          conn.removeListener("data", onData);
          conn.removeListener("error", onError);
          conn.removeListener("close", onClose);
        };
        const fail = () => {
          cleanup();
          try {
            conn.destroy();
          } catch (e) {
            /* ignore */
          }
        };
        const onData = (chunk) => {
          buffer = Buffer.concat([buffer, chunk]);
          const idx = buffer.indexOf(0x0a);
          if (idx < 0) {
            if (buffer.length > U.MAX_LINE_LENGTH) fail();
            return;
          }
          const line = buffer.subarray(0, idx).toString("utf8");
          onLine(line);
        };
        const onError = () => fail();
        const onClose = () => fail();
        conn.on("data", onData);
        conn.on("error", onError);
        conn.on("close", onClose);
      });
      server.on("error", (err) => {
        if (!this.closed) reject(err);
      });
      server.listen(0, "0.0.0.0", () => {
        this.server = server;
        this.timer = setTimeout(() => this.close(), FILE_SERVER_TTL * 1000);
        resolve(server.address().port);
      });
    });
  }

  close() {
    if (this.closed) return;
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    try {
      this.server.close();
    } catch (e) {
      /* ignore */
    }
  }
}

class SocketFrameReader {
  constructor(socket) {
    this.socket = socket;
    this.buffer = Buffer.alloc(0);
    this.error = null;
    this.ended = false;
    this.reads = [];
    socket.on("data", (chunk) => {
      if (this.ended) return;
      this.buffer = Buffer.concat([this.buffer, chunk]);
      this._pump();
    });
    const fail = () => {
      this.ended = true;
      this.error = this.error || new Error("connection closed");
      this._pump();
    };
    socket.on("error", (err) => {
      this.error = err;
      fail();
    });
    socket.on("close", fail);
    socket.on("end", fail);
  }

  _headFrameLen() {
    if (this.buffer.length < 4) return null;
    const len = this.buffer.readUInt32BE(0);
    if (len > MAX_CHUNK_WIRE) {
      // a bogus length must not make us buffer up to 4 GiB before failing
      this.error = new Error("file frame too large");
      this.ended = true;
      return null;
    }
    if (this.buffer.length < 4 + len) return null;
    return len;
  }

  _headLine() {
    const idx = this.buffer.indexOf(0x0a);
    if (idx < 0) {
      if (this.buffer.length > U.MAX_LINE_LENGTH) return { tooLong: true };
      return null;
    }
    return { line: this.buffer.subarray(0, idx).toString("utf8").replace(/\r$/, ""), size: idx + 1 };
  }

  _pump() {
    while (this.reads.length > 0) {
      const read = this.reads[0];
      if (read.type === "line") {
        const head = this._headLine();
        if (head == null) break;
        this.reads.shift();
        clearTimeout(read.timer);
        if (head.tooLong) read.reject(new Error("line too long"));
        else {
          this.buffer = this.buffer.subarray(head.size);
          read.resolve(head.line);
        }
      } else {
        const len = this._headFrameLen();
        if (len == null) break;
        this.reads.shift();
        clearTimeout(read.timer);
        const frame = this.buffer.subarray(4, 4 + len);
        this.buffer = this.buffer.subarray(4 + len);
        read.resolve(frame);
      }
    }
    while (this.reads.length > 0 && this.ended) {
      const read = this.reads.shift();
      clearTimeout(read.timer);
      read.reject(this.error || new Error("connection closed"));
    }
  }

  _enqueue(read, timeoutMs) {
    return new Promise((resolve, reject) => {
      read.resolve = resolve;
      read.reject = reject;
      read.timer = setTimeout(() => {
        const idx = this.reads.indexOf(read);
        if (idx >= 0) this.reads.splice(idx, 1);
        reject(new Error("read timeout"));
      }, timeoutMs);
      this.reads.push(read);
      this._pump();
    });
  }

  readFrame(timeoutMs = 120000) {
    return this._enqueue({ type: "frame" }, timeoutMs);
  }

  readLine(timeoutMs = 15000) {
    return this._enqueue({ type: "line" }, timeoutMs);
  }

  destroy() {
    try {
      this.socket.destroy();
    } catch (e) {
      /* ignore */
    }
  }
}

async function downloadFileOffer(fileInfo, targetPath, options = {}) {
  options = options || {};
  const onProgress = options.onProgress || null;
  if (fileInfo.fileSize < 0) return { ok: false, message: "\u6587\u4ef6\u5927\u5c0f\u65e0\u6548" };
  if (fileInfo.fileSize > MAX_DOWNLOAD_BYTES) {
    return { ok: false, message: "\u6587\u4ef6\u8fc7\u5927\uff08\u8d85\u8fc7 512 MB \u9650\u5236\uff09" };
  }
  let fileKey;
  try {
    fileKey = fileInfo.fileKey ? C.fromB64(fileInfo.fileKey) : null;
  } catch (e) {
    fileKey = null;
  }
  if (!fileKey || fileKey.length !== C.KEY_LEN) {
    return { ok: false, message: "\u6587\u4ef6\u5bc6\u94a5\u7f3a\u5931\u6216\u65e0\u6548" };
  }
  const tmpPath = targetPath + ".part";
  let start = options.offset || 0;
  if (start < 0) start = 0;
  if (fileInfo.fileSize > 0 && start > fileInfo.fileSize) start = 0;
  let partSize = 0;
  try {
    partSize = fs.statSync(tmpPath).size;
  } catch (e) {
    partSize = 0;
  }
  if (start > partSize) start = partSize;

  let socket;
  try {
    socket = await new Promise((resolve, reject) => {
      const s = net.connect(
        { host: fileInfo.downloadHost, port: fileInfo.downloadPort },
        () => resolve(s)
      );
      s.setTimeout(10000, () => {
        s.destroy();
        reject(new Error("connect timeout"));
      });
      s.on("error", (err) => {
        reject(err);
      });
    });
  } catch (e) {
    return { ok: false, message: `\u8fde\u63a5\u5931\u8d25: ${e.message || e}` };
  }
  socket.setNoDelay(true);
  const reader = new SocketFrameReader(socket);
  try {
    const req = new M.NetworkPacket({
      type: "file_download",
      fileId: fileInfo.fileId,
      token: fileDownloadToken(fileKey, fileInfo.fileId),
      offset: start,
    });
    socket.write(Buffer.from(req.toJson() + "\n", "utf8"));
    let metaLine;
    try {
      metaLine = await reader.readLine(15000);
    } catch (e) {
      return { ok: false, message: "\u53d1\u9001\u65b9\u65e0\u54cd\u5e94" };
    }
    if (metaLine == null) return { ok: false, message: "\u53d1\u9001\u65b9\u65e0\u54cd\u5e94" };
    let meta;
    try {
      const metaJson = C.aesGcmDecrypt(fileKey, C.fromB64(metaLine)).toString("utf8");
      meta = M.NetworkPacket.fromJson(metaJson);
    } catch (e) {
      return { ok: false, message: "\u6587\u4ef6\u5bc6\u94a5\u4e0d\u5339\u914d\u6216\u6570\u636e\u635f\u574f" };
    }
    if (meta.type !== "file_meta" || !meta.fileInfo) {
      return { ok: false, message: "\u65e0\u6548\u7684\u54cd\u5e94" };
    }
    if (meta.fileInfo.fileSize < 0) return { ok: false, message: "\u6587\u4ef6\u5927\u5c0f\u65e0\u6548" };
    if (meta.fileInfo.fileId !== fileInfo.fileId) {
      return { ok: false, message: "\u6587\u4ef6\u4e0d\u5339\u914d" };
    }
    const offerSize =
      fileInfo.fileSize > 0 && fileInfo.fileSize <= MAX_DOWNLOAD_BYTES ? fileInfo.fileSize : 0;
    const metaSize =
      meta.fileInfo.fileSize > 0 && meta.fileInfo.fileSize <= MAX_DOWNLOAD_BYTES
        ? meta.fileInfo.fileSize
        : 0;
    let expected;
    if (offerSize > 0 && metaSize > 0) expected = Math.min(offerSize, metaSize);
    else expected = offerSize || metaSize;
    const progressTotal = expected > 0 ? expected : fileInfo.fileSize;
    if (expected > 0 && start > expected) {
      try {
        fs.unlinkSync(tmpPath);
      } catch (e) {
        /* ignore */
      }
      return { ok: false, message: "\u65ad\u70b9\u6570\u636e\u635f\u574f\uff0c\u8bf7\u91cd\u65b0\u4e0b\u8f7d" };
    }
    let received = start;
    let eof = false;
    const fd = fs.openSync(tmpPath, start > 0 ? "r+" : "w");
    try {
      if (start > 0) fs.ftruncateSync(fd, start);
      while (!eof) {
        let frame;
        try {
          frame = await reader.readFrame(120000);
        } catch (e) {
          return { ok: false, message: "\u6587\u4ef6\u4f20\u8f93\u4e2d\u65ad" };
        }
        if (frame.length === 0) {
          eof = true;
          break;
        }
        if (frame.length < GCM_MIN_FRAME || frame.length > MAX_CHUNK_WIRE) {
          return { ok: false, message: "\u6587\u4ef6\u6570\u636e\u635f\u574f" };
        }
        let plain;
        try {
          plain = C.aesGcmDecrypt(fileKey, frame);
        } catch (e) {
          return { ok: false, message: "\u6587\u4ef6\u6570\u636e\u6821\u9a8c\u5931\u8d25\uff08\u53ef\u80fd\u88ab\u7be1\u6539\uff09" };
        }
        received += plain.length;
        if (expected > 0 && received > expected) {
          return { ok: false, message: "\u6587\u4ef6\u5927\u5c0f\u4e0d\u7b26" };
        }
        if (received > MAX_DOWNLOAD_BYTES) {
          return { ok: false, message: "\u6587\u4ef6\u8d85\u8fc7\u9650\u5236" };
        }
        fs.writeSync(fd, plain, 0, plain.length, received - plain.length);
        if (onProgress) onProgress(received, progressTotal);
      }
    } finally {
      fs.closeSync(fd);
    }
    if (expected > 0 && received !== expected) {
      return { ok: false, message: `\u6587\u4ef6\u4e0d\u5b8c\u6574\uff08${received}/${expected} \u5b57\u8282\uff09` };
    }
    fs.renameSync(tmpPath, targetPath);
    return { ok: true, message: "" };
  } catch (e) {
    return { ok: false, message: `\u4e0b\u8f7d\u5931\u8d25: ${e.message || e}` };
  } finally {
    reader.destroy();
  }
}

module.exports = {
  MAX_DOWNLOAD_BYTES,
  CHUNK_SIZE,
  FILE_SERVER_TTL,
  fileDownloadToken,
  serveDownload,
  OutgoingFileServer,
  downloadFileOffer,
  packFrame,
};
