import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, replace
from typing import List, Optional

from . import secretbox
from .models import FILE_KIND_FILE, MEDIA_VIDEO, ChatMessage, forwarded_to_json


@dataclass
class SavedGroup:
    group_id: str
    group_name: str
    is_host: bool
    host_ip: str = ""
    host_port: int = 0
    my_name: str = ""
    member_count: int = 1
    last_message: str = ""
    last_message_time: int = 0
    created_at: int = 0
    # Group owner (creator) published announcement, persisted so members keep
    # it across restarts and while the host is offline. None means "leave the
    # stored value untouched" (an existing caller building a metadata refresh
    # without an announcement must never clear it); "" clears it.
    announcement: Optional[str] = None
    # True when this member was kicked out of the group: the row is kept so the
    # local chat history survives (messages cascade off saved_groups), but the
    # group is hidden from the group list and can never be rejoined. None means
    # "leave the stored value untouched".
    kicked: Optional[bool] = None


@dataclass
class SavedMessage:
    id: str
    group_id: str
    content: str
    timestamp: int
    sender_id: str
    sender_name: str
    is_from_me: bool
    file_size: int = 0
    download_host: str = ""
    download_port: int = 0
    # file-message kind ("file" | "image" | "video"): media kinds render
    # inline in the conversation, also after a restart
    kind: str = FILE_KIND_FILE
    # Folder transfer metadata (empty/0 for a plain file): survives restart so
    # the grouped folder card still shows its name and entry list.
    folder_id: str = ""
    folder_name: str = ""
    relative_path: str = ""
    folder_total: int = 0
    # True while an own direct-chat message still waits for the peer to come
    # online (pending send). Restored into the outbox at startup.
    pending: bool = False
    # Reply/quote (empty when the message is not a reply): survives restart so
    # the quoted header still renders. Mirrors the wire ChatMessage fields.
    # quote fields: reply_to = quoted id, reply_preview = snippet (encrypted
    # at rest like the body), reply_sender = original sender DISPLAY NAME;
    # quoted_sender = original sender DEVICE id and quoted_kind = the original
    # message's content kind ("text"/file kind), both parsed from the nested
    # wire quote object (empty when only the legacy flat triple arrived).
    reply_to: str = ""
    reply_preview: str = ""
    reply_sender: str = ""
    quoted_sender: str = ""
    quoted_kind: str = ""
    # Forward provenance (compact JSON, "" = not a forward): display-only
    # "转发" badge data, never used for authorization (plaintext like
    # replySender — origin names are display metadata, not content).
    forwarded: str = ""
    # Own direct-chat message read by the peer (set by a read_receipt):
    # survives restart so "已读" does not flip back after a relaunch.
    read: bool = False
    # Message edit: content replaced by its author (edit_message) and the
    # mentioned peer ids (JSON list, "@" mentions). Both survive restart.
    edited: bool = False
    mentions: str = ""


@dataclass
class SavedCallLog:
    """One local call-log entry (never sent over the wire, not synced)."""

    id: str
    conversation_key: str  # "direct:<peer_id>"
    peer_id: str
    peer_name: str
    direction: str  # "incoming" | "outgoing"
    result: str  # answered | missed | rejected | cancelled | failed
    media: str = MEDIA_VIDEO  # "audio" | "video"
    start_time: int = 0  # epoch ms
    duration: int = 0  # seconds actually connected (0 when never answered)


@dataclass
class PendingOp:
    """One staged message-experience operation (edit/reaction/pin) waiting
    for its conversation to become reachable again. The UNIQUE key
    (scope, kind, message_id, emoji) makes staging idempotent: toggling the
    same reaction/pin (or re-editing the same message) while offline UPDATES
    the staged row in place (latest payload + timestamp wins, queue position
    kept), so one user action can never queue duplicates (Android parity:
    PendingOpEntity)."""

    op_id: int
    scope: str  # group id or "direct:<peer_id>"
    kind: str  # "edit" | "reaction" | "pin"
    message_id: str
    emoji: str = ""
    active: bool = False
    content: str = ""  # decrypted edit text (stored encrypted)
    created_at: int = 0


@dataclass
class SavedGroupFile:
    """One entry of a group's shared-file index (群文件). The metadata columns
    are PLAINTEXT at rest (searchable with SQL LIKE); only the download
    snapshot mirrors the wire offer. local_path is local-only (never synced):
    the source path on the uploader, so it can re-serve the bytes."""

    group_id: str
    file_id: str
    name: str
    size: int
    sender_id: str
    sender_name: str = ""
    ts: int = 0
    download_host: str = ""
    download_port: int = 0
    file_key: str = ""
    local_path: str = ""


class ChatStore:
    # Delete tombstones kept per group: enough for convergence after a short
    # offline period without growing the table forever.
    TOMBSTONE_CAP = 200
    # Call-log history kept per conversation (newest kept, oldest trimmed).
    CALL_LOG_CAP = 200
    # Group file share area (群文件): index entries and removal tombstones
    # kept per group (newest kept by ts / removed_at).
    GROUP_FILES_CAP = 500
    REMOVED_GROUP_FILES_CAP = 500

    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        # Chat bodies, last-message previews and group passwords are
        # encrypted at rest under the installation content key (secretbox):
        # a stolen database file alone reveals no conversation content.
        self._data_dir = os.path.dirname(os.path.abspath(db_path)) or "."
        self._init_tables()

    # -------------------------------------------------- at-rest secret layer

    def _enc(self, text: str) -> str:
        try:
            return secretbox.protect(self._data_dir, text)
        except Exception:
            return text

    def _dec(self, text: str) -> str:
        try:
            return secretbox.unprotect(self._data_dir, text)
        except Exception:
            return text

    def get_secret(self, key: str, default: str = "") -> str:
        """A protected settings value ("enc1:..." at rest)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return self._dec(row["value"]) if row is not None else default

    def set_secret(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, self._enc(value)),
            )
            self._conn.commit()

    def delete_secret(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._conn.commit()

    # Group passwords are join credentials: stored encrypted (get_secret),
    # never in the plaintext settings namespace.

    def get_group_password(self, group_id: str) -> str:
        return self.get_secret(f"group_password_{group_id}", "")

    def set_group_password(self, group_id: str, password: str) -> None:
        self.set_secret(f"group_password_{group_id}", password)

    def delete_group_password(self, group_id: str) -> None:
        self.delete_secret(f"group_password_{group_id}")

    def _init_tables(self) -> None:
        with self._lock:
            c = self._conn.cursor()
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_groups (
                    groupId TEXT PRIMARY KEY,
                    groupName TEXT NOT NULL,
                    isHost INTEGER NOT NULL,
                    hostIp TEXT NOT NULL DEFAULT '',
                    hostPort INTEGER NOT NULL DEFAULT 0,
                    myName TEXT NOT NULL DEFAULT '',
                    memberCount INTEGER NOT NULL DEFAULT 1,
                    lastMessage TEXT NOT NULL DEFAULT '',
                    lastMessageTime INTEGER NOT NULL DEFAULT 0,
                    createdAt INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # migrate databases created before multi-group support
            cols = {r[1] for r in c.execute("PRAGMA table_info(saved_groups)").fetchall()}
            if "hostPort" not in cols:
                c.execute(
                    "ALTER TABLE saved_groups ADD COLUMN hostPort INTEGER NOT NULL DEFAULT 0"
                )
            # migrate databases created before group management (owner
            # announcement + kicked marker)
            if "announcement" not in cols:
                c.execute(
                    "ALTER TABLE saved_groups ADD COLUMN announcement TEXT NOT NULL DEFAULT ''"
                )
            if "kicked" not in cols:
                c.execute(
                    "ALTER TABLE saved_groups ADD COLUMN kicked INTEGER NOT NULL DEFAULT 0"
                )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS saved_messages (
                    id TEXT NOT NULL,
                    groupId TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    senderId TEXT NOT NULL,
                    senderName TEXT NOT NULL,
                    isFromMe INTEGER NOT NULL,
                    FOREIGN KEY (groupId) REFERENCES saved_groups(groupId) ON DELETE CASCADE,
                    PRIMARY KEY (groupId, id)
                )
                """
            )
            # migrate databases created before the composite-PK + pending
            # schema: message ids come off the wire and are NOT unique across
            # conversations, so a bare id key let one group's message
            # overwrite/delete another's rows (Android parity).
            self._migrate_saved_messages(c)
            c.execute("CREATE INDEX IF NOT EXISTS idx_msgs_group ON saved_messages(groupId)")
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS deleted_messages (
                    group_id TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    deleted_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, msg_id)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)
                """
            )
            # Message-experience tables (edit/reactions/pins/group read
            # receipts). All FK-cascade off saved_messages' composite PK, so
            # deleting a message (or its whole group) cleans these up.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS message_reactions (
                    group_id TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    emoji TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    FOREIGN KEY (group_id, msg_id) REFERENCES saved_messages(groupId, id) ON DELETE CASCADE,
                    PRIMARY KEY (group_id, msg_id, emoji, actor_id)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS pinned_messages (
                    group_id TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    pinned_at INTEGER NOT NULL,
                    pinned_by TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (group_id, msg_id) REFERENCES saved_messages(groupId, id) ON DELETE CASCADE,
                    PRIMARY KEY (group_id, msg_id)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS group_reads (
                    group_id TEXT NOT NULL,
                    msg_id TEXT NOT NULL,
                    reader_id TEXT NOT NULL,
                    FOREIGN KEY (group_id, msg_id) REFERENCES saved_messages(groupId, id) ON DELETE CASCADE,
                    PRIMARY KEY (group_id, msg_id, reader_id)
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_ops (
                    opId INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    messageId TEXT NOT NULL,
                    emoji TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 0,
                    content TEXT NOT NULL DEFAULT '',
                    createdAt INTEGER NOT NULL,
                    UNIQUE (scope, kind, messageId, emoji)
                )
                """
            )
            self._conn.commit()
            # Local call history: one row per finished call, keyed to the 1:1
            # conversation with the other participant ("direct:<peer_id>").
            # Never sent over the wire and never synced (Android parity).
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS call_logs (
                    id TEXT PRIMARY KEY,
                    conversationKey TEXT NOT NULL,
                    peerId TEXT NOT NULL,
                    peerName TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    result TEXT NOT NULL,
                    media TEXT NOT NULL DEFAULT 'video',
                    startTime INTEGER NOT NULL,
                    duration INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_call_logs_conv ON call_logs(conversationKey, startTime)"
            )
            # Group file share area (群文件): a per-group persistent file
            # index, separate from chat file messages. Metadata columns are
            # plaintext at rest (searchable); downloadHost/downloadPort/
            # fileKey snapshot the uploader's offer (per-file key + download
            # token mechanism shared with chat files). localPath is the
            # uploader's local source (never synced). FK-cascade off the
            # group so removing the group removes its share index.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS group_files (
                    groupId TEXT NOT NULL,
                    fileId TEXT NOT NULL,
                    name TEXT NOT NULL,
                    size INTEGER NOT NULL DEFAULT 0,
                    senderId TEXT NOT NULL,
                    senderName TEXT NOT NULL DEFAULT '',
                    ts INTEGER NOT NULL DEFAULT 0,
                    downloadHost TEXT NOT NULL DEFAULT '',
                    downloadPort INTEGER NOT NULL DEFAULT 0,
                    fileKey TEXT NOT NULL DEFAULT '',
                    localPath TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (groupId) REFERENCES saved_groups(groupId) ON DELETE CASCADE,
                    PRIMARY KEY (groupId, fileId)
                )
                """
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_group_files_group ON group_files(groupId)"
            )
            # Removal tombstones of the share area (offline-member convergence,
            # join_ack / history_reply removedIds), same shape as
            # deleted_messages but capped at REMOVED_GROUP_FILES_CAP.
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS removed_group_files (
                    group_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    removed_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, file_id)
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def _migrate_saved_messages(c) -> None:
        info = c.execute("PRAGMA table_info(saved_messages)").fetchall()
        if not info:
            return  # fresh database — the CREATE above already has the shape
        cols = {r[1] for r in info}
        pk_cols = [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5]]
        if "pending" in cols and pk_cols == ["groupId", "id"]:
            # migrate databases created before file-message support
            if "fileSize" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN fileSize INTEGER NOT NULL DEFAULT 0"
                )
            if "downloadHost" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN downloadHost TEXT NOT NULL DEFAULT ''"
                )
            if "downloadPort" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN downloadPort INTEGER NOT NULL DEFAULT 0"
                )
            if "kind" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'"
                )
            if "folderId" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN folderId TEXT NOT NULL DEFAULT ''"
                )
            if "folderName" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN folderName TEXT NOT NULL DEFAULT ''"
                )
            if "relativePath" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN relativePath TEXT NOT NULL DEFAULT ''"
                )
            if "folderTotal" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN folderTotal INTEGER NOT NULL DEFAULT 0"
                )
            if "replyTo" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN replyTo TEXT NOT NULL DEFAULT ''"
                )
            if "replyPreview" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN replyPreview TEXT NOT NULL DEFAULT ''"
                )
            if "replySender" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN replySender TEXT NOT NULL DEFAULT ''"
                )
            if "quotedSender" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN quotedSender TEXT NOT NULL DEFAULT ''"
                )
            if "quotedKind" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN quotedKind TEXT NOT NULL DEFAULT ''"
                )
            if "forwarded" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN forwarded TEXT NOT NULL DEFAULT ''"
                )
            if "read" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN read INTEGER NOT NULL DEFAULT 0"
                )
            if "edited" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN edited INTEGER NOT NULL DEFAULT 0"
                )
            if "mentions" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN mentions TEXT NOT NULL DEFAULT ''"
                )
            return
        # old schema: rebuild with the composite PK (+ pending) and copy rows
        c.execute("ALTER TABLE saved_messages RENAME TO saved_messages_old")
        c.execute(
            """
            CREATE TABLE saved_messages (
                id TEXT NOT NULL,
                groupId TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                senderId TEXT NOT NULL,
                senderName TEXT NOT NULL,
                isFromMe INTEGER NOT NULL,
                fileSize INTEGER NOT NULL DEFAULT 0,
                downloadHost TEXT NOT NULL DEFAULT '',
                downloadPort INTEGER NOT NULL DEFAULT 0,
                kind TEXT NOT NULL DEFAULT 'file',
                folderId TEXT NOT NULL DEFAULT '',
                folderName TEXT NOT NULL DEFAULT '',
                relativePath TEXT NOT NULL DEFAULT '',
                folderTotal INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0,
                replyTo TEXT NOT NULL DEFAULT '',
                replyPreview TEXT NOT NULL DEFAULT '',
                replySender TEXT NOT NULL DEFAULT '',
                quotedSender TEXT NOT NULL DEFAULT '',
                quotedKind TEXT NOT NULL DEFAULT '',
                forwarded TEXT NOT NULL DEFAULT '',
                read INTEGER NOT NULL DEFAULT 0,
                edited INTEGER NOT NULL DEFAULT 0,
                mentions TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (groupId) REFERENCES saved_groups(groupId) ON DELETE CASCADE,
                PRIMARY KEY (groupId, id)
            )
            """
        )
        # tolerate old tables that predate the optional file columns
        def _col(name: str, default: str) -> str:
            return name if name in cols else default

        c.execute(
            f"""
            INSERT INTO saved_messages
                (id, groupId, content, timestamp, senderId, senderName, isFromMe,
                 fileSize, downloadHost, downloadPort, kind,
                 folderId, folderName, relativePath, folderTotal, pending,
                 replyTo, replyPreview, replySender, quotedSender, quotedKind,
                 forwarded, read, edited, mentions)
            SELECT id, groupId, content, timestamp, senderId, senderName, isFromMe,
                   {_col('fileSize', '0')}, {_col('downloadHost', "''")},
                   {_col('downloadPort', '0')}, {_col('kind', "'file'")},
                   {_col('folderId', "''")}, {_col('folderName', "''")},
                   {_col('relativePath', "''")}, {_col('folderTotal', '0')}, 0,
                   {_col('replyTo', "''")}, {_col('replyPreview', "''")},
                   {_col('replySender', "''")}, {_col('quotedSender', "''")},
                   {_col('quotedKind', "''")}, {_col('forwarded', "''")},
                   {_col('read', '0')},
                   {_col('edited', '0')}, {_col('mentions', "''")}
            FROM saved_messages_old
            """
        )
        c.execute("DROP TABLE saved_messages_old")

    def _row_to_group(self, row) -> SavedGroup:
        keys = row.keys()
        return SavedGroup(
            group_id=row["groupId"],
            group_name=row["groupName"],
            is_host=bool(row["isHost"]),
            host_ip=row["hostIp"],
            host_port=row["hostPort"] if "hostPort" in keys else 0,
            my_name=row["myName"],
            member_count=row["memberCount"],
            last_message=self._dec(row["lastMessage"]),
            last_message_time=row["lastMessageTime"],
            created_at=row["createdAt"],
            announcement=row["announcement"] if "announcement" in keys else "",
            kicked=bool(row["kicked"]) if "kicked" in keys else False,
        )

    def get_all_groups(self) -> List[SavedGroup]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saved_groups ORDER BY createdAt DESC"
            ).fetchall()
        return [self._row_to_group(r) for r in rows]

    def get_group(self, group_id: str) -> Optional[SavedGroup]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saved_groups WHERE groupId = ? LIMIT 1", (group_id,)
            ).fetchone()
        return self._row_to_group(row) if row is not None else None

    def upsert_group(self, group: SavedGroup) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM saved_groups WHERE groupId = ?", (group.group_id,)
            ).fetchone()
            existing = self._row_to_group(row) if row is not None else None
            merged = SavedGroup(
                group_id=group.group_id,
                group_name=group.group_name or (existing.group_name if existing else ""),
                is_host=group.is_host if group.is_host is not None else (existing.is_host if existing else False),
                host_ip=group.host_ip if group.host_ip else (existing.host_ip if existing else ""),
                host_port=group.host_port or (existing.host_port if existing else 0),
                my_name=group.my_name if group.my_name else (existing.my_name if existing else ""),
                member_count=group.member_count,
                last_message=group.last_message,
                last_message_time=group.last_message_time,
                created_at=existing.created_at if existing else group.created_at or int(time.time() * 1000),
                # None = "leave untouched" so metadata refreshes cannot clear
                # the announcement, and an explicit "" clears it
                announcement=(
                    group.announcement
                    if group.announcement is not None
                    else (existing.announcement if existing else "")
                ),
                kicked=(
                    group.kicked
                    if group.kicked is not None
                    else (existing.kicked if existing else False)
                ),
            )
            self._conn.execute(
                """
                INSERT INTO saved_groups
                (groupId, groupName, isHost, hostIp, hostPort, myName, memberCount, lastMessage, lastMessageTime, createdAt, announcement, kicked)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(groupId) DO UPDATE SET
                    groupName = excluded.groupName,
                    isHost = excluded.isHost,
                    hostIp = excluded.hostIp,
                    hostPort = excluded.hostPort,
                    myName = excluded.myName,
                    memberCount = excluded.memberCount,
                    lastMessage = excluded.lastMessage,
                    lastMessageTime = excluded.lastMessageTime,
                    announcement = excluded.announcement,
                    kicked = excluded.kicked
                """,
                (
                    merged.group_id,
                    merged.group_name,
                    1 if merged.is_host else 0,
                    merged.host_ip,
                    merged.host_port,
                    merged.my_name,
                    merged.member_count,
                    # the preview is conversation content: encrypted at rest
                    self._enc(merged.last_message),
                    merged.last_message_time,
                    merged.created_at,
                    merged.announcement or "",
                    1 if merged.kicked else 0,
                ),
            )
            self._conn.commit()

    def set_group_kicked(self, group_id: str, kicked: bool = True) -> None:
        """Flag a group as "removed by the owner": the row (and its message
        history via the FK) stays, but the group is hidden from the list.
        [kicked]=False reverses it (a same-id group created again)."""
        with self._lock:
            self._conn.execute(
                "UPDATE saved_groups SET kicked = ? WHERE groupId = ?",
                (1 if kicked else 0, group_id),
            )
            self._conn.commit()

    def set_group_announcement(self, group_id: str, announcement: str) -> None:
        """Replace a group's owner announcement ("" clears it)."""
        with self._lock:
            self._conn.execute(
                "UPDATE saved_groups SET announcement = ? WHERE groupId = ?",
                (announcement or "", group_id),
            )
            self._conn.commit()

    def insert_message(self, message: SavedMessage) -> None:
        self.insert_messages([message])

    def insert_messages(self, messages: List[SavedMessage]) -> None:
        if not messages:
            return
        with self._lock:
            # UPSERT, never OR REPLACE: replacing a row deletes it first, and
            # the new ON DELETE CASCADE children (reactions / pins / group
            # reads) would silently go with it. DO UPDATE keeps the parent row
            # alive (and refreshes the payload) while child state survives —
            # Android's OnConflictStrategy.IGNORE parity.
            self._conn.executemany(
                """
                INSERT INTO saved_messages
                (id, groupId, content, timestamp, senderId, senderName, isFromMe,
                 fileSize, downloadHost, downloadPort, kind,
                 folderId, folderName, relativePath, folderTotal, pending,
                 replyTo, replyPreview, replySender, quotedSender, quotedKind,
                 forwarded, read, edited, mentions)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(groupId, id) DO UPDATE SET
                    content=excluded.content,
                    timestamp=excluded.timestamp,
                    senderId=excluded.senderId,
                    senderName=excluded.senderName,
                    isFromMe=excluded.isFromMe,
                    fileSize=excluded.fileSize,
                    downloadHost=excluded.downloadHost,
                    downloadPort=excluded.downloadPort,
                    kind=excluded.kind,
                    folderId=excluded.folderId,
                    folderName=excluded.folderName,
                    relativePath=excluded.relativePath,
                    folderTotal=excluded.folderTotal,
                    pending=excluded.pending,
                    replyTo=excluded.replyTo,
                    replyPreview=excluded.replyPreview,
                    replySender=excluded.replySender,
                    quotedSender=excluded.quotedSender,
                    quotedKind=excluded.quotedKind,
                    forwarded=excluded.forwarded,
                    read=excluded.read,
                    edited=excluded.edited,
                    mentions=excluded.mentions
                """,
                [
                    (
                        m.id,
                        m.group_id,
                        # message bodies are the sensitive payload: encrypted
                        # at rest (decrypted transparently on read)
                        self._enc(m.content),
                        m.timestamp,
                        m.sender_id,
                        m.sender_name,
                        1 if m.is_from_me else 0,
                        m.file_size,
                        m.download_host,
                        m.download_port,
                        m.kind,
                        m.folder_id,
                        m.folder_name,
                        m.relative_path,
                        m.folder_total,
                        1 if m.pending else 0,
                        m.reply_to,
                        # the quote snippet is conversation content too
                        self._enc(m.reply_preview),
                        m.reply_sender,
                        m.quoted_sender,
                        m.quoted_kind,
                        m.forwarded,
                        1 if m.read else 0,
                        1 if m.edited else 0,
                        m.mentions,
                    )
                    for m in messages
                ],
            )
            self._conn.commit()

    def get_messages_for_group(self, group_id: str) -> List[SavedMessage]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saved_messages WHERE groupId = ? ORDER BY timestamp ASC",
                (group_id,),
            ).fetchall()
        return [
            SavedMessage(
                id=r["id"],
                group_id=r["groupId"],
                content=self._dec(r["content"]),
                timestamp=r["timestamp"],
                sender_id=r["senderId"],
                sender_name=r["senderName"],
                is_from_me=bool(r["isFromMe"]),
                file_size=r["fileSize"] if "fileSize" in r.keys() else 0,
                download_host=r["downloadHost"] if "downloadHost" in r.keys() else "",
                download_port=r["downloadPort"] if "downloadPort" in r.keys() else 0,
                kind=r["kind"] if "kind" in r.keys() else FILE_KIND_FILE,
                folder_id=r["folderId"] if "folderId" in r.keys() else "",
                folder_name=r["folderName"] if "folderName" in r.keys() else "",
                relative_path=r["relativePath"] if "relativePath" in r.keys() else "",
                folder_total=r["folderTotal"] if "folderTotal" in r.keys() else 0,
                pending=bool(r["pending"]) if "pending" in r.keys() else False,
                reply_to=r["replyTo"] if "replyTo" in r.keys() else "",
                reply_preview=(
                    self._dec(r["replyPreview"])
                    if "replyPreview" in r.keys()
                    else ""
                ),
                reply_sender=r["replySender"] if "replySender" in r.keys() else "",
                quoted_sender=r["quotedSender"] if "quotedSender" in r.keys() else "",
                quoted_kind=r["quotedKind"] if "quotedKind" in r.keys() else "",
                forwarded=r["forwarded"] if "forwarded" in r.keys() else "",
                read=bool(r["read"]) if "read" in r.keys() else False,
                edited=bool(r["edited"]) if "edited" in r.keys() else False,
                mentions=r["mentions"] if "mentions" in r.keys() else "",
            )
            for r in rows
        ]

    def get_pending_direct_messages(self) -> List[SavedMessage]:
        """Undelivered (pending-send) direct-chat messages across all chats;
        re-queued into the outbox at process start (Android parity)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM saved_messages
                WHERE groupId LIKE 'direct:%' AND pending = 1
                ORDER BY timestamp ASC
                """
            ).fetchall()
        return [
            SavedMessage(
                id=r["id"],
                group_id=r["groupId"],
                content=self._dec(r["content"]),
                timestamp=r["timestamp"],
                sender_id=r["senderId"],
                sender_name=r["senderName"],
                is_from_me=bool(r["isFromMe"]),
                file_size=r["fileSize"] if "fileSize" in r.keys() else 0,
                download_host=r["downloadHost"] if "downloadHost" in r.keys() else "",
                download_port=r["downloadPort"] if "downloadPort" in r.keys() else 0,
                kind=r["kind"] if "kind" in r.keys() else FILE_KIND_FILE,
                folder_id=r["folderId"] if "folderId" in r.keys() else "",
                folder_name=r["folderName"] if "folderName" in r.keys() else "",
                relative_path=r["relativePath"] if "relativePath" in r.keys() else "",
                folder_total=r["folderTotal"] if "folderTotal" in r.keys() else 0,
                pending=True,
                reply_to=r["replyTo"] if "replyTo" in r.keys() else "",
                reply_preview=(
                    self._dec(r["replyPreview"])
                    if "replyPreview" in r.keys()
                    else ""
                ),
                reply_sender=r["replySender"] if "replySender" in r.keys() else "",
                quoted_sender=r["quotedSender"] if "quotedSender" in r.keys() else "",
                quoted_kind=r["quotedKind"] if "quotedKind" in r.keys() else "",
                forwarded=r["forwarded"] if "forwarded" in r.keys() else "",
                read=bool(r["read"]) if "read" in r.keys() else False,
                edited=bool(r["edited"]) if "edited" in r.keys() else False,
                mentions=r["mentions"] if "mentions" in r.keys() else "",
            )
            for r in rows
        ]

    def update_message_pending(self, group_id: str, message_id: str, pending: bool) -> None:
        """Flip the persisted delivery state of one message (queued -> sent)."""
        with self._lock:
            self._conn.execute(
                "UPDATE saved_messages SET pending = ? WHERE groupId = ? AND id = ?",
                (1 if pending else 0, group_id, message_id),
            )
            self._conn.commit()

    def update_message_read(self, group_id: str, message_id: str, read: bool) -> None:
        """Flip the persisted read state of one own direct-chat message (the
        peer's read_receipt covered it)."""
        with self._lock:
            self._conn.execute(
                "UPDATE saved_messages SET read = ? WHERE groupId = ? AND id = ?",
                (1 if read else 0, group_id, message_id),
            )
            self._conn.commit()

    # ------------------------------------------------- edit/reactions/pins/reads

    def update_message_content(
        self,
        group_id: str,
        message_id: str,
        content: str,
        sender_id: Optional[str] = None,
    ) -> bool:
        """Apply an author edit to one stored message: replace the body and
        raise the edited flag. Returns False when the row does not exist or —
        when [sender_id] is given — when the stored author differs. The author
        condition is enforced in SQL on purpose: a forged edit packet must
        never rewrite another author's stored text even if a caller skipped
        the network-layer authorization (defense in depth)."""
        with self._lock:
            if sender_id is None:
                cur = self._conn.execute(
                    "UPDATE saved_messages SET content = ?, edited = 1 "
                    "WHERE groupId = ? AND id = ?",
                    (self._enc(content), group_id, message_id),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE saved_messages SET content = ?, edited = 1 "
                    "WHERE groupId = ? AND id = ? AND senderId = ?",
                    (self._enc(content), group_id, message_id, sender_id),
                )
            self._conn.commit()
        return cur.rowcount > 0

    def add_reaction(self, group_id: str, message_id: str, emoji: str, actor_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO message_reactions (group_id, msg_id, emoji, actor_id) "
                "VALUES (?, ?, ?, ?)",
                (group_id, message_id, emoji, actor_id),
            )
            self._conn.commit()

    def remove_reaction(self, group_id: str, message_id: str, emoji: str, actor_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM message_reactions WHERE group_id = ? AND msg_id = ? "
                "AND emoji = ? AND actor_id = ?",
                (group_id, message_id, emoji, actor_id),
            )
            self._conn.commit()

    def get_reactions(self, group_id: str) -> dict:
        """All reactions of one conversation: {msg_id: [(emoji, actor_id), …]}
        (insertion order preserved, so the UI shows the oldest emoji first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT msg_id, emoji, actor_id FROM message_reactions "
                "WHERE group_id = ? ORDER BY rowid ASC",
                (group_id,),
            ).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["msg_id"], []).append((r["emoji"], r["actor_id"]))
        return out

    def set_message_pinned(
        self, group_id: str, message_id: str, pinned: bool, pinned_by: str = "",
        pinned_at: Optional[int] = None,
    ) -> None:
        with self._lock:
            if pinned:
                ts = int(time.time() * 1000) if pinned_at is None else int(pinned_at)
                self._conn.execute(
                    "INSERT OR REPLACE INTO pinned_messages "
                    "(group_id, msg_id, pinned_at, pinned_by) VALUES (?, ?, ?, ?)",
                    (group_id, message_id, ts, pinned_by),
                )
            else:
                self._conn.execute(
                    "DELETE FROM pinned_messages WHERE group_id = ? AND msg_id = ?",
                    (group_id, message_id),
                )
            self._conn.commit()

    def get_pinned_messages(self, group_id: str) -> List[tuple]:
        """[(msg_id, pinned_at, pinned_by)] oldest pin first (the banner shows
        the newest, i.e. the last entry)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT msg_id, pinned_at, pinned_by FROM pinned_messages "
                "WHERE group_id = ? ORDER BY pinned_at ASC",
                (group_id,),
            ).fetchall()
        return [(r["msg_id"], r["pinned_at"], r["pinned_by"]) for r in rows]

    def record_group_reads(self, group_id: str, msg_ids, reader_id: str) -> int:
        """Persist "reader_id has read these own messages" rows (idempotent).
        Returns how many NEW rows were inserted (0 = a repeat receipt carried
        no news), so the caller can skip the UI refresh entirely — receipts
        arrive for every member and every new message."""
        ids = [str(i) for i in dict.fromkeys(msg_ids or []) if i]
        if not ids or not reader_id:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO group_reads (group_id, msg_id, reader_id) "
                "VALUES (?, ?, ?)",
                [(group_id, i, reader_id) for i in ids],
            )
            inserted = self._conn.total_changes - before
            self._conn.commit()
        return inserted

    def get_group_readers(self, group_id: str) -> dict:
        """{msg_id: [reader_id, …]} of recorded group read receipts."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT msg_id, reader_id FROM group_reads WHERE group_id = ?",
                (group_id,),
            ).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(r["msg_id"], []).append(r["reader_id"])
        return out

    def move_messages(self, from_group_id: str, to_group_id: str) -> None:
        """Move every message row from one conversation key to another (used
        when a manually added "ip:..." placeholder chat is revealed to be a
        real device id by the handshake). UPSERT (never OR REPLACE): the target
        chat's observer may already hold some of these rows, and REPLACE would
        delete the conflicting parent row — taking the target conversation's
        reactions / pins / read receipts with it via ON DELETE CASCADE."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO saved_messages
                    (id, groupId, content, timestamp, senderId, senderName, isFromMe,
                     fileSize, downloadHost, downloadPort, kind,
                     folderId, folderName, relativePath, folderTotal, pending,
                     replyTo, replyPreview, replySender, quotedSender, quotedKind,
                     forwarded, read, edited, mentions)
                SELECT id, ?, content, timestamp, senderId, senderName, isFromMe,
                       fileSize, downloadHost, downloadPort, kind,
                       folderId, folderName, relativePath, folderTotal, pending,
                       replyTo, replyPreview, replySender, quotedSender, quotedKind,
                       forwarded, read, edited, mentions
                FROM saved_messages WHERE groupId = ?
                ON CONFLICT(groupId, id) DO UPDATE SET
                    content=excluded.content,
                    timestamp=excluded.timestamp,
                    senderId=excluded.senderId,
                    senderName=excluded.senderName,
                    isFromMe=excluded.isFromMe,
                    fileSize=excluded.fileSize,
                    downloadHost=excluded.downloadHost,
                    downloadPort=excluded.downloadPort,
                    kind=excluded.kind,
                    folderId=excluded.folderId,
                    folderName=excluded.folderName,
                    relativePath=excluded.relativePath,
                    folderTotal=excluded.folderTotal,
                    pending=excluded.pending,
                    replyTo=excluded.replyTo,
                    replyPreview=excluded.replyPreview,
                    replySender=excluded.replySender,
                    quotedSender=excluded.quotedSender,
                    quotedKind=excluded.quotedKind,
                    forwarded=excluded.forwarded,
                    read=excluded.read,
                    edited=excluded.edited,
                    mentions=excluded.mentions
                """,
                (to_group_id, from_group_id),
            )
            # the message-experience rows follow their message: re-keyed AFTER
            # the copied rows exist (their FK references the new group key) and
            # BEFORE the source rows are deleted (the FK cascade would
            # otherwise drop them). UPDATE OR REPLACE only collapses a
            # duplicate (same emoji/actor or same pin) into one row.
            for table in ("message_reactions", "pinned_messages", "group_reads"):
                self._conn.execute(
                    f"UPDATE OR REPLACE {table} SET group_id = ? WHERE group_id = ?",
                    (to_group_id, from_group_id),
                )
            self._conn.execute(
                "DELETE FROM saved_messages WHERE groupId = ?", (from_group_id,)
            )
            self._conn.commit()

    def get_message_timestamp(self, group_id: str, message_id: str) -> Optional[int]:
        """The timestamp of one stored message (None when absent): the cheap
        lookup the group read-receipt path needs instead of decrypting the
        whole conversation."""
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamp FROM saved_messages WHERE groupId = ? AND id = ?",
                (group_id, message_id),
            ).fetchone()
        return None if row is None else int(row["timestamp"])

    def get_own_message_ids_upto(self, group_id: str, timestamp: int) -> List[str]:
        """Ids of own messages at or before [timestamp] — the coverage cut-off
        of a group read receipt, computed in SQL without loading bodies."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM saved_messages "
                "WHERE groupId = ? AND isFromMe = 1 AND timestamp <= ?",
                (group_id, int(timestamp)),
            ).fetchall()
        return [r["id"] for r in rows]

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM saved_messages WHERE groupId = ?", (group_id,))
            self._conn.execute("DELETE FROM deleted_messages WHERE group_id = ?", (group_id,))
            self._conn.execute("DELETE FROM pending_ops WHERE scope = ?", (group_id,))
            # group_files cascades off saved_groups, but its removal tombstones
            # have no FK: clear them explicitly so a re-created group does not
            # inherit stale tombstones
            self._conn.execute(
                "DELETE FROM removed_group_files WHERE group_id = ?", (group_id,)
            )
            self._conn.execute("DELETE FROM saved_groups WHERE groupId = ?", (group_id,))
            # local call history of a removed conversation must go with it
            self._conn.execute(
                "DELETE FROM call_logs WHERE conversationKey = ?", (group_id,)
            )
            # the join credential and join id of a removed group must not
            # linger in the settings table
            self._conn.execute(
                "DELETE FROM settings WHERE key IN (?, ?)",
                (f"group_password_{group_id}", f"group_join_id_{group_id}"),
            )
            self._conn.commit()

    def delete_message(self, group_id: str, message_id: str) -> None:
        """Delete one message of one conversation — keyed by (group, id):
        message ids arrive from the network and are not unique across
        conversations (Android parity)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM saved_messages WHERE groupId = ? AND id = ?",
                (group_id, message_id),
            )
            self._conn.commit()

    def record_deleted_messages(
        self, group_id: str, msg_ids, deleted_at: Optional[int] = None
    ) -> None:
        """Persist delete tombstones for [group_id]: a member that was offline
        during a delete replays them on rejoin (join_ack / history_reply
        deletedIds) so deleted messages converge instead of resurrecting.
        Only the newest TOMBSTONE_CAP ids per group are kept (by deleted_at)."""
        ids = [str(i) for i in dict.fromkeys(msg_ids or []) if i]
        if not ids:
            return
        ts = int(time.time() * 1000) if deleted_at is None else int(deleted_at)
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO deleted_messages (group_id, msg_id, deleted_at) VALUES (?, ?, ?)",
                [(group_id, i, ts) for i in ids],
            )
            self._conn.execute(
                """
                DELETE FROM deleted_messages
                WHERE group_id = ? AND msg_id NOT IN (
                    SELECT msg_id FROM deleted_messages WHERE group_id = ?
                    ORDER BY deleted_at DESC LIMIT ?
                )
                """,
                (group_id, group_id, self.TOMBSTONE_CAP),
            )
            self._conn.commit()

    def get_deleted_ids(self, group_id: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT msg_id FROM deleted_messages WHERE group_id = ? ORDER BY deleted_at DESC",
                (group_id,),
            ).fetchall()
        return [r["msg_id"] for r in rows]

    # --------------------------------------------------- group file share area

    def upsert_group_file(self, entry: SavedGroupFile) -> None:
        """Insert or refresh one entry of a group's share index (dedup by
        (groupId, fileId)). A newer row never loses the local source path we
        already had: the wire entry carries no localPath, so an upsert from
        the network keeps the stored one (only the uploader knows it)."""
        with self._lock:
            if not entry.local_path:
                row = self._conn.execute(
                    "SELECT localPath FROM group_files WHERE groupId = ? AND fileId = ?",
                    (entry.group_id, entry.file_id),
                ).fetchone()
                if row is not None:
                    entry = replace(entry, local_path=row["localPath"] or "")
            self._conn.execute(
                """
                INSERT INTO group_files
                    (groupId, fileId, name, size, senderId, senderName, ts,
                     downloadHost, downloadPort, fileKey, localPath)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(groupId, fileId) DO UPDATE SET
                    name = excluded.name,
                    size = excluded.size,
                    senderId = excluded.senderId,
                    senderName = excluded.senderName,
                    ts = excluded.ts,
                    downloadHost = excluded.downloadHost,
                    downloadPort = excluded.downloadPort,
                    fileKey = excluded.fileKey,
                    localPath = excluded.localPath
                """,
                (
                    entry.group_id,
                    entry.file_id,
                    entry.name,
                    int(entry.size),
                    entry.sender_id,
                    entry.sender_name,
                    int(entry.ts),
                    entry.download_host,
                    int(entry.download_port),
                    entry.file_key,
                    entry.local_path,
                ),
            )
            self._prune_group_files_locked(entry.group_id)
            self._conn.commit()

    def get_group_files(self, group_id: str) -> List[SavedGroupFile]:
        """The group's share index, newest first, tombstoned ids excluded.
        This is the source fed to join_ack/history_reply convergence packets."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM group_files
                WHERE groupId = ?
                  AND fileId NOT IN (
                      SELECT file_id FROM removed_group_files WHERE group_id = ?
                  )
                ORDER BY ts DESC, fileId DESC
                """,
                (group_id, group_id),
            ).fetchall()
        return [self._row_to_group_file(r) for r in rows]

    def get_group_file(self, group_id: str, file_id: str) -> Optional[SavedGroupFile]:
        """One share-index entry, or None. Tombstoned ids are invisible (a
        removed file never reappears through a lookup)."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM group_files
                WHERE groupId = ? AND fileId = ?
                  AND fileId NOT IN (
                      SELECT file_id FROM removed_group_files WHERE group_id = ?
                  )
                """,
                (group_id, file_id, group_id),
            ).fetchone()
        return None if row is None else self._row_to_group_file(row)

    def get_local_group_files(self) -> List[SavedGroupFile]:
        """Every share-index entry this device itself uploaded (it has a local
        source path). Startup uses it to re-register the served bytes and
        refresh the advertised address/port (the shared listener port and the
        local IP can change across restarts)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM group_files WHERE localPath != ''"
            ).fetchall()
        return [self._row_to_group_file(r) for r in rows]

    @staticmethod
    def _row_to_group_file(r) -> SavedGroupFile:
        return SavedGroupFile(
            group_id=r["groupId"],
            file_id=r["fileId"],
            name=r["name"],
            size=int(r["size"]),
            sender_id=r["senderId"],
            sender_name=r["senderName"],
            ts=int(r["ts"]),
            download_host=r["downloadHost"],
            download_port=int(r["downloadPort"]),
            file_key=r["fileKey"],
            local_path=r["localPath"],
        )

    def record_removed_group_files(
        self, group_id: str, file_ids, removed_at: Optional[int] = None
    ) -> None:
        """Persist share-area removal tombstones and drop the matching index
        rows. A member that was offline during the removal replays them on
        rejoin (join_ack / history_reply removedIds) so removed files converge
        instead of resurrecting (message-tombstone semantics)."""
        ids = [str(i) for i in dict.fromkeys(file_ids or []) if i]
        if not ids:
            return
        ts = int(time.time() * 1000) if removed_at is None else int(removed_at)
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO removed_group_files (group_id, file_id, removed_at) "
                "VALUES (?, ?, ?)",
                [(group_id, i, ts) for i in ids],
            )
            self._conn.executemany(
                "DELETE FROM group_files WHERE groupId = ? AND fileId = ?",
                [(group_id, i) for i in ids],
            )
            self._conn.execute(
                """
                DELETE FROM removed_group_files
                WHERE group_id = ? AND file_id NOT IN (
                    SELECT file_id FROM removed_group_files WHERE group_id = ?
                    ORDER BY removed_at DESC LIMIT ?
                )
                """,
                (group_id, group_id, self.REMOVED_GROUP_FILES_CAP),
            )
            self._conn.commit()

    def get_removed_group_file_ids(self, group_id: str) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT file_id FROM removed_group_files WHERE group_id = ? "
                "ORDER BY removed_at DESC",
                (group_id,),
            ).fetchall()
        return [r["file_id"] for r in rows]

    def _prune_group_files_locked(self, group_id: str) -> None:
        """Keep only the newest GROUP_FILES_CAP entries of one group (caller
        holds the lock)."""
        self._conn.execute(
            """
            DELETE FROM group_files
            WHERE groupId = ? AND fileId NOT IN (
                SELECT fileId FROM group_files WHERE groupId = ?
                ORDER BY ts DESC, fileId DESC LIMIT ?
            )
            """,
            (group_id, group_id, self.GROUP_FILES_CAP),
        )

    # ------------------------------------------------------- pending op log

    def stage_pending_op(
        self,
        scope: str,
        kind: str,
        message_id: str,
        emoji: str = "",
        active: bool = False,
        content: str = "",
        created_at: Optional[int] = None,
    ) -> None:
        """Stage one offline message-experience op for [scope]. Idempotent:
        an op with the same (scope, kind, message_id, emoji) is updated in
        place (latest payload/timestamp, queue position kept — Android parity:
        ChatDao.stagePendingOp). The edit text is conversation content and is
        encrypted at rest like the message bodies."""
        ts = int(time.time() * 1000) if created_at is None else int(created_at)
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE pending_ops SET active = ?, content = ?, createdAt = ?
                WHERE scope = ? AND kind = ? AND messageId = ? AND emoji = ?
                """,
                (1 if active else 0, self._enc(content), ts, scope, kind, message_id, emoji),
            )
            if cur.rowcount == 0:
                self._conn.execute(
                    """
                    INSERT INTO pending_ops
                        (scope, kind, messageId, emoji, active, content, createdAt)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (scope, kind, message_id, emoji, 1 if active else 0, self._enc(content), ts),
                )
            self._conn.commit()

    def _row_to_pending_op(self, r) -> PendingOp:
        return PendingOp(
            op_id=int(r["opId"]),
            scope=r["scope"],
            kind=r["kind"],
            message_id=r["messageId"],
            emoji=r["emoji"],
            active=bool(r["active"]),
            content=self._dec(r["content"]),
            created_at=int(r["createdAt"]),
        )

    def get_pending_ops(self, scope: str) -> List[PendingOp]:
        """Staged ops of one conversation in staging order (oldest first)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_ops WHERE scope = ? ORDER BY opId ASC",
                (scope,),
            ).fetchall()
        return [self._row_to_pending_op(r) for r in rows]

    def get_all_pending_ops(self) -> dict:
        """{scope: [PendingOp, …]} across every conversation (startup view);
        scopes keep their ops in staging order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pending_ops ORDER BY opId ASC"
            ).fetchall()
        out: dict = {}
        for r in rows:
            op = self._row_to_pending_op(r)
            out.setdefault(op.scope, []).append(op)
        return out

    def delete_pending_op(self, op_id: int) -> None:
        """Drop one staged op after it was replayed successfully."""
        with self._lock:
            self._conn.execute("DELETE FROM pending_ops WHERE opId = ?", (op_id,))
            self._conn.commit()

    def delete_pending_ops(self, scope: str) -> None:
        """Drop every staged op of one conversation (e.g. the conversation was
        removed while ops were still staged)."""
        with self._lock:
            self._conn.execute("DELETE FROM pending_ops WHERE scope = ?", (scope,))
            self._conn.commit()

    # ------------------------------------------------------------- call logs

    def add_call_log(self, entry: SavedCallLog) -> None:
        """Persist one finished call and keep only the newest CALL_LOG_CAP
        entries of its conversation (oldest trimmed)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO call_logs
                    (id, conversationKey, peerId, peerName, direction, result,
                     media, startTime, duration)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.id,
                    entry.conversation_key,
                    entry.peer_id,
                    entry.peer_name,
                    entry.direction,
                    entry.result,
                    entry.media or MEDIA_VIDEO,
                    entry.start_time,
                    entry.duration,
                ),
            )
            self._conn.execute(
                """
                DELETE FROM call_logs
                WHERE conversationKey = ? AND id NOT IN (
                    SELECT id FROM call_logs WHERE conversationKey = ?
                    ORDER BY startTime DESC, rowid DESC LIMIT ?
                )
                """,
                (entry.conversation_key, entry.conversation_key, self.CALL_LOG_CAP),
            )
            self._conn.commit()

    def get_call_logs(self, conversation_key: str, limit: int = CALL_LOG_CAP) -> List[SavedCallLog]:
        """Newest [limit] call logs of one conversation, oldest first (so the
        UI can interleave them into the message flow by timestamp)."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM call_logs WHERE conversationKey = ?
                ORDER BY startTime DESC, rowid DESC LIMIT ?
                """,
                (conversation_key, limit),
            ).fetchall()
        return [
            SavedCallLog(
                id=r["id"],
                conversation_key=r["conversationKey"],
                peer_id=r["peerId"],
                peer_name=r["peerName"],
                direction=r["direction"],
                result=r["result"],
                media=r["media"] if "media" in r.keys() else MEDIA_VIDEO,
                start_time=r["startTime"],
                duration=r["duration"],
            )
            for r in reversed(rows)
        ]

    def move_call_logs(self, from_key: str, to_key: str) -> None:
        """Re-key a conversation's call logs (used when a manually added
        "ip:..." placeholder chat is revealed to be a real device id)."""
        if from_key == to_key:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE OR REPLACE call_logs SET conversationKey = ? WHERE conversationKey = ?",
                (to_key, from_key),
            )
            self._conn.commit()

    def delete_call_logs(self, conversation_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM call_logs WHERE conversationKey = ?", (conversation_key,)
            )
            self._conn.commit()

    @staticmethod
    def escape_like(keyword: str) -> str:
        r"""Escape SQL LIKE wildcards in [keyword] (and the escape char itself)
        so the result is a literal-substring pattern: "a%b_c" -> "a\%b\_c".
        Pair with ``LIKE ? ESCAPE '\'``."""
        return (
            keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )

    def search_messages(
        self, keyword: str, group_id: Optional[str] = None, limit: int = 200
    ) -> List[SavedMessage]:
        """Keyword search over persisted history, newest match first.

        Message bodies (and only they) are encrypted at rest (secretbox), so
        SQL LIKE prefilters the plaintext-at-rest columns (sender name, file
        path, folder name); the body pass decrypts the scope's rows and
        matches in Python — a plain LIKE on the ciphertext column would
        silently match nothing. Direct chats live under "direct:<peerId>"
        group keys, so one query covers group and 1:1 history.
        """
        kw = (keyword or "").strip()
        if not kw:
            return []
        like = f"%{self.escape_like(kw)}%"
        clauses = [
            "("
            "senderName LIKE ? ESCAPE '\\'"
            " OR relativePath LIKE ? ESCAPE '\\'"
            " OR folderName LIKE ? ESCAPE '\\'"
            ")",
        ]
        params: List = [like, like, like]
        if group_id is not None:
            clauses.append("groupId = ?")
            params.append(group_id)
        body_sql = "SELECT * FROM saved_messages"
        if group_id is not None:
            body_sql += " WHERE groupId = ?"
        with self._lock:
            sql = (
                "SELECT * FROM saved_messages WHERE "
                + " AND ".join(clauses)
                + f" ORDER BY timestamp DESC LIMIT {int(limit)}"
            )
            rows = list(self._conn.execute(sql, params).fetchall())
            # body pass: (groupId, id, content) of the whole scope, newest
            # first — deduped against the name-column matches
            body_rows = self._conn.execute(
                body_sql + " ORDER BY timestamp DESC",
                [group_id] if group_id is not None else [],
            ).fetchall()
        found = {(r["groupId"], r["id"]) for r in rows}
        for r in body_rows:
            key = (r["groupId"], r["id"])
            if key in found:
                continue
            # case-insensitive like the SQL LIKE prefilter above (SQLite LIKE
            # is ASCII-case-insensitive, so the two passes must agree)
            if kw.lower() in self._dec(self._raw_content(r)).lower():
                rows.append(r)
        rows.sort(key=lambda r: r["timestamp"], reverse=True)
        return [self._row_to_message(r) for r in rows[:limit]]

    def _raw_content(self, row) -> str:
        return row["content"]

    def _row_to_message(self, r) -> SavedMessage:
        keys = r.keys()
        return SavedMessage(
            id=r["id"],
            group_id=r["groupId"],
            content=self._dec(r["content"]),
            timestamp=r["timestamp"],
            sender_id=r["senderId"],
            sender_name=r["senderName"],
            is_from_me=bool(r["isFromMe"]),
            file_size=r["fileSize"] if "fileSize" in keys else 0,
            download_host=r["downloadHost"] if "downloadHost" in keys else "",
            download_port=r["downloadPort"] if "downloadPort" in keys else 0,
            kind=r["kind"] if "kind" in keys else FILE_KIND_FILE,
            folder_id=r["folderId"] if "folderId" in keys else "",
            folder_name=r["folderName"] if "folderName" in keys else "",
            relative_path=r["relativePath"] if "relativePath" in keys else "",
            folder_total=r["folderTotal"] if "folderTotal" in keys else 0,
            pending=bool(r["pending"]) if "pending" in keys else False,
            reply_to=r["replyTo"] if "replyTo" in keys else "",
            # the quote snippet is conversation content too (encrypted at rest)
            reply_preview=self._dec(r["replyPreview"]) if "replyPreview" in keys else "",
            reply_sender=r["replySender"] if "replySender" in keys else "",
            quoted_sender=r["quotedSender"] if "quotedSender" in keys else "",
            quoted_kind=r["quotedKind"] if "quotedKind" in keys else "",
            forwarded=r["forwarded"] if "forwarded" in keys else "",
            read=bool(r["read"]) if "read" in keys else False,
            edited=bool(r["edited"]) if "edited" in keys else False,
            mentions=r["mentions"] if "mentions" in keys else "",
        )

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def to_saved_message(group_id: str, msg: ChatMessage) -> SavedMessage:
    fi = msg.file_info
    return SavedMessage(
        id=msg.id,
        group_id=group_id,
        content=msg.content,
        timestamp=msg.timestamp,
        sender_id=msg.sender_id,
        sender_name=msg.sender_name,
        is_from_me=msg.is_from_me,
        file_size=fi.file_size if fi is not None else 0,
        download_host=fi.download_host if fi is not None else "",
        download_port=fi.download_port if fi is not None else 0,
        kind=fi.kind if fi is not None else FILE_KIND_FILE,
        folder_id=fi.folder_id if fi is not None else "",
        folder_name=fi.folder_name if fi is not None else "",
        relative_path=fi.relative_path if fi is not None else "",
        folder_total=fi.folder_total if fi is not None else 0,
        pending=msg.pending,
        reply_to=msg.reply_to or "",
        reply_preview=msg.reply_preview or "",
        reply_sender=msg.reply_sender or "",
        quoted_sender=msg.quote_sender or "",
        quoted_kind=msg.quote_kind or "",
        forwarded=forwarded_to_json(msg.forwarded),
        read=msg.read,
        edited=msg.edited,
        mentions=json.dumps(msg.mentions) if msg.mentions else "",
    )
