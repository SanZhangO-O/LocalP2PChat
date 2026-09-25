"use strict";

const fs = require("fs");
const path = require("path");

const U = require("./util");
const { Store } = require("./store");

const UPLOAD_TTL_MS = 24 * 3600 * 1000;
const SNAPSHOT_MESSAGE_WINDOW = 300;

// Server-relayed chat: conversations are stored once on the server and every
// account participant sees the same history. No LAN protocol involved.
class ChatEngine {
  constructor(server, dataDir) {
    this.server = server;
    this.dataDir = dataDir;
    this.store = new Store(dataDir);
    this.uploadsDir = path.join(dataDir, "uploads");
    this.filesDir = path.join(dataDir, "files");
    fs.mkdirSync(this.uploadsDir, { recursive: true });
    fs.mkdirSync(this.filesDir, { recursive: true });
    this.groups = new Map(); // groupId -> group
    this.directs = new Map(); // chatKey -> { key, participants: [idA, idB] }
    this.chats = new Map(); // chatKey -> { messages: [...] } (memory cache)
    this._loadGroups();
    this._loadDirects();
    this._pruneUploads();
  }

  /* ---------------- persistence ---------------- */

  _loadGroups() {
    for (const g of this.store.load("groups.json", [])) {
      if (!g || !g.groupId) continue;
      this.groups.set(g.groupId, {
        groupId: g.groupId,
        name: String(g.name || "").slice(0, U.MAX_NAME_LENGTH),
        announcement: String(g.announcement || "").slice(0, U.MAX_ANNOUNCEMENT_LENGTH),
        creatorId: g.creatorId || "",
        members: new Set(Array.isArray(g.members) ? g.members.filter((m) => typeof m === "string") : []),
        createdAt: g.createdAt || U.nowMs(),
      });
    }
  }

  _saveGroups() {
    const out = Array.from(this.groups.values()).map((g) => ({
      groupId: g.groupId,
      name: g.name,
      announcement: g.announcement,
      creatorId: g.creatorId,
      members: Array.from(g.members),
      createdAt: g.createdAt,
    }));
    this.store.save("groups.json", out);
  }

  _loadDirects() {
    for (const d of this.store.load("directs.json", [])) {
      if (d && d.key && Array.isArray(d.participants) && d.participants.length === 2) {
        this.directs.set(d.key, { key: d.key, participants: d.participants.slice(0, 2) });
      }
    }
  }

  _saveDirects() {
    this.store.save(
      "directs.json",
      Array.from(this.directs.values()).map((d) => ({ key: d.key, participants: d.participants }))
    );
  }

  _messagesFor(key) {
    let chat = this.chats.get(key);
    if (!chat) {
      chat = { messages: this.store.loadChatDecrypted(key).map((m) => new Message(m)) };
      this.chats.set(key, chat);
    }
    return chat.messages;
  }

  _saveChat(key) {
    const messages = this._messagesFor(key).slice(-U.MAX_MESSAGE_HISTORY);
    if (messages.length !== this.chats.get(key).messages.length) {
      this.chats.get(key).messages = messages;
    }
    this.store.saveChat(key, messages.map((m) => m.stripped()));
  }

  _deleteChat(key) {
    this.chats.delete(key);
    this.store.deleteChat(key);
  }

  // uploads are staging for files already moved into the shared store;
  // anything older than a day is dead weight
  _pruneUploads() {
    const cutoff = Date.now() - UPLOAD_TTL_MS;
    let names = [];
    try {
      names = fs.readdirSync(this.uploadsDir);
    } catch (e) {
      return;
    }
    for (const name of names) {
      const p = path.join(this.uploadsDir, name);
      try {
        if (fs.statSync(p).mtimeMs < cutoff) fs.unlinkSync(p);
      } catch (e) {
        /* ignore */
      }
    }
  }

  /* ---------------- helpers ---------------- */

  registry() {
    return this.server.registry;
  }

  account(accountId) {
    return this.registry().get(accountId);
  }

  userName(accountId) {
    const rec = this.account(accountId);
    if (!rec) return "?";
    return (rec.nickname || rec.username || "?").trim() || "?";
  }

  isOnline(accountId) {
    return this.server.isOnline(accountId);
  }

  broadcastTo(accountIds, obj) {
    for (const id of accountIds) {
      this.server.web.broadcast(obj, id);
    }
  }

  pushSnapshots(accountIds) {
    for (const id of accountIds) {
      this.server.web.broadcast({ snapshot: this.snapshotFor(id) }, id);
    }
  }

  pushSnapshotAll() {
    for (const rec of this.registry().accounts.values()) {
      this.server.web.broadcast({ snapshot: this.snapshotFor(rec.id) }, rec.id);
    }
  }

  _conversation(key) {
    if (this.directs.has(key)) return { key, type: "direct", participants: this.directs.get(key).participants };
    const g = this.groups.get(key);
    if (g) return { key, type: "group", participants: Array.from(g.members), group: g };
    return null;
  }

  _requireParticipant(accountId, key) {
    const conv = this._conversation(key);
    if (!conv) throw new Error("会话不存在");
    if (!conv.participants.includes(accountId)) throw new Error("不是该会话的成员");
    return conv;
  }

  _directKeyFor(a, b) {
    return U.directKey(a, b);
  }

  _ensureDirect(a, b) {
    const key = this._directKeyFor(a, b);
    if (!this.directs.has(key)) {
      this.directs.set(key, { key, participants: [a, b] });
      this._saveDirects();
    }
    return key;
  }

  _appendMessage(key, sender, fields) {
    const messages = this._messagesFor(key);
    const msg = new Message({
      id: U.uuid(),
      timestamp: U.nowMs(),
      senderId: sender.id,
      senderName: this.userName(sender.id),
      ...fields,
    });
    messages.push(msg);
    if (messages.length > U.MAX_MESSAGE_HISTORY) {
      this.chats.get(key).messages = messages.slice(-U.MAX_MESSAGE_HISTORY);
    }
    this._saveChat(key);
    return msg;
  }

  _markRead(accountId, key) {
    const messages = this._messagesFor(key);
    const conv = this._conversation(key);
    let changed = false;
    if (conv.type === "direct") {
      for (const m of messages) {
        if (m.senderId !== accountId && !m.read) {
          m.read = true;
          changed = true;
        }
      }
    } else {
      for (const m of messages) {
        if (m.senderId !== accountId && !(m.readers || []).includes(accountId)) {
          m.readers = m.readers || [];
          m.readers.push(accountId);
          changed = true;
        }
      }
    }
    if (changed) this._saveChat(key);
    return changed;
  }

  _toggleReaction(key, messageId, accountId, emoji, active) {
    const messages = this._messagesFor(key);
    const target = messages.find((m) => m.id === messageId);
    if (!target) throw new Error("消息不存在");
    target.reactions = target.reactions || {};
    const list = target.reactions[emoji] || [];
    const idx = list.indexOf(accountId);
    if (active && idx < 0) list.push(accountId);
    if (!active && idx >= 0) list.splice(idx, 1);
    if (list.length) target.reactions[emoji] = list;
    else delete target.reactions[emoji];
    this._saveChat(key);
    return target;
  }

  /* ---------------- presence ---------------- */

  presenceChanged() {
    this.pushSnapshotAll();
  }

  /* ---------------- actions ---------------- */

  async handleAction(conn, action, msg) {
    const me = this.account(conn.accountId);
    if (!me) return;
    switch (action) {
      case "getSnapshot":
        conn.send(JSON.stringify({ snapshot: this.snapshotFor(me.id) }));
        return;
      case "setNickname": {
        const name = String(msg.name || "").trim().slice(0, U.MAX_NAME_LENGTH);
        if (!U.isValidName(name)) throw new Error("昵称不能为空");
        me.nickname = name;
        this.registry().save();
        this.pushSnapshotAll();
        return;
      }
      case "startDirect": {
        let target = null;
        const id = String(msg.userId || "");
        const username = String(msg.username || "").trim();
        if (id) target = this.account(id);
        else if (username) {
          target =
            Array.from(this.registry().accounts.values()).find(
              (a) => a.username === username
            ) || null;
        }
        if (!target) throw new Error("用户不存在");
        if (target.id === me.id) throw new Error("不能和自己聊天");
        const key = this._ensureDirect(me.id, target.id);
        conn.send(JSON.stringify({ startedDirect: { reqId: msg.reqId, chatKey: key, userId: target.id } }));
        this.pushSnapshots([me.id, target.id]);
        return;
      }
      case "removeDirect": {
        const key = String(msg.chatKey || "");
        const conv = this._requireParticipant(me.id, key);
        if (conv.type !== "direct") throw new Error("不是直聊会话");
        this.directs.delete(key);
        this._saveDirects();
        this._deleteChat(key);
        this.pushSnapshots(conv.participants);
        return;
      }
      case "createGroup": {
        const name = String(msg.name || "").trim().slice(0, U.MAX_NAME_LENGTH);
        if (!U.isValidName(name)) throw new Error("群名不能为空");
        const group = {
          groupId: U.uuid(),
          name,
          announcement: "",
          creatorId: me.id,
          members: new Set([me.id]),
          createdAt: U.nowMs(),
        };
        for (const username of Array.isArray(msg.members) ? msg.members : []) {
          const rec = Array.from(this.registry().accounts.values()).find(
            (a) => a.username === String(username || "")
          );
          if (rec && rec.id !== me.id) group.members.add(rec.id);
        }
        this.groups.set(group.groupId, group);
        this._saveGroups();
        conn.send(
          JSON.stringify({ createdGroup: { reqId: msg.reqId, groupId: group.groupId, name: group.name } })
        );
        this.pushSnapshots(Array.from(group.members));
        return;
      }
      case "inviteMember": {
        const group = this.groups.get(String(msg.groupId || ""));
        if (!group) throw new Error("群组不存在");
        if (!group.members.has(me.id)) throw new Error("不是该群的成员");
        const username = String(msg.username || "").trim();
        const rec = Array.from(this.registry().accounts.values()).find(
          (a) => a.username === username
        );
        if (!rec) throw new Error("用户不存在");
        if (group.members.has(rec.id)) throw new Error("已在群中");
        group.members.add(rec.id);
        this._saveGroups();
        this.pushSnapshots(Array.from(group.members));
        return;
      }
      case "kickMember": {
        const group = this.groups.get(String(msg.groupId || ""));
        if (!group) throw new Error("群组不存在");
        if (!group.members.has(me.id)) throw new Error("不是该群的成员");
        if (group.creatorId !== me.id) throw new Error("只有群主可以移出成员");
        const targetId = String(msg.targetId || "");
        if (targetId === group.creatorId) throw new Error("不能移出群主");
        if (!group.members.has(targetId)) throw new Error("不是群成员");
        group.members.delete(targetId);
        this._saveGroups();
        this.broadcastTo([targetId], { event: `你已被群主移出群组「${group.name}」` });
        this.pushSnapshots(Array.from(group.members).concat([targetId]));
        return;
      }
      case "groupUpdate": {
        const group = this.groups.get(String(msg.groupId || ""));
        if (!group) throw new Error("群组不存在");
        if (!group.members.has(me.id)) throw new Error("不是该群的成员");
        if (group.creatorId !== me.id) throw new Error("只有群主可以修改群信息");
        if (msg.name != null) {
          const name = String(msg.name).trim().slice(0, U.MAX_NAME_LENGTH);
          if (!U.isValidName(name)) throw new Error("群名不能为空");
          group.name = name;
        }
        if (msg.announcement != null) {
          group.announcement = String(msg.announcement).slice(0, U.MAX_ANNOUNCEMENT_LENGTH);
        }
        this._saveGroups();
        this.pushSnapshots(Array.from(group.members));
        return;
      }
      case "leaveGroup": {
        const group = this.groups.get(String(msg.groupId || ""));
        if (!group) throw new Error("群组不存在");
        if (!group.members.has(me.id)) throw new Error("不是该群的成员");
        if (group.creatorId === me.id) {
          // the creator leaving dissolves the group for everyone
          const members = Array.from(group.members);
          this.groups.delete(group.groupId);
          this._saveGroups();
          this._deleteChat(group.groupId);
          this.broadcastTo(members, { event: `群组「${group.name}」已解散` });
          this.pushSnapshots(members);
          return;
        }
        group.members.delete(me.id);
        this._saveGroups();
        this.pushSnapshots(Array.from(group.members).concat([me.id]));
        return;
      }
      case "sendChat": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        const content = String(msg.content || "");
        if (!U.isValidContent(content)) throw new Error("消息为空或过长");
        this._appendMessage(key, me, { content });
        this._pushChat(key);
        return;
      }
      case "sendFile": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        const uploadId = String(msg.uploadId || "");
        const source = this._uploadPath(uploadId);
        if (!fs.existsSync(source)) throw new Error("上传文件不存在");
        const fileId = U.uuid();
        const rawName = String(uploadId).slice(uploadId.indexOf("_") + 1) || "file";
        const fileName = U.sanitizeFileName(rawName);
        const target = path.join(this.filesDir, `${U.sanitizeFileId(fileId)}_${fileName}`);
        fs.renameSync(source, target);
        let size = 0;
        try {
          size = fs.statSync(target).size;
        } catch (e) {
          /* ignore */
        }
        this._appendMessage(key, me, {
          fileInfo: {
            fileId,
            fileName,
            fileSize: size,
            kind: U.detectMediaKind(fileName),
          },
        });
        this._pushChat(key);
        return;
      }
      case "sendTyping": {
        const key = String(msg.chatKey || "");
        const conv = this._requireParticipant(me.id, key);
        const active = Boolean(msg.active);
        const others = conv.participants.filter((id) => id !== me.id);
        this.broadcastTo(others, {
          typing: { chatKey: key, senderId: me.id, senderName: this.userName(me.id), active },
        });
        return;
      }
      case "openChat": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        if (this._markRead(me.id, key)) this._pushChat(key);
        return;
      }
      case "editMessage": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        const content = String(msg.content || "");
        if (!U.isValidContent(content)) throw new Error("消息为空或过长");
        const target = this._messagesFor(key).find((m) => m.id === String(msg.messageId || ""));
        if (!target) throw new Error("消息不存在");
        if (target.senderId !== me.id) throw new Error("只能编辑自己的消息");
        if (target.fileInfo) throw new Error("文件消息不能编辑");
        target.content = content;
        target.edited = true;
        this._saveChat(key);
        this._pushChat(key);
        return;
      }
      case "deleteMessage": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        const messages = this._messagesFor(key);
        const idx = messages.findIndex((m) => m.id === String(msg.messageId || ""));
        if (idx < 0) throw new Error("消息不存在");
        if (messages[idx].senderId !== me.id) throw new Error("只能删除自己的消息");
        messages.splice(idx, 1);
        this._saveChat(key);
        this._pushChat(key);
        return;
      }
      case "react": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        const emoji = U.sanitizeEmoji(String(msg.emoji || ""));
        if (!emoji) throw new Error("无效的表情");
        this._toggleReaction(key, String(msg.messageId || ""), me.id, emoji, Boolean(msg.active));
        this._pushChat(key);
        return;
      }
      case "pin": {
        const key = String(msg.chatKey || "");
        this._requireParticipant(me.id, key);
        const target = this._messagesFor(key).find((m) => m.id === String(msg.messageId || ""));
        if (!target) throw new Error("消息不存在");
        const active = Boolean(msg.active);
        target.pinned = active;
        target.pinnedBy = active ? me.id : null;
        this._saveChat(key);
        this._pushChat(key);
        return;
      }
      default:
        return;
    }
  }

  _pushChat(key) {
    const conv = this._conversation(key);
    if (conv) this.pushSnapshots(conv.participants);
  }

  /* ---------------- files ---------------- */

  _uploadPath(uploadId) {
    const safe = U.sanitizeFileName(String(uploadId));
    return path.join(this.uploadsDir, safe);
  }

  serveFile(res, fileId) {
    const safeId = U.sanitizeFileId(fileId);
    const prefix = safeId + "_";
    // partial writes must never be served; look at what is actually on disk
    let match = null;
    try {
      for (const name of fs.readdirSync(this.filesDir)) {
        if (name.startsWith(prefix)) {
          match = path.join(this.filesDir, name);
          break;
        }
      }
    } catch (e) {
      match = null;
    }
    if (!match) {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    const displayName = path.basename(match).slice(safeId.length + 1);
    const headers = {
      "Content-Type": "application/octet-stream",
      "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(displayName)}`,
    };
    try {
      headers["Content-Length"] = String(fs.statSync(match).size);
    } catch (e) {
      /* ignore */
    }
    res.writeHead(200, headers);
    const stream = fs.createReadStream(match);
    stream.on("error", () => {
      try {
        res.destroy();
      } catch (e) {
        /* ignore */
      }
    });
    stream.pipe(res);
  }

  /* ---------------- snapshot ---------------- */

  snapshotFor(accountId) {
    const me = this.account(accountId);
    if (!me) return null;
    const users = [];
    for (const rec of this.registry().accounts.values()) {
      users.push({
        id: rec.id,
        username: rec.username,
        name: (rec.nickname || rec.username || "?").trim() || "?",
        online: this.isOnline(rec.id),
      });
    }
    const groups = [];
    for (const g of this.groups.values()) {
      if (!g.members.has(accountId)) continue;
      groups.push({
        groupId: g.groupId,
        name: g.name,
        announcement: g.announcement || "",
        creatorId: g.creatorId,
        creatorName: this.userName(g.creatorId),
        members: Array.from(g.members).map((id) => ({
          id,
          name: this.userName(id),
          online: this.isOnline(id),
        })),
      });
    }
    const directs = [];
    const chats = {};
    for (const d of this.directs.values()) {
      if (!d.participants.includes(accountId)) continue;
      const otherId = d.participants[0] === accountId ? d.participants[1] : d.participants[0];
      const other = this.account(otherId);
      if (!other) continue;
      directs.push({
        key: d.key,
        userId: otherId,
        name: this.userName(otherId),
        online: this.isOnline(otherId),
      });
      chats[d.key] = this._chatView(d.key, accountId);
    }
    for (const g of this.groups.values()) {
      if (g.members.has(accountId)) chats[g.groupId] = this._chatView(g.groupId, accountId);
    }
    return {
      profile: {
        userId: me.id,
        username: me.username,
        name: (me.nickname || me.username || "?").trim() || "?",
      },
      users,
      directs,
      groups,
      chats,
    };
  }

  _chatView(key, accountId) {
    const messages = this._messagesFor(key);
    return {
      key,
      messages: messages.slice(-SNAPSHOT_MESSAGE_WINDOW).map((m) => m.clientView()),
    };
  }
}

class Message {
  constructor(doc) {
    this.id = String(doc.id || U.uuid());
    this.content = typeof doc.content === "string" ? doc.content : "";
    this.timestamp = Number(doc.timestamp) || U.nowMs();
    this.senderId = String(doc.senderId || "");
    this.senderName = String(doc.senderName || "?").slice(0, U.MAX_NAME_LENGTH);
    this.edited = Boolean(doc.edited);
    this.fileInfo = doc.fileInfo
      ? {
          fileId: String(doc.fileInfo.fileId || ""),
          fileName: String(doc.fileInfo.fileName || "file"),
          fileSize: Number(doc.fileInfo.fileSize) || 0,
          kind: U.detectMediaKind(String(doc.fileInfo.fileName || "")),
        }
      : null;
    this.reactions = doc.reactions && typeof doc.reactions === "object" ? doc.reactions : null;
    this.pinned = Boolean(doc.pinned);
    this.pinnedBy = doc.pinnedBy || null;
    this.readers = Array.isArray(doc.readers) ? doc.readers : null;
    this.read = Boolean(doc.read);
  }

  stripped() {
    const out = {
      id: this.id,
      content: this.content,
      timestamp: this.timestamp,
      senderId: this.senderId,
      senderName: this.senderName,
    };
    if (this.fileInfo) out.fileInfo = { ...this.fileInfo };
    if (this.edited) out.edited = true;
    if (this.reactions && Object.keys(this.reactions).length) out.reactions = this.reactions;
    if (this.pinned) out.pinned = true;
    if (this.readers && this.readers.length) out.readers = this.readers;
    if (this.read) out.read = true;
    return out;
  }

  clientView() {
    return {
      id: this.id,
      content: this.content,
      timestamp: this.timestamp,
      senderId: this.senderId,
      senderName: this.senderName,
      edited: this.edited,
      fileInfo: this.fileInfo,
      reactions: this.reactions || {},
      pinned: this.pinned,
      pinnedBy: this.pinnedBy,
      readers: this.readers || [],
      read: this.read,
    };
  }
}

module.exports = { ChatEngine };
