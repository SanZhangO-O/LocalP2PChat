"use strict";

const os = require("os");
const { randomUUID } = require("crypto");

const MAX_CONTENT_LENGTH = 5000;
const MAX_NAME_LENGTH = 64;
const MAX_ANNOUNCEMENT_LENGTH = 500;
const MAX_EMOJI_LEN = 16;
const MAX_MESSAGE_HISTORY = 2000;

const IMAGE_EXTENSIONS = new Set([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"]);
const VIDEO_EXTENSIONS = new Set([".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"]);
const AUDIO_EXTENSIONS = new Set([".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".mp3", ".flac"]);

const FILE_KIND_FILE = "file";
const FILE_KIND_IMAGE = "image";
const FILE_KIND_VIDEO = "video";
const FILE_KIND_AUDIO = "audio";

function uuid() {
  return randomUUID();
}

function nowMs() {
  return Date.now();
}

// IPv4 addresses of this machine that other LAN devices can reach
function lanAddresses() {
  const out = [];
  const ifaces = os.networkInterfaces();
  for (const name of Object.keys(ifaces)) {
    for (const iface of ifaces[name] || []) {
      if (iface.family === "IPv4" && !iface.internal) out.push(iface.address);
    }
  }
  return out;
}

function codePointsOf(text) {
  return Array.from(text);
}

function isValidContent(content) {
  if (typeof content !== "string" || !content.trim()) return false;
  // count code points, not UTF-16 units
  return codePointsOf(content).length <= MAX_CONTENT_LENGTH;
}

function isValidName(name) {
  if (typeof name !== "string") return false;
  const trimmed = name.trim();
  return trimmed.length > 0 && codePointsOf(trimmed).length <= MAX_NAME_LENGTH;
}

function detectMediaKind(name) {
  const idx = name.lastIndexOf(".");
  const ext = idx >= 0 ? name.slice(idx).toLowerCase() : "";
  if (IMAGE_EXTENSIONS.has(ext)) return FILE_KIND_IMAGE;
  if (VIDEO_EXTENSIONS.has(ext)) return FILE_KIND_VIDEO;
  if (AUDIO_EXTENSIONS.has(ext)) return FILE_KIND_AUDIO;
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

const ID_CHARS = new Set(
  "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_".split("")
);

// fileId/uploadId values feed local save paths: keep only the safe
// uuid-like alphabet so a crafted id can never escape its directory
function sanitizeFileId(value) {
  let out = "";
  for (const ch of String(value == null ? "" : value)) {
    if (ID_CHARS.has(ch)) out += ch;
    if (out.length >= 64) break;
  }
  return out || "file";
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

// direct conversation key for a pair of account ids (order independent)
function directKey(a, b) {
  const [x, y] = [String(a), String(b)].sort();
  return `direct:${x}|${y}`;
}

module.exports = {
  MAX_CONTENT_LENGTH,
  MAX_NAME_LENGTH,
  MAX_ANNOUNCEMENT_LENGTH,
  MAX_EMOJI_LEN,
  MAX_MESSAGE_HISTORY,
  FILE_KIND_FILE,
  FILE_KIND_IMAGE,
  FILE_KIND_VIDEO,
  FILE_KIND_AUDIO,
  uuid,
  nowMs,
  lanAddresses,
  codePointsOf,
  isValidContent,
  isValidName,
  detectMediaKind,
  sanitizeFileName,
  sanitizeFileId,
  sanitizeEmoji,
  directKey,
};
