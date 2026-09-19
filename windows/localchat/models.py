import json
import os
from dataclasses import dataclass
from typing import Optional, List

MAX_CONTENT_LENGTH = 5000
MAX_LINE_LENGTH = 64 * 1024
TCP_PORT = 9999
# Reply/quote previews travel inside the wire reply fields: cap the copied
# snippet so one huge quoted message cannot bloat every reply packet
# (Android parity: Models.MAX_REPLY_PREVIEW).
MAX_REPLY_PREVIEW = 120


def _strict_int(value, field: str) -> int:
    """Strict integer coercion for wire fields: only real ints (never bool)
    and pure ASCII digit strings pass; floats, bools and anything else raise
    ValueError so the packet reader rejects the whole malformed packet
    instead of silently rounding a crafted value."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value and all("0" <= c <= "9" for c in value):
        return int(value)
    raise ValueError(f"{field} must be an integer")


def _strict_size(value, field: str) -> int:
    """_strict_int plus a non-negative lower bound: a negative fileSize is
    meaningless on the wire and would corrupt UI progress math."""
    size = _strict_int(value, field)
    if size < 0:
        raise ValueError(f"{field} must be non-negative")
    return size


def _strict_port(value, field: str) -> int:
    """_strict_int plus the 0-65535 port range. 0 means "not set" and stays
    allowed where the field is optional (existing semantics)."""
    port = _strict_int(value, field)
    if not 0 <= port <= 65535:
        raise ValueError(f"{field} must be a port in 0..65535")
    return port


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".3gp"}
AUDIO_EXTENSIONS = {".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".mp3", ".flac"}

FILE_KIND_FILE = "file"
FILE_KIND_IMAGE = "image"
FILE_KIND_VIDEO = "video"
FILE_KIND_AUDIO = "audio"
MEDIA_KINDS = (FILE_KIND_IMAGE, FILE_KIND_VIDEO)

# Mentions (@提及): ChatMessage.mentions carries peer ids; the special id
# "all" means "mentioned everyone". Optional on the wire (omitted when unset)
# so a plain message stays byte-identical (Android parity).
MENTION_ALL = "all"
MAX_MENTIONS = 64
MAX_MENTION_ID_LEN = 128
# Reaction emoji are sender-supplied short strings: capped and stripped of
# control characters so one packet cannot bloat storage or rendering.
MAX_EMOJI_LEN = 16

# Call media kinds carried by CallInfo.media: "audio" or (implicit) "video".
# Omitted on the wire for video so a plain video offer stays byte-identical.
MEDIA_AUDIO = "audio"
MEDIA_VIDEO = "video"

# Call log direction/result vocabulary (local-only, shared with the UI and
# storage: never sent over the wire).
CALL_DIRECTION_INCOMING = "incoming"
CALL_DIRECTION_OUTGOING = "outgoing"
CALL_RESULT_ANSWERED = "answered"
CALL_RESULT_MISSED = "missed"
CALL_RESULT_REJECTED = "rejected"
CALL_RESULT_CANCELLED = "cancelled"
CALL_RESULT_FAILED = "failed"


def detect_media_kind(name: str) -> str:
    """Classify a file by extension: "image", "video", "audio" or "file". Used
    on the send path so media is offered as a viewable message; the receiving
    end renders it inline instead of as a plain file card. Old peers that do
    not know "audio" degrade it to a plain file card that still downloads and
    plays externally (normalize_media_kind parity on the Android side)."""
    ext = os.path.splitext(name)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return FILE_KIND_IMAGE
    if ext in VIDEO_EXTENSIONS:
        return FILE_KIND_VIDEO
    if ext in AUDIO_EXTENSIONS:
        return FILE_KIND_AUDIO
    return FILE_KIND_FILE


def normalize_media_kind(kind: str) -> str:
    """Keep only the known kinds on parse; anything else (future sender kinds,
    crafted values) degrades to a plain file message."""
    if kind in (FILE_KIND_IMAGE, FILE_KIND_VIDEO, FILE_KIND_AUDIO):
        return kind
    return FILE_KIND_FILE


def sanitize_emoji(value) -> str:
    """A reaction emoji is a sender-supplied short string: drop control
    characters and whitespace so a crafted value can never bloat storage or
    break rendering. Returns "" when nothing usable remains."""
    text = str(value or "")
    text = "".join(
        ch for ch in text if ch.isprintable() and not ch.isspace()
    )
    return text[:MAX_EMOJI_LEN]


def sanitize_mentions(raw) -> List[str]:
    """Normalize an inbound mentions list: strings only, deduped in order,
    each capped, at most MAX_MENTIONS entries. A malformed shape is ignored
    (returns []) rather than failing the whole message."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: List[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        entry = item[:MAX_MENTION_ID_LEN]
        if entry and entry not in out:
            out.append(entry)
        if len(out) >= MAX_MENTIONS:
            break
    return out


_EMOJI_MODIFIER_ORDS = frozenset((0xFE0F, 0x200D, 0x20E3, 0x2764, 0x00A9, 0x00AE, 0x2122))


def _is_emoji_char(ch: str) -> bool:
    o = ord(ch)
    return (
        o >= 0x1F000
        or 0x2600 <= o <= 0x27BF
        or 0x2B00 <= o <= 0x2BFF
        or o in _EMOJI_MODIFIER_ORDS
    )


def is_big_emoji(content: str) -> bool:
    """True when [content] is sticker-sized: only emoji (+ variation selectors,
    ZWJ, keycap caps), 1..16 code points, at least one real emoji character
    (not only modifiers). Local rendering hint only (never sent on the wire)."""
    text = str(content or "").strip()
    if not text or len(text) > 16:
        return False
    if not all(_is_emoji_char(ch) for ch in text):
        return False
    return any(
        ord(ch) >= 0x1F000
        or 0x2600 <= ord(ch) <= 0x27BF
        or 0x2B00 <= ord(ch) <= 0x2BFF
        or ord(ch) in (0x2764, 0x00A9, 0x00AE, 0x2122)
        for ch in text
    )


# Folder transfer: a folder is offered as one file_message per entry (so old
# peers still see ordinary downloadable files), each carrying the same
# folderId plus its folderName/relativePath/folderTotal. Receivers group the
# entries by folderId and reproduce the tree under a directory the user picks.
MAX_FOLDER_FILES = 1000
MAX_RELATIVE_PATH_LENGTH = 1024
_FOLDER_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def sanitize_folder_id(value) -> str:
    """Folder ids are sender-generated opaque keys (uuid4). Keep only a safe
    alphabet so a crafted id can never do anything but group messages."""
    text = "".join(ch for ch in str(value or "") if ch in _FOLDER_ID_CHARS)
    return text[:64]


def sanitize_relative_path(value) -> str:
    """Normalize a sender-provided folder-relative path to a safe relative
    POSIX-style path. Backslashes become "/"; empty, "." and ".." segments are
    dropped, and each remaining segment goes through sanitize_file_name (which
    strips separators, control characters and trailing dots/spaces). The
    result can therefore never be absolute or escape the receiver's chosen
    destination directory. Returns "" when nothing usable remains."""
    text = str(value or "").replace("\\", "/")
    parts = []
    for seg in text.split("/"):
        if not seg or seg in (".", ".."):
            continue
        # a ":" could turn a segment into a drive-relative path ("C:") that
        # escapes a joined root; Windows file names never contain one
        seg = sanitize_file_name(seg).replace(":", "")
        if seg:
            parts.append(seg)
        if len(parts) >= 64:
            break
    out = "/".join(parts)
    if len(out) > MAX_RELATIVE_PATH_LENGTH:
        out = out[:MAX_RELATIVE_PATH_LENGTH].rstrip("/ .")
    return out



def sanitize_file_name(name: str) -> str:
    """Sanitize a file name received from the network before it is ever
    combined into a local save path: normalize separators and keep only the
    basename, strip control characters, drop trailing dots/spaces (Windows
    cannot round-trip them) and cap the length. Returns "file" when nothing
    usable remains, so a crafted fileName can never push the suggested save
    path outside the Downloads directory."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if not (ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0))
    name = name.rstrip(" .")
    if len(name) > 255:
        # truncation can re-expose a trailing dot/space
        name = name[:255].rstrip(" .")
    return name or "file"


@dataclass
class Peer:
    id: str
    name: str
    ip_address: str
    port: int

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "ipAddress": self.ip_address, "port": self.port}

    @staticmethod
    def from_dict(d: dict) -> "Peer":
        return Peer(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            ip_address=str(d.get("ipAddress", "")),
            port=_strict_port(d.get("port", 0), "peer port"),
        )


@dataclass
class FileInfo:
    """Metadata for a file offered in chat. The bytes travel over a separate
    short-lived download server on the sender (see file_download/file_meta
    handshake in network.py), not over the message stream.

    file_key is a random per-file AES key (Base64) that travels INSIDE the
    (already encrypted) message channel and protects the raw download
    stream: chunk framing and GCM authentication are handled by the file
    transfer layer (Android parity)."""

    file_id: str
    file_name: str
    file_size: int
    download_host: str
    download_port: int
    file_key: str = ""
    # "file" (plain file card), "image" or "video": media kinds are rendered
    # inline in the conversation after download. Default "file" is omitted on
    # the wire so packets match kotlinx.serialization's output (defaults are
    # not encoded), keeping old clients interoperable.
    kind: str = FILE_KIND_FILE
    # Folder transfer (all optional, omitted on the wire at their defaults so
    # a plain file offer stays byte-identical): one message per entry, the
    # same folder_id on each, relative_path locating the entry inside the
    # folder, folder_name the root folder name and folder_total the entry
    # count (0 = unknown). Old peers ignore these fields and just see files.
    folder_id: str = ""
    folder_name: str = ""
    relative_path: str = ""
    folder_total: int = 0

    def to_dict(self) -> dict:
        d = {
            "fileId": self.file_id,
            "fileName": self.file_name,
            "fileSize": self.file_size,
            "downloadHost": self.download_host,
            "downloadPort": self.download_port,
        }
        if self.file_key:
            d["fileKey"] = self.file_key
        if self.kind != FILE_KIND_FILE:
            d["kind"] = self.kind
        if self.folder_id:
            d["folderId"] = self.folder_id
            if self.folder_name:
                d["folderName"] = self.folder_name
            if self.relative_path:
                d["relativePath"] = self.relative_path
            if self.folder_total > 0:
                d["folderTotal"] = self.folder_total
        return d

    @staticmethod
    def from_dict(d: dict) -> "FileInfo":
        folder_total = _strict_size(d.get("folderTotal", 0), "folderTotal")
        if folder_total > MAX_FOLDER_FILES:
            # advisory only: never trust a crafted count for allocation
            folder_total = 0
        return FileInfo(
            file_id=str(d.get("fileId", "")),
            # every inbound path (group relay, direct chat, restart recovery)
            # builds its save-path suggestion from this name, so sanitize it
            # here once instead of trusting the sender's basename
            file_name=sanitize_file_name(str(d.get("fileName", ""))),
            file_size=_strict_size(d.get("fileSize", 0), "fileSize"),
            download_host=str(d.get("downloadHost", "")),
            download_port=_strict_port(d.get("downloadPort", 0), "downloadPort"),
            file_key=str(d.get("fileKey", "")),
            kind=normalize_media_kind(str(d.get("kind", FILE_KIND_FILE))),
            folder_id=sanitize_folder_id(d.get("folderId", "")),
            folder_name=sanitize_file_name(str(d.get("folderName", ""))) if d.get("folderName") else "",
            relative_path=sanitize_relative_path(d.get("relativePath", "")),
            folder_total=folder_total,
        )


@dataclass
class ChatMessage:
    id: str
    content: str
    timestamp: int
    sender_id: str
    sender_name: str
    is_from_me: bool = False
    file_info: Optional[FileInfo] = None
    # Reply/quote (optional, omitted from the wire when unset so a plain
    # message stays byte-identical): reply_to is the quoted message's id,
    # reply_preview a short snippet of its content and reply_sender the
    # original sender's display name. All three are self-contained so the
    # receiver can render the quote even when the referenced message is not
    # in its local history (Android parity: ChatMessage.replyTo/replyPreview/
    # replySender).
    reply_to: Optional[str] = None
    reply_preview: Optional[str] = None
    reply_sender: Optional[str] = None
    # Message edit: mentions is the optional list of mentioned peer ids
    # (MENTION_ALL = everyone); edited marks a message whose content was
    # changed by its author via edit_message (set by the receiver, and carried
    # on the wire only when True so a plain message stays byte-identical).
    mentions: Optional[List[str]] = None
    edited: bool = False
    # Local-only delivery state (like is_from_me, never sent over the wire):
    # true while an offline-sent message still waits in the direct chat
    # outbox for the peer to come online (Android parity).
    pending: bool = False
    # Local-only read state for OWN direct-chat messages: flipped when the
    # peer's read_receipt covers this message. Never sent over the wire;
    # group chats do not track per-reader receipts.
    read: bool = False

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "content": self.content,
            "timestamp": self.timestamp,
            "senderId": self.sender_id,
            "senderName": self.sender_name,
        }
        if self.file_info is not None:
            d["fileInfo"] = self.file_info.to_dict()
        if self.reply_to is not None:
            d["replyTo"] = self.reply_to
            if self.reply_preview is not None:
                d["replyPreview"] = self.reply_preview
            if self.reply_sender is not None:
                d["replySender"] = self.reply_sender
        if self.mentions:
            d["mentions"] = list(self.mentions)
        if self.edited:
            d["edited"] = True
        return d

    @staticmethod
    def from_dict(d: dict) -> "ChatMessage":
        msg_id = str(d.get("id", ""))
        sender_id = str(d.get("senderId", ""))
        if not msg_id or not sender_id:
            raise ValueError("chat message missing required field: id or senderId")
        file_info = None
        if d.get("fileInfo") is not None:
            file_info = FileInfo.from_dict(d["fileInfo"])
        reply_to = None if d.get("replyTo") is None else str(d["replyTo"])
        reply_preview = (
            None if d.get("replyPreview") is None else str(d["replyPreview"])
        )
        reply_sender = (
            None if d.get("replySender") is None else str(d["replySender"])
        )
        mentions = sanitize_mentions(d.get("mentions"))
        edited = d.get("edited", False)
        if not isinstance(edited, bool):
            # like the typing/call flags: only a real boolean passes, so a
            # crafted "true"/1 cannot forge the edited marker
            raise ValueError("chat field edited must be a boolean")
        return ChatMessage(
            id=msg_id,
            content=str(d.get("content", "")),
            timestamp=_strict_int(d.get("timestamp", 0), "timestamp"),
            sender_id=sender_id,
            sender_name=str(d.get("senderName", "")),
            file_info=file_info,
            reply_to=reply_to,
            reply_preview=reply_preview,
            reply_sender=reply_sender,
            mentions=mentions or None,
            edited=edited,
        )

    def marked_from_me(self, my_id: str) -> "ChatMessage":
        self.is_from_me = self.sender_id == my_id
        return self


@dataclass
class GroupInfo:
    group_name: str
    creator_name: str
    creator_id: str
    member_count: int

    def to_dict(self) -> dict:
        return {
            "groupName": self.group_name,
            "creatorName": self.creator_name,
            "creatorId": self.creator_id,
            "memberCount": self.member_count,
        }

    @staticmethod
    def from_dict(d: dict) -> "GroupInfo":
        return GroupInfo(
            group_name=str(d.get("groupName", "")),
            creator_name=str(d.get("creatorName", "")),
            creator_id=str(d.get("creatorId", "")),
            member_count=int(d.get("memberCount", 1)),
        )


@dataclass
class ContactRequest:
    """An incoming first-contact / removed-member dial parked in the
    contact-request message box for the user to accept or ignore (Android
    parity). Persisted by the ViewModel; [fromRemoved] marks a request from
    a member the local user removed. [peer_fingerprint] is the dialer's
    identity-key fingerprint ("安全码") proven by the secured handshake —
    shown in the request card so the user can compare it out-of-band
    against the peer's settings screen before accepting."""

    id: str
    name: str
    ip: str
    port: int
    from_removed: bool = False
    timestamp: int = 0
    peer_fingerprint: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "timestamp": self.timestamp,
        }
        if self.from_removed:
            d["fromRemoved"] = True
        if self.peer_fingerprint:
            d["peerFingerprint"] = self.peer_fingerprint
        return d

    @staticmethod
    def from_dict(d: dict) -> "ContactRequest":
        return ContactRequest(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            ip=str(d.get("ip", "")),
            port=int(d.get("port", 0)),
            from_removed=bool(d.get("fromRemoved", False)),
            timestamp=int(d.get("timestamp", 0)),
            peer_fingerprint=str(d.get("peerFingerprint", "")),
        )


@dataclass
class CallInfo:
    """Metadata for a video/audio call (see docs/video_call_protocol.md).

    Serialization mirrors kotlinx.serialization on the Android side:
    - camelCase keys
    - default-valued fields (accepted=True, audioEnabled=True, mediaPort=0) are
      omitted so the wire bytes match the Kotlin output byte-for-byte.
    """

    call_id: str
    caller_id: str
    caller_name: str
    callee_id: str
    media_port: int = 0
    accepted: bool = True
    audio_enabled: bool = True
    # "audio" | "video"; empty = video (omitted on the wire so a plain video
    # offer stays byte-identical to the pre-media-field format).
    media: str = ""

    def to_dict(self) -> dict:
        d = {
            "callId": self.call_id,
            "callerId": self.caller_id,
            "callerName": self.caller_name,
            "calleeId": self.callee_id,
        }
        if self.media_port:
            d["mediaPort"] = self.media_port
        if not self.accepted:
            d["accepted"] = False
        if not self.audio_enabled:
            d["audioEnabled"] = False
        if self.media:
            d["media"] = self.media
        return d

    @staticmethod
    def from_dict(d: dict) -> "CallInfo":
        accepted = d.get("accepted", True)
        audio_enabled = d.get("audioEnabled", True)
        if not isinstance(accepted, bool):
            raise ValueError("call field accepted must be a boolean")
        if not isinstance(audio_enabled, bool):
            raise ValueError("call field audioEnabled must be a boolean")
        # Unknown media kinds degrade to video (empty) rather than failing:
        # a future kind must never break the whole call packet.
        media = str(d.get("media", "") or "")
        if media != MEDIA_AUDIO:
            media = ""
        return CallInfo(
            call_id=str(d.get("callId", "")),
            caller_id=str(d.get("callerId", "")),
            caller_name=str(d.get("callerName", "")),
            callee_id=str(d.get("calleeId", "")),
            media_port=_strict_port(d.get("mediaPort", 0), "mediaPort"),
            accepted=accepted,
            audio_enabled=audio_enabled,
            media=media,
        )


@dataclass
class NetworkPacket:
    type: str
    group_id: Optional[str] = None
    peer: Optional[Peer] = None
    members: Optional[List[Peer]] = None
    message: Optional[ChatMessage] = None
    messages: Optional[List[ChatMessage]] = None
    message_id: Optional[str] = None
    sender_id: Optional[str] = None
    error_message: Optional[str] = None
    group_info: Optional[GroupInfo] = None
    file_info: Optional[FileInfo] = None
    file_id: Optional[str] = None
    target_id: Optional[str] = None
    call: Optional[CallInfo] = None
    # The group's host (creator), returned by a member-sponsored join so the
    # newcomer can connect to the host for the relay path.
    host: Optional[Peer] = None
    # Delete tombstones (message ids deleted while the receiver was away),
    # attached to join_ack / history_reply for convergence. Optional: omitted
    # from the wire entirely when empty (byte-compat with older peers).
    deleted_ids: Optional[List[str]] = None
    # Handshake: which kind of secured connection is being set up
    # (query/join/mesh/direct).
    hs_mode: Optional[str] = None
    # Handshake: Base64 ephemeral ECDH public key.
    eph: Optional[str] = None
    # Handshake: Base64 long-term identity public key (direct mode).
    ident: Optional[str] = None
    # Handshake: Base64 HMAC confirmation (password modes).
    mac: Optional[str] = None
    # Handshake: Base64 ECDSA signature over the transcript (direct mode).
    sig: Optional[str] = None
    # file_download request only: Base64(HMAC-SHA256(fileKey,
    # "lc-file-dl-v1:" + fileId)) — the downloader proves it received the
    # (encrypted) offer. MANDATORY on every request: a sender refuses a
    # request without it.
    token: Optional[str] = None
    # file_download request only: bytes the receiver already holds in its
    # ".part" staging file. MANDATORY (resume support): the sender starts the
    # byte stream at this offset, so 0 means a fresh download and a value
    # equal to the file size transfers nothing but the meta/EOF. The chunks
    # stay independently GCM-encrypted with a fresh random nonce, so no
    # nonce/AAD state depends on this offset (Android parity).
    offset: Optional[int] = None
    # Wire-session sequence number: stamped by Wire.send_packet (per
    # direction, strictly 1,2,3,...) INSIDE the GCM-protected JSON, so the
    # receiver can reject replayed/reordered/injected lines. Never set by
    # application code.
    seq: Optional[int] = None
    # read_receipt: the newest message id the sender has read in the
    # [group_id] scope; reader_id is the reader's device id (must match the
    # packet's authenticated sender). Direct chats only — group chats do not
    # track per-reader receipts (see README).
    up_to_id: Optional[str] = None
    reader_id: Optional[str] = None
    # typing: true while the sender is composing in the [group_id] scope,
    # false once it stopped. Advisory only: receivers also expire an indicator
    # that received no refresh (see README).
    active: Optional[bool] = None
    # group_update packet (group owner only): a new display name and/or a new
    # announcement. Both optional; None means "unchanged" and is omitted from
    # the wire so the bytes match kotlinx.serialization (defaults are not
    # encoded).
    group_name: Optional[str] = None
    announcement: Optional[str] = None
    # edit_message packet: the author's replacement text for [message_id].
    new_content: Optional[str] = None
    # reaction packet: the emoji being toggled on/off for [message_id] by
    # [sender_id] (with [active]). Sanitized + length-capped on parse.
    emoji: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"type": self.type}
        if self.group_id is not None:
            d["groupId"] = self.group_id
        if self.peer is not None:
            d["peer"] = self.peer.to_dict()
        if self.members is not None:
            d["members"] = [p.to_dict() for p in self.members]
        if self.message is not None:
            d["message"] = self.message.to_dict()
        if self.messages is not None:
            d["messages"] = [m.to_dict() for m in self.messages]
        if self.message_id is not None:
            d["messageId"] = self.message_id
        if self.sender_id is not None:
            d["senderId"] = self.sender_id
        if self.error_message is not None:
            d["errorMessage"] = self.error_message
        if self.group_info is not None:
            d["groupInfo"] = self.group_info.to_dict()
        if self.file_info is not None:
            d["fileInfo"] = self.file_info.to_dict()
        if self.file_id is not None:
            d["fileId"] = self.file_id
        if self.target_id is not None:
            d["targetId"] = self.target_id
        if self.call is not None:
            d["call"] = self.call.to_dict()
        if self.host is not None:
            d["host"] = self.host.to_dict()
        if self.deleted_ids:
            d["deletedIds"] = list(self.deleted_ids)
        if self.hs_mode is not None:
            d["hsMode"] = self.hs_mode
        if self.eph is not None:
            d["eph"] = self.eph
        if self.ident is not None:
            d["ident"] = self.ident
        if self.mac is not None:
            d["mac"] = self.mac
        if self.sig is not None:
            d["sig"] = self.sig
        if self.token is not None:
            d["token"] = self.token
        if self.offset is not None:
            d["offset"] = self.offset
        if self.seq is not None:
            d["seq"] = self.seq
        if self.up_to_id is not None:
            d["upToId"] = self.up_to_id
        if self.reader_id is not None:
            d["readerId"] = self.reader_id
        if self.active is not None:
            d["active"] = self.active
        if self.group_name is not None:
            d["groupName"] = self.group_name
        if self.announcement is not None:
            d["announcement"] = self.announcement
        if self.new_content is not None:
            d["newContent"] = self.new_content
        if self.emoji is not None:
            d["emoji"] = self.emoji
        return d

    def to_json(self) -> str:
        # Compact separators so the bytes match kotlinx.serialization's output
        # on the Android side ({"type":"chat",...} without spaces).
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def from_dict(d: dict) -> "NetworkPacket":
        pkt_type = str(d.get("type", ""))
        if not pkt_type:
            raise ValueError("packet missing required field: type")
        pkt = NetworkPacket(type=pkt_type)
        if d.get("groupId") is not None:
            pkt.group_id = str(d["groupId"])
        if d.get("peer") is not None:
            pkt.peer = Peer.from_dict(d["peer"])
        if d.get("members") is not None:
            pkt.members = [Peer.from_dict(m) for m in d["members"]]
        if d.get("errorMessage") is not None:
            pkt.error_message = str(d["errorMessage"])
        if d.get("message") is not None:
            pkt.message = ChatMessage.from_dict(d["message"])
        if d.get("messageId") is not None:
            pkt.message_id = str(d["messageId"])
        if d.get("messages") is not None:
            pkt.messages = [ChatMessage.from_dict(m) for m in d["messages"]]
        if d.get("senderId") is not None:
            pkt.sender_id = str(d["senderId"])
        if d.get("groupInfo") is not None:
            pkt.group_info = GroupInfo.from_dict(d["groupInfo"])
        if d.get("fileInfo") is not None:
            pkt.file_info = FileInfo.from_dict(d["fileInfo"])
        if d.get("fileId") is not None:
            pkt.file_id = str(d["fileId"])
        if d.get("targetId") is not None:
            pkt.target_id = str(d["targetId"])
        if d.get("call") is not None:
            pkt.call = CallInfo.from_dict(d["call"])
        if d.get("host") is not None:
            pkt.host = Peer.from_dict(d["host"])
        if d.get("deletedIds") is not None:
            raw = d["deletedIds"]
            # a list is the only valid shape (the Kotlin side declares
            # List<String>?): a malformed scalar/string must not be iterated
            # into single-character ids
            if isinstance(raw, (list, tuple)):
                pkt.deleted_ids = [str(i) for i in raw]
        if d.get("hsMode") is not None:
            pkt.hs_mode = str(d["hsMode"])
        if d.get("eph") is not None:
            pkt.eph = str(d["eph"])
        if d.get("ident") is not None:
            pkt.ident = str(d["ident"])
        if d.get("mac") is not None:
            pkt.mac = str(d["mac"])
        if d.get("sig") is not None:
            pkt.sig = str(d["sig"])
        if d.get("token") is not None:
            pkt.token = str(d["token"])
        if d.get("offset") is not None:
            pkt.offset = _strict_int(d["offset"], "offset")
        if d.get("seq") is not None:
            pkt.seq = _strict_int(d["seq"], "seq")
        if d.get("upToId") is not None:
            pkt.up_to_id = str(d["upToId"])
        if d.get("readerId") is not None:
            pkt.reader_id = str(d["readerId"])
        if d.get("active") is not None:
            # like the call flags: only a real boolean passes, so a crafted
            # "false"/0/1 cannot silently flip the indicator
            if not isinstance(d["active"], bool):
                raise ValueError("typing field active must be a boolean")
            pkt.active = d["active"]
        if d.get("groupName") is not None:
            pkt.group_name = str(d["groupName"])
        if d.get("announcement") is not None:
            pkt.announcement = str(d["announcement"])
        if d.get("newContent") is not None:
            pkt.new_content = str(d["newContent"])
        if d.get("emoji") is not None:
            pkt.emoji = sanitize_emoji(d["emoji"])
        if pkt_type == "error" and pkt.error_message is None:
            raise ValueError("error packet missing required field: errorMessage")
        if pkt_type == "chat" and pkt.message is None:
            raise ValueError("chat packet missing required field: message")
        if pkt_type == "file_message" and pkt.message is None:
            raise ValueError("file_message packet missing required field: message")
        if pkt_type == "file_download" and not pkt.file_id:
            raise ValueError("file_download packet missing required field: fileId")
        if pkt_type == "file_download":
            # resume is part of the wire contract: a request without (or with
            # a negative) offset is malformed and refused before any bytes
            if pkt.offset is None:
                raise ValueError(
                    "file_download packet missing required field: offset"
                )
            if pkt.offset < 0:
                raise ValueError("file_download offset must be non-negative")
        if pkt_type == "delete_message" and not pkt.message_id:
            raise ValueError("delete_message packet missing required field: messageId")
        # Decode strictness policy (deliberate asymmetry with the Android
        # peer): Windows fails closed — a malformed packet aborts the decode
        # and the connection — while Android's kotlinx model declares these
        # fields nullable and its handlers ignore what is meaningless. See
        # AGENTS.md §2/§5: compat applies to MISSING NEW FIELDS only, never
        # to validation failures; do not loosen these without a protocol
        # decision plus a mixed-version E2E run.
        if pkt_type == "read_receipt" and (not pkt.up_to_id or not pkt.reader_id):
            raise ValueError("read_receipt packet missing required field: upToId or readerId")
        if pkt_type == "typing" and (not pkt.sender_id or pkt.active is None):
            raise ValueError("typing packet missing required field: senderId or active")
        if pkt_type == "group_update" and not pkt.group_id:
            raise ValueError("group_update packet missing required field: groupId")
        if pkt_type == "kick_member" and (not pkt.group_id or not pkt.target_id):
            raise ValueError("kick_member packet missing required field: groupId/targetId")
        if pkt_type == "edit_message":
            if not pkt.message_id or not pkt.sender_id or pkt.new_content is None:
                raise ValueError(
                    "edit_message packet missing required field: messageId/senderId/newContent"
                )
            if not is_valid_content(pkt.new_content):
                raise ValueError("edit_message newContent is not valid content")
        if pkt_type == "reaction":
            if not pkt.message_id or not pkt.sender_id or not pkt.emoji:
                raise ValueError(
                    "reaction packet missing required field: messageId/senderId/emoji"
                )
        if pkt_type == "pin_message" and (not pkt.message_id or not pkt.sender_id):
            raise ValueError(
                "pin_message packet missing required field: messageId/senderId"
            )
        return pkt

    @staticmethod
    def from_json(line: str) -> "NetworkPacket":
        return NetworkPacket.from_dict(json.loads(line))


def is_valid_content(content: str) -> bool:
    return bool(content.strip()) and len(content) <= MAX_CONTENT_LENGTH
