"use strict";

const fs = require("fs");
const path = require("path");

const C = require("./crypto");

function atomicWriteJson(filePath, obj) {
  const tmp = filePath + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(obj), "utf8");
  fs.renameSync(tmp, filePath);
}

function readJson(filePath, fallback) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch (e) {
    return fallback;
  }
}

class SecretBox {
  constructor(dataDir) {
    this.filePath = path.join(dataDir, "secret_key.json");
    let doc = readJson(this.filePath, null);
    if (doc && doc.k) {
      try {
        this.key = C.fromB64(doc.k);
      } catch (e) {
        this.key = null;
      }
    } else {
      this.key = null;
    }
    if (!this.key || this.key.length !== C.KEY_LEN) {
      this.key = C.randomBytes(C.KEY_LEN);
      atomicWriteJson(this.filePath, { scheme: "plain", k: C.toB64(this.key) });
    }
  }

  protect(text) {
    if (!text) return text;
    try {
      return "enc1:" + C.toB64(C.aesGcmEncrypt(this.key, Buffer.from(String(text), "utf8")));
    } catch (e) {
      return text;
    }
  }

  unprotect(text) {
    if (!text || !String(text).startsWith("enc1:")) return text;
    try {
      return C.aesGcmDecrypt(this.key, C.fromB64(String(text).slice(5))).toString("utf8");
    } catch (e) {
      return "";
    }
  }
}

class Store {
  constructor(dataDir) {
    this.dataDir = dataDir;
    this.chatsDir = path.join(dataDir, "chats");
    fs.mkdirSync(this.chatsDir, { recursive: true });
    this.secretBox = new SecretBox(dataDir);
  }

  _path(name) {
    return path.join(this.dataDir, name);
  }

  load(name, fallback) {
    return readJson(this._path(name), fallback);
  }

  save(name, obj) {
    atomicWriteJson(this._path(name), obj);
  }

  chatPath(key) {
    const safe = Buffer.from(key, "utf8").toString("hex");
    return path.join(this.chatsDir, safe + ".json");
  }

  loadChat(key) {
    const doc = readJson(this.chatPath(key), null);
    if (!doc || !Array.isArray(doc.messages)) return [];
    return doc.messages;
  }

  saveChat(key, messages) {
    const msgs = messages.map((m) => {
      const out = Object.assign({}, m);
      if (out.content != null && !(typeof out.content === "string" && out.content.startsWith("enc1:"))) {
        out.content = this.secretBox.protect(out.content);
      }
      if (out.replyPreview) out.replyPreview = this.secretBox.protect(out.replyPreview);
      return out;
    });
    atomicWriteJson(this.chatPath(key), { messages: msgs });
  }

  loadChatDecrypted(key) {
    return this.loadChat(key).map((m) => {
      const out = Object.assign({}, m);
      if (typeof out.content === "string" && out.content.startsWith("enc1:")) {
        out.content = this.secretBox.unprotect(out.content);
      }
      if (typeof out.replyPreview === "string" && out.replyPreview.startsWith("enc1:")) {
        out.replyPreview = this.secretBox.unprotect(out.replyPreview);
      }
      return out;
    });
  }

  deleteChat(key) {
    try {
      fs.unlinkSync(this.chatPath(key));
    } catch (e) {
      /* ignore */
    }
  }

  protect(text) {
    return this.secretBox.protect(text);
  }

  unprotect(text) {
    return this.secretBox.unprotect(text);
  }
}

module.exports = { Store, SecretBox, atomicWriteJson, readJson };
