"use strict";

const os = require("os");
const { randomUUID } = require("crypto");

const MAX_CONTENT_LENGTH = 5000;
const MAX_LINE_LENGTH = 64 * 1024;
const TCP_PORT = 9999;
const MAX_REPLY_PREVIEW = 120;
const MAX_QUOTE_FIELD_LEN = 128;
const MENTION_ALL = "all";
const MAX_MENTIONS = 64;
const MAX_MENTION_ID_LEN = 128;
const MAX_EMOJI_LEN = 16;
const MAX_GROUP_FILES = 500;
const MAX_GROUP_FILE_REMOVED = 500;
const MAX_FILE_ID_LEN = 128;
const MAX_SENDER_NAME_LEN = 64;
const MAX_DELETED_IDS = 200;
const MAX_DELETED_ID_LEN = 128;
const MAX_FOLDER_FILES = 1000;
const MAX_RELATIVE_PATH_LENGTH = 1024;

const IMAGE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"]);
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"]);
const AUDIO_EXTENSIONS = new Set([".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".mp3", ".flac"]);

const FILE_KIND_FILE = "file";
const FILE_KIND_IMAGE = "image";
const FILE_KIND_VIDEO = "video";
const FILE_KIND_AUDIO = "audio";
const CONTENT_KIND_TEXT = "text";

function uuid() {
  return randomUUID();
}

function nowMs() {
  return Date.now();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function getLocalIpAddress() {
  const ifaces = os.networkInterfaces();
  for (const name of Object.keys(ifaces)) {
    for (const iface of ifaces[name] || []) {
      if (iface.family === "IPv4" && !iface.internal) {
        return iface.address;
      }
    }
  }
  try {
    const candidate = os.hostname();
    if (candidate && !candidate.startsWith("127.")) {
      return candidate;
    }
  } catch (e) {
    /* fallthrough */
  }
  return "";
}

function numericGroupIdOf(groupName, fingerprint) {
  let hash = 0x811c9dc5;
  const units = Buffer.from(`${groupName}\u0000${fingerprint}`, "utf16le");
  for (let i = 0; i + 1 < units.length; i += 2) {
    const unit = units[i] | (units[i + 1] << 8);
    hash ^= unit;
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  const digits = ((hash % 100000000) + 100000000) % 100000000;
  return String(digits).padStart(8, "0");
}

function formatNumericGroupId(groupId) {
  const out = [];
  for (let i = 0; i < groupId.length; i += 4) {
    out.push(groupId.slice(i, i + 4));
  }
  return out.join(" ");
}

function codePointsOf(text) {
  return Array.from(text);
}

function isValidContent(content) {
  return Boolean(content && content.trim()) && content.length <= MAX_CONTENT_LENGTH;
}

function detectMediaKind(name) {
  const idx = name.lastIndexOf(".");
  const ext = idx >= 0 ? name.slice(idx).toLowerCase() : "";
  if (IMAGE_EXTENSIONS.has(ext)) return FILE_KIND_IMAGE;
  if (VIDEO_EXTENSIONS.has(ext)) return FILE_KIND_VIDEO;
  if (AUDIO_EXTENSIONS.has(ext)) return FILE_KIND_AUDIO;
  return FILE_KIND_FILE;
}

function normalizeMediaKind(kind) {
  if (kind === FILE_KIND_IMAGE || kind === FILE_KIND_VIDEO || kind === FILE_KIND_AUDIO) {
    return kind;
  }
  return FILE_KIND_FILE;
}

function sanitizeFileName(name) {
  let text = String(name == null ? "" : name).replace(/\\/g, "/");
  const idx = text.lastIndexOf("/");
  if (idx >= 0) text = text.slice(idx + 1);
  let out = "";
  for (const ch of text) {
    const cp = ch.codePointAt(0);
    if (cp < 32 || (cp >= 0x7f && cp < 0xa0)) continue;
    out += ch;
  }
  out = out.replace(/[ .]+$/u, "");
  if (out.length > 255) {
    out = out.slice(0, 255).replace(/[ .]+$/u, "");
  }
  return out || "file";
}

const FOLDER_ID_CHARS = new Set(
  "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_".split("")
);

// fileId values are sender-supplied and get combined into local save paths:
// keep only the safe uuid-like alphabet (parity with sanitizeFolderId) so a
// crafted id ("..", separators, drive letters) can never escape the
// downloads directory.
function sanitizeFileId(value) {
  let out = "";
  for (const ch of String(value == null ? "" : value)) {
    if (FOLDER_ID_CHARS.has(ch)) out += ch;
    if (out.length >= 64) break;
  }
  return out || "file";
}

function sanitizeFolderId(value) {
  let out = "";
  for (const ch of String(value == null ? "" : value)) {
    if (FOLDER_ID_CHARS.has(ch)) out += ch;
    if (out.length >= 64) break;
  }
  return out;
}

function sanitizeRelativePath(value) {
  const text = String(value == null ? "" : value).replace(/\\/g, "/");
  const parts = [];
  for (const seg of text.split("/")) {
    if (!seg || seg === "." || seg === "..") continue;
    const clean = sanitizeFileName(seg).replace(/:/g, "");
    if (clean) parts.push(clean);
    if (parts.length >= 64) break;
  }
  let out = parts.join("/");
  if (out.length > MAX_RELATIVE_PATH_LENGTH) {
    out = out.slice(0, MAX_RELATIVE_PATH_LENGTH).replace(/[/ .]+$/u, "");
  }
  return out;
}

function isEmojiOnlyPrintable(ch) {
  const cp = ch.codePointAt(0);
  if (cp < 0x20 || (cp >= 0x7f && cp < 0xa0)) return false;
  return !/\s/u.test(ch);
}

function sanitizeEmoji(value) {
  const text = String(value == null ? "" : value);
  let out = "";
  for (const ch of codePointsOf(text)) {
    if (isEmojiOnlyPrintable(ch) && !/\s/u.test(ch)) {
      out += ch;
    }
    if (out.length >= MAX_EMOJI_LEN * 2) break;
  }
  const cps = codePointsOf(out);
  return cps.slice(0, MAX_EMOJI_LEN).join("");
}

function sanitizeMentions(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const item of raw) {
    if (typeof item !== "string") continue;
    const entry = item.slice(0, MAX_MENTION_ID_LEN);
    if (entry && !out.includes(entry)) out.push(entry);
    if (out.length >= MAX_MENTIONS) break;
  }
  return out;
}

function makeQuotePreview(text) {
  const flat = String(text == null ? "" : text).replace(/\n/g, " ").trim();
  const cps = codePointsOf(flat);
  if (cps.length <= MAX_REPLY_PREVIEW) return flat;
  return cps.slice(0, MAX_REPLY_PREVIEW).join("") + "\u2026";
}

function sanitizeIdList(ids, cap, maxLen = MAX_DELETED_ID_LEN) {
  if (typeof ids === "string") ids = [ids];
  const out = [];
  const seen = new Set();
  for (const raw of ids || []) {
    const mid = String(raw);
    if (!mid.trim() || mid.length > maxLen || seen.has(mid)) continue;
    seen.add(mid);
    out.push(mid);
    if (out.length >= cap) break;
  }
  return out;
}

function sanitizeDeletedIds(deletedIds) {
  return sanitizeIdList(deletedIds, MAX_DELETED_IDS);
}

function sanitizeGroupFiles(raw, groupFileInfoFromDict) {
  if (!Array.isArray(raw)) {
    if (raw && typeof raw === "object") raw = [raw];
    else raw = [];
  }
  const out = [];
  const seen = new Set();
  for (const item of raw) {
    if (!item || typeof item !== "object" || Array.isArray(item)) continue;
    let entry;
    try {
      entry = groupFileInfoFromDict(item);
    } catch (e) {
      continue;
    }
    if (seen.has(entry.fileId)) continue;
    seen.add(entry.fileId);
    out.push(entry);
    if (out.length >= MAX_GROUP_FILES) break;
  }
  return out;
}

function canRemoveGroupFile(senderId, entrySenderId, creatorId) {
  if (!senderId) return false;
  return senderId === entrySenderId || (Boolean(creatorId) && senderId === creatorId);
}

function strictInt(value, field) {
  if (typeof value === "boolean") throw new Error(`${field} must be an integer`);
  if (Number.isInteger(value) && typeof value === "number") return value;
  if (typeof value === "string" && value.length > 0 && /^[0-9]+$/.test(value)) {
    return parseInt(value, 10);
  }
  throw new Error(`${field} must be an integer`);
}

function strictSize(value, field) {
  const size = strictInt(value, field);
  if (size < 0) throw new Error(`${field} must be non-negative`);
  return size;
}

function strictPort(value, field) {
  const port = strictInt(value, field);
  if (port < 0 || port > 65535) throw new Error(`${field} must be a port in 0..65535`);
  return port;
}

function baseName(p) {
  const norm = String(p).replace(/\\/g, "/");
  const idx = norm.lastIndexOf("/");
  return idx >= 0 ? norm.slice(idx + 1) : norm;
}

function parseHostPort(host, defaultPort = TCP_PORT) {
  let text = String(host || "").trim();
  text = text.replace(/\uff1a/g, ":").replace(/\uff0e/g, ".");
  if (text.includes(":")) {
    const idx = text.lastIndexOf(":");
    const tail = text.slice(idx + 1).trim();
    if (/^[0-9]+$/.test(tail)) {
      return [text.slice(0, idx).trim(), parseInt(tail, 10)];
    }
  }
  return [text, defaultPort];
}

module.exports = {
  MAX_CONTENT_LENGTH,
  MAX_LINE_LENGTH,
  TCP_PORT,
  MAX_REPLY_PREVIEW,
  MAX_QUOTE_FIELD_LEN,
  MENTION_ALL,
  MAX_MENTIONS,
  MAX_MENTION_ID_LEN,
  MAX_EMOJI_LEN,
  MAX_GROUP_FILES,
  MAX_GROUP_FILE_REMOVED,
  MAX_FILE_ID_LEN,
  MAX_SENDER_NAME_LEN,
  MAX_DELETED_IDS,
  MAX_DELETED_ID_LEN,
  MAX_FOLDER_FILES,
  MAX_RELATIVE_PATH_LENGTH,
  FILE_KIND_FILE,
  FILE_KIND_IMAGE,
  FILE_KIND_VIDEO,
  FILE_KIND_AUDIO,
  CONTENT_KIND_TEXT,
  uuid,
  nowMs,
  sleep,
  getLocalIpAddress,
  numericGroupIdOf,
  formatNumericGroupId,
  codePointsOf,
  isValidContent,
  detectMediaKind,
  normalizeMediaKind,
  sanitizeFileName,
  sanitizeFolderId,
  sanitizeFileId,
  sanitizeRelativePath,
  sanitizeEmoji,
  sanitizeMentions,
  makeQuotePreview,
  sanitizeIdList,
  sanitizeDeletedIds,
  sanitizeGroupFiles,
  canRemoveGroupFile,
  strictInt,
  strictSize,
  strictPort,
  baseName,
  parseHostPort,
};
