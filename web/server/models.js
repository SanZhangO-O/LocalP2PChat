"use strict";

const U = require("./util");

class Peer {
  constructor(id, name, ipAddress, port) {
    this.id = id || "";
    this.name = name || "";
    this.ipAddress = ipAddress || "";
    this.port = port || 0;
  }

  toDict() {
    return { id: this.id, name: this.name, ipAddress: this.ipAddress, port: this.port };
  }

  static fromDict(d) {
    return new Peer(
      String(d.id == null ? "" : d.id),
      String(d.name == null ? "" : d.name),
      String(d.ipAddress == null ? "" : d.ipAddress),
      U.strictPort(d.port == null ? 0 : d.port, "peer port")
    );
  }
}

class FileInfo {
  constructor(
    fileId,
    fileName,
    fileSize,
    downloadHost,
    downloadPort,
    fileKey = "",
    kind = U.FILE_KIND_FILE,
    folderId = "",
    folderName = "",
    relativePath = "",
    folderTotal = 0
  ) {
    this.fileId = fileId;
    this.fileName = fileName;
    this.fileSize = fileSize;
    this.downloadHost = downloadHost;
    this.downloadPort = downloadPort;
    this.fileKey = fileKey;
    this.kind = kind;
    this.folderId = folderId;
    this.folderName = folderName;
    this.relativePath = relativePath;
    this.folderTotal = folderTotal;
  }

  toDict() {
    const d = {
      fileId: this.fileId,
      fileName: this.fileName,
      fileSize: this.fileSize,
      downloadHost: this.downloadHost,
      downloadPort: this.downloadPort,
    };
    if (this.fileKey) d.fileKey = this.fileKey;
    if (this.kind !== U.FILE_KIND_FILE) d.kind = this.kind;
    if (this.folderId) {
      d.folderId = this.folderId;
      if (this.folderName) d.folderName = this.folderName;
      if (this.relativePath) d.relativePath = this.relativePath;
      if (this.folderTotal > 0) d.folderTotal = this.folderTotal;
    }
    return d;
  }

  static fromDict(d) {
    let folderTotal = U.strictSize(d.folderTotal == null ? 0 : d.folderTotal, "folderTotal");
    if (folderTotal > U.MAX_FOLDER_FILES) folderTotal = 0;
    return new FileInfo(
      String(d.fileId == null ? "" : d.fileId),
      U.sanitizeFileName(String(d.fileName == null ? "" : d.fileName)),
      U.strictSize(d.fileSize == null ? 0 : d.fileSize, "fileSize"),
      String(d.downloadHost == null ? "" : d.downloadHost),
      U.strictPort(d.downloadPort == null ? 0 : d.downloadPort, "downloadPort"),
      String(d.fileKey == null ? "" : d.fileKey),
      U.normalizeMediaKind(String(d.kind == null ? U.FILE_KIND_FILE : d.kind)),
      U.sanitizeFolderId(d.folderId == null ? "" : d.folderId),
      d.folderName ? U.sanitizeFileName(String(d.folderName)) : "",
      U.sanitizeRelativePath(d.relativePath == null ? "" : d.relativePath),
      folderTotal
    );
  }
}

class ForwardedInfo {
  constructor(originSender, originGroup = "", originTime = 0) {
    this.originSender = originSender;
    this.originGroup = originGroup;
    this.originTime = originTime;
  }

  toDict() {
    const d = { originSender: this.originSender };
    if (this.originGroup) d.originGroup = this.originGroup;
    if (this.originTime) d.originTime = this.originTime;
    return d;
  }

  static fromDict(d) {
    let originTime = d.originTime == null ? 0 : d.originTime;
    originTime = Number(originTime);
    if (!Number.isFinite(originTime)) originTime = 0;
    return new ForwardedInfo(
      String(d.originSender == null ? "" : d.originSender).slice(0, U.MAX_QUOTE_FIELD_LEN),
      String(d.originGroup == null ? "" : d.originGroup).slice(0, U.MAX_QUOTE_FIELD_LEN),
      Math.max(0, Math.trunc(originTime))
    );
  }
}

function parseForwarded(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  try {
    return ForwardedInfo.fromDict(raw);
  } catch (e) {
    return null;
  }
}

class GroupFileInfo {
  constructor(
    fileId,
    name,
    size,
    senderId,
    senderName = "",
    ts = 0,
    downloadHost = "",
    downloadPort = 0,
    fileKey = ""
  ) {
    this.fileId = fileId;
    this.name = name;
    this.size = size;
    this.senderId = senderId;
    this.senderName = senderName;
    this.ts = ts;
    this.downloadHost = downloadHost;
    this.downloadPort = downloadPort;
    this.fileKey = fileKey;
  }

  toDict() {
    const d = {
      fileId: this.fileId,
      name: this.name,
      size: this.size,
      senderId: this.senderId,
    };
    if (this.senderName) d.senderName = this.senderName;
    if (this.ts) d.ts = this.ts;
    if (this.downloadHost) d.downloadHost = this.downloadHost;
    if (this.downloadPort) d.downloadPort = this.downloadPort;
    if (this.fileKey) d.fileKey = this.fileKey;
    return d;
  }

  static fromDict(d) {
    const fileId = String(d.fileId == null ? "" : d.fileId).slice(0, U.MAX_FILE_ID_LEN);
    if (!fileId) throw new Error("group file entry missing fileId");
    const senderId = String(d.senderId == null ? "" : d.senderId).slice(0, U.MAX_MENTION_ID_LEN);
    const name = U.sanitizeFileName(String(d.name == null ? "" : d.name));
    if (!name) throw new Error("group file entry missing name");
    return new GroupFileInfo(
      fileId,
      name,
      U.strictSize(d.size == null ? 0 : d.size, "size"),
      senderId,
      String(d.senderName == null ? "" : d.senderName).slice(0, U.MAX_SENDER_NAME_LEN),
      U.strictInt(d.ts == null ? 0 : d.ts, "ts"),
      String(d.downloadHost == null ? "" : d.downloadHost),
      U.strictPort(d.downloadPort == null ? 0 : d.downloadPort, "downloadPort"),
      String(d.fileKey == null ? "" : d.fileKey).slice(0, 128)
    );
  }
}

class ChatMessage {
  constructor(fields) {
    this.id = fields.id;
    this.content = fields.content;
    this.timestamp = fields.timestamp;
    this.senderId = fields.senderId;
    this.senderName = fields.senderName;
    this.isFromMe = Boolean(fields.isFromMe);
    this.fileInfo = fields.fileInfo || null;
    this.replyTo = fields.replyTo != null ? fields.replyTo : null;
    this.replyPreview = fields.replyPreview != null ? fields.replyPreview : null;
    this.replySender = fields.replySender != null ? fields.replySender : null;
    this.quoteSender = fields.quoteSender != null ? fields.quoteSender : null;
    this.quoteKind = fields.quoteKind != null ? fields.quoteKind : null;
    this.forwarded = fields.forwarded || null;
    this.mentions = fields.mentions && fields.mentions.length ? fields.mentions : null;
    this.edited = Boolean(fields.edited);
    this.senderPubId = fields.senderPubId != null ? fields.senderPubId : null;
    this.senderSig = fields.senderSig != null ? fields.senderSig : null;
    this.pending = Boolean(fields.pending);
    this.read = Boolean(fields.read);
  }

  toDict() {
    const d = {
      id: this.id,
      content: this.content,
      timestamp: this.timestamp,
      senderId: this.senderId,
      senderName: this.senderName,
    };
    if (this.fileInfo) d.fileInfo = this.fileInfo.toDict();
    if (this.replyTo != null) {
      d.replyTo = this.replyTo;
      if (this.replyPreview != null) d.replyPreview = this.replyPreview;
      if (this.replySender != null) d.replySender = this.replySender;
      const quote = { quotedId: this.replyTo, quotedSender: this.quoteSender || "" };
      const kind = this.quoteKind || U.CONTENT_KIND_TEXT;
      if (kind !== U.CONTENT_KIND_TEXT) quote.quotedKind = kind;
      if (this.replySender) quote.quotedName = this.replySender;
      if (this.replyPreview) quote.quotedPreview = this.replyPreview;
      d.quote = quote;
    }
    if (this.forwarded) d.forwarded = this.forwarded.toDict();
    if (this.mentions) d.mentions = this.mentions.slice();
    if (this.edited) d.edited = true;
    if (this.senderPubId) d.senderPubId = this.senderPubId;
    if (this.senderSig) d.senderSig = this.senderSig;
    return d;
  }

  static fromDict(d) {
    const msgId = String(d.id == null ? "" : d.id);
    const senderId = String(d.senderId == null ? "" : d.senderId);
    if (!msgId || !senderId) throw new Error("chat message missing required field: id or senderId");
    const fileInfo = d.fileInfo != null ? FileInfo.fromDict(d.fileInfo) : null;
    let replyTo = null;
    let replyPreview = null;
    let replySender = null;
    let quoteSender = null;
    let quoteKind = null;
    const quote = d.quote;
    if (quote && typeof quote === "object" && !Array.isArray(quote)) {
      replyTo = String(quote.quotedId == null ? "" : quote.quotedId) || null;
      replyPreview = String(quote.quotedPreview == null ? "" : quote.quotedPreview) || null;
      replySender = String(quote.quotedName == null ? "" : quote.quotedName) || null;
      quoteSender =
        String(quote.quotedSender == null ? "" : quote.quotedSender).slice(0, U.MAX_QUOTE_FIELD_LEN) ||
        null;
      const kind = String(quote.quotedKind == null ? U.CONTENT_KIND_TEXT : quote.quotedKind);
      quoteKind =
        [
          U.CONTENT_KIND_TEXT,
          U.FILE_KIND_FILE,
          U.FILE_KIND_IMAGE,
          U.FILE_KIND_VIDEO,
          U.FILE_KIND_AUDIO,
        ].includes(kind) === true
          ? kind
          : U.CONTENT_KIND_TEXT;
    } else {
      replyTo = d.replyTo == null ? null : String(d.replyTo);
      replyPreview = d.replyPreview == null ? null : String(d.replyPreview);
      replySender = d.replySender == null ? null : String(d.replySender);
    }
    if (replyPreview != null) replyPreview = replyPreview.slice(0, U.MAX_REPLY_PREVIEW);
    if (replySender != null) replySender = replySender.slice(0, U.MAX_QUOTE_FIELD_LEN);
    if (replyTo != null) replyTo = replyTo.slice(0, U.MAX_QUOTE_FIELD_LEN);
    const mentions = U.sanitizeMentions(d.mentions);
    const edited = d.edited == null ? false : d.edited;
    if (typeof edited !== "boolean") throw new Error("chat field edited must be a boolean");
    const senderPubId = d.senderPubId == null ? null : String(d.senderPubId);
    const senderSig = d.senderSig == null ? null : String(d.senderSig);
    return new ChatMessage({
      id: msgId,
      content: String(d.content == null ? "" : d.content),
      timestamp: U.strictInt(d.timestamp == null ? 0 : d.timestamp, "timestamp"),
      senderId,
      senderName: String(d.senderName == null ? "" : d.senderName),
      fileInfo,
      replyTo,
      replyPreview,
      replySender,
      quoteSender,
      quoteKind,
      forwarded: parseForwarded(d.forwarded),
      mentions: mentions.length ? mentions : null,
      edited,
      senderPubId,
      senderSig,
    });
  }

  markedFromMe(myId) {
    this.isFromMe = this.senderId === myId;
    return this;
  }

  clone() {
    return new ChatMessage({
      id: this.id,
      content: this.content,
      timestamp: this.timestamp,
      senderId: this.senderId,
      senderName: this.senderName,
      isFromMe: this.isFromMe,
      fileInfo: this.fileInfo
        ? new FileInfo(
            this.fileInfo.fileId,
            this.fileInfo.fileName,
            this.fileInfo.fileSize,
            this.fileInfo.downloadHost,
            this.fileInfo.downloadPort,
            this.fileInfo.fileKey,
            this.fileInfo.kind,
            this.fileInfo.folderId,
            this.fileInfo.folderName,
            this.fileInfo.relativePath,
            this.fileInfo.folderTotal
          )
        : null,
      replyTo: this.replyTo,
      replyPreview: this.replyPreview,
      replySender: this.replySender,
      quoteSender: this.quoteSender,
      quoteKind: this.quoteKind,
      forwarded: this.forwarded,
      mentions: this.mentions ? this.mentions.slice() : null,
      edited: this.edited,
      senderPubId: this.senderPubId,
      senderSig: this.senderSig,
      pending: this.pending,
      read: this.read,
    });
  }
}

class GroupInfo {
  constructor(groupName, creatorName, creatorId, memberCount) {
    this.groupName = groupName;
    this.creatorName = creatorName;
    this.creatorId = creatorId;
    this.memberCount = memberCount;
  }

  toDict() {
    return {
      groupName: this.groupName,
      creatorName: this.creatorName,
      creatorId: this.creatorId,
      memberCount: this.memberCount,
    };
  }

  static fromDict(d) {
    const memberCount = Number(d.memberCount == null ? 1 : d.memberCount);
    if (!Number.isFinite(memberCount)) {
      // parity with the Python reader: a non-numeric count fails the whole
      // packet decode instead of silently producing NaN
      throw new Error("groupInfo memberCount must be an integer");
    }
    return new GroupInfo(
      String(d.groupName == null ? "" : d.groupName),
      String(d.creatorName == null ? "" : d.creatorName),
      String(d.creatorId == null ? "" : d.creatorId),
      memberCount
    );
  }
}

class ContactRequest {
  constructor(id, name, ip, port, fromRemoved = false, timestamp = 0, peerFingerprint = "") {
    this.id = id;
    this.name = name;
    this.ip = ip;
    this.port = port;
    this.fromRemoved = fromRemoved;
    this.timestamp = timestamp;
    this.peerFingerprint = peerFingerprint;
  }

  toDict() {
    const d = {
      id: this.id,
      name: this.name,
      ip: this.ip,
      port: this.port,
      timestamp: this.timestamp,
    };
    if (this.fromRemoved) d.fromRemoved = true;
    if (this.peerFingerprint) d.peerFingerprint = this.peerFingerprint;
    return d;
  }
}

class CallInfo {
  constructor(
    callId,
    callerId,
    callerName,
    calleeId,
    mediaPort = 0,
    accepted = true,
    audioEnabled = true,
    media = "",
    meetingId = ""
  ) {
    this.callId = callId;
    this.callerId = callerId;
    this.callerName = callerName;
    this.calleeId = calleeId;
    this.mediaPort = mediaPort;
    this.accepted = accepted;
    this.audioEnabled = audioEnabled;
    this.media = media;
    this.meetingId = meetingId;
  }

  toDict() {
    const d = {
      callId: this.callId,
      callerId: this.callerId,
      callerName: this.callerName,
      calleeId: this.calleeId,
    };
    if (this.mediaPort) d.mediaPort = this.mediaPort;
    if (!this.accepted) d.accepted = false;
    if (!this.audioEnabled) d.audioEnabled = false;
    if (this.media) d.media = this.media;
    if (this.meetingId) d.meetingId = this.meetingId;
    return d;
  }

  static fromDict(d) {
    const accepted = d.accepted == null ? true : d.accepted;
    const audioEnabled = d.audioEnabled == null ? true : d.audioEnabled;
    if (typeof accepted !== "boolean") throw new Error("call field accepted must be a boolean");
    if (typeof audioEnabled !== "boolean") {
      throw new Error("call field audioEnabled must be a boolean");
    }
    let media = String(d.media == null ? "" : d.media);
    if (media !== "audio") media = "";
    return new CallInfo(
      String(d.callId == null ? "" : d.callId),
      String(d.callerId == null ? "" : d.callerId),
      String(d.callerName == null ? "" : d.callerName),
      String(d.calleeId == null ? "" : d.calleeId),
      U.strictPort(d.mediaPort == null ? 0 : d.mediaPort, "mediaPort"),
      accepted,
      audioEnabled,
      media,
      String(d.meetingId == null ? "" : d.meetingId)
    );
  }
}

class NetworkPacket {
  constructor(fields = {}) {
    this.type = fields.type;
    this.groupId = fields.groupId != null ? fields.groupId : null;
    this.peer = fields.peer || null;
    this.members = fields.members || null;
    this.message = fields.message || null;
    this.messages = fields.messages || null;
    this.messageId = fields.messageId != null ? fields.messageId : null;
    this.senderId = fields.senderId != null ? fields.senderId : null;
    this.errorMessage = fields.errorMessage != null ? fields.errorMessage : null;
    this.groupInfo = fields.groupInfo || null;
    this.fileInfo = fields.fileInfo || null;
    this.fileId = fields.fileId != null ? fields.fileId : null;
    this.targetId = fields.targetId != null ? fields.targetId : null;
    this.call = fields.call || null;
    this.host = fields.host || null;
    this.deletedIds = fields.deletedIds || null;
    this.hsMode = fields.hsMode != null ? fields.hsMode : null;
    this.eph = fields.eph != null ? fields.eph : null;
    this.ident = fields.ident != null ? fields.ident : null;
    this.mac = fields.mac != null ? fields.mac : null;
    this.sig = fields.sig != null ? fields.sig : null;
    this.token = fields.token != null ? fields.token : null;
    this.offset = fields.offset != null ? fields.offset : null;
    this.seq = fields.seq != null ? fields.seq : null;
    this.upToId = fields.upToId != null ? fields.upToId : null;
    this.readerId = fields.readerId != null ? fields.readerId : null;
    this.active = fields.active != null ? fields.active : null;
    this.groupName = fields.groupName != null ? fields.groupName : null;
    this.announcement = fields.announcement != null ? fields.announcement : null;
    this.newContent = fields.newContent != null ? fields.newContent : null;
    this.emoji = fields.emoji != null ? fields.emoji : null;
    this.senderPubId = fields.senderPubId != null ? fields.senderPubId : null;
    this.senderSig = fields.senderSig != null ? fields.senderSig : null;
    this.name = fields.name != null ? fields.name : null;
    this.size = fields.size != null ? fields.size : null;
    this.ts = fields.ts != null ? fields.ts : null;
    this.senderName = fields.senderName != null ? fields.senderName : null;
    this.groupFiles = fields.groupFiles || null;
    this.removedIds = fields.removedIds || null;
  }

  toDict() {
    const d = { type: this.type };
    if (this.groupId != null) d.groupId = this.groupId;
    if (this.peer) d.peer = this.peer.toDict();
    if (this.members) d.members = this.members.map((p) => p.toDict());
    if (this.message) d.message = this.message.toDict();
    if (this.messages) d.messages = this.messages.map((m) => m.toDict());
    if (this.messageId != null) d.messageId = this.messageId;
    if (this.senderId != null) d.senderId = this.senderId;
    if (this.errorMessage != null) d.errorMessage = this.errorMessage;
    if (this.groupInfo) d.groupInfo = this.groupInfo.toDict();
    if (this.fileInfo) d.fileInfo = this.fileInfo.toDict();
    if (this.fileId != null) d.fileId = this.fileId;
    if (this.targetId != null) d.targetId = this.targetId;
    if (this.call) d.call = this.call.toDict();
    if (this.host) d.host = this.host.toDict();
    if (this.deletedIds && this.deletedIds.length) d.deletedIds = this.deletedIds.slice();
    if (this.hsMode != null) d.hsMode = this.hsMode;
    if (this.eph != null) d.eph = this.eph;
    if (this.ident != null) d.ident = this.ident;
    if (this.mac != null) d.mac = this.mac;
    if (this.sig != null) d.sig = this.sig;
    if (this.token != null) d.token = this.token;
    if (this.offset != null) d.offset = this.offset;
    if (this.seq != null) d.seq = this.seq;
    if (this.upToId != null) d.upToId = this.upToId;
    if (this.readerId != null) d.readerId = this.readerId;
    if (this.active != null) d.active = this.active;
    if (this.groupName != null) d.groupName = this.groupName;
    if (this.announcement != null) d.announcement = this.announcement;
    if (this.newContent != null) d.newContent = this.newContent;
    if (this.emoji != null) d.emoji = this.emoji;
    if (this.senderPubId != null) d.senderPubId = this.senderPubId;
    if (this.senderSig != null) d.senderSig = this.senderSig;
    if (this.name != null) d.name = this.name;
    if (this.size != null) d.size = this.size;
    if (this.ts != null) d.ts = this.ts;
    if (this.senderName != null) d.senderName = this.senderName;
    if (this.groupFiles && this.groupFiles.length) {
      d.groupFiles = this.groupFiles.map((e) => e.toDict());
    }
    if (this.removedIds && this.removedIds.length) d.removedIds = this.removedIds.slice();
    return d;
  }

  toJson() {
    return JSON.stringify(this.toDict());
  }

  static fromDict(d) {
    const pktType = String(d.type == null ? "" : d.type);
    if (!pktType) throw new Error("packet missing required field: type");
    const pkt = new NetworkPacket({ type: pktType });
    if (d.groupId != null) pkt.groupId = String(d.groupId);
    if (d.peer != null) pkt.peer = Peer.fromDict(d.peer);
    if (d.members != null) pkt.members = d.members.map((m) => Peer.fromDict(m));
    if (d.errorMessage != null) pkt.errorMessage = String(d.errorMessage);
    if (d.message != null) pkt.message = ChatMessage.fromDict(d.message);
    if (d.messageId != null) pkt.messageId = String(d.messageId);
    if (d.messages != null) pkt.messages = d.messages.map((m) => ChatMessage.fromDict(m));
    if (d.senderId != null) pkt.senderId = String(d.senderId);
    if (d.groupInfo != null) pkt.groupInfo = GroupInfo.fromDict(d.groupInfo);
    if (d.fileInfo != null) pkt.fileInfo = FileInfo.fromDict(d.fileInfo);
    if (d.fileId != null) pkt.fileId = String(d.fileId);
    if (d.targetId != null) pkt.targetId = String(d.targetId);
    if (d.call != null) pkt.call = CallInfo.fromDict(d.call);
    if (d.host != null) pkt.host = Peer.fromDict(d.host);
    if (d.deletedIds != null) {
      if (Array.isArray(d.deletedIds)) pkt.deletedIds = d.deletedIds.map((i) => String(i));
    }
    if (d.hsMode != null) pkt.hsMode = String(d.hsMode);
    if (d.eph != null) pkt.eph = String(d.eph);
    if (d.ident != null) pkt.ident = String(d.ident);
    if (d.mac != null) pkt.mac = String(d.mac);
    if (d.sig != null) pkt.sig = String(d.sig);
    if (d.token != null) pkt.token = String(d.token);
    if (d.offset != null) pkt.offset = U.strictInt(d.offset, "offset");
    if (d.seq != null) pkt.seq = U.strictInt(d.seq, "seq");
    if (d.upToId != null) pkt.upToId = String(d.upToId);
    if (d.readerId != null) pkt.readerId = String(d.readerId);
    if (d.active != null) {
      if (typeof d.active !== "boolean") throw new Error("typing field active must be a boolean");
      pkt.active = d.active;
    }
    if (d.groupName != null) pkt.groupName = String(d.groupName);
    if (d.announcement != null) pkt.announcement = String(d.announcement);
    if (d.newContent != null) pkt.newContent = String(d.newContent);
    if (d.emoji != null) pkt.emoji = U.sanitizeEmoji(d.emoji);
    if (d.senderPubId != null) pkt.senderPubId = String(d.senderPubId);
    if (d.senderSig != null) pkt.senderSig = String(d.senderSig);
    if (d.name != null) pkt.name = String(d.name);
    if (d.size != null) pkt.size = U.strictSize(d.size, "size");
    if (d.ts != null) pkt.ts = U.strictInt(d.ts, "ts");
    if (d.senderName != null) pkt.senderName = String(d.senderName);
    if (d.groupFiles != null) {
      pkt.groupFiles = U.sanitizeGroupFiles(d.groupFiles, GroupFileInfo.fromDict);
    }
    if (d.removedIds != null) {
      if (Array.isArray(d.removedIds)) pkt.removedIds = d.removedIds.map((i) => String(i));
    }
    if (pktType === "error" && pkt.errorMessage == null) {
      throw new Error("error packet missing required field: errorMessage");
    }
    if (pktType === "chat" && pkt.message == null) {
      throw new Error("chat packet missing required field: message");
    }
    if (pktType === "file_message" && pkt.message == null) {
      throw new Error("file_message packet missing required field: message");
    }
    if (pktType === "file_download" && !pkt.fileId) {
      throw new Error("file_download packet missing required field: fileId");
    }
    if (pktType === "file_download") {
      if (pkt.offset == null) throw new Error("file_download packet missing required field: offset");
      if (pkt.offset < 0) throw new Error("file_download offset must be non-negative");
    }
    if (pktType === "delete_message" && !pkt.messageId) {
      throw new Error("delete_message packet missing required field: messageId");
    }
    if (pktType === "read_receipt" && (!pkt.upToId || !pkt.readerId)) {
      throw new Error("read_receipt packet missing required field: upToId or readerId");
    }
    if (pktType === "typing" && (!pkt.senderId || pkt.active == null)) {
      throw new Error("typing packet missing required field: senderId or active");
    }
    if (pktType === "group_update" && !pkt.groupId) {
      throw new Error("group_update packet missing required field: groupId");
    }
    if (pktType === "kick_member" && (!pkt.groupId || !pkt.targetId)) {
      throw new Error("kick_member packet missing required field: groupId/targetId");
    }
    if (pktType === "edit_message") {
      if (!pkt.messageId || !pkt.senderId || pkt.newContent == null) {
        throw new Error("edit_message packet missing required field: messageId/senderId/newContent");
      }
      if (!U.isValidContent(pkt.newContent)) {
        throw new Error("edit_message newContent is not valid content");
      }
    }
    if (pktType === "reaction") {
      if (!pkt.messageId || !pkt.senderId || !pkt.emoji) {
        throw new Error("reaction packet missing required field: messageId/senderId/emoji");
      }
    }
    if (pktType === "pin_message" && (!pkt.messageId || !pkt.senderId)) {
      throw new Error("pin_message packet missing required field: messageId/senderId");
    }
    return pkt;
  }

  static fromJson(line) {
    return NetworkPacket.fromDict(JSON.parse(line));
  }
}

module.exports = {
  Peer,
  FileInfo,
  ForwardedInfo,
  GroupFileInfo,
  ChatMessage,
  GroupInfo,
  ContactRequest,
  CallInfo,
  NetworkPacket,
  parseForwarded,
};
