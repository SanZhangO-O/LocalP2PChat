import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from . import secretbox
from .models import FILE_KIND_FILE, ChatMessage


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
    reply_to: str = ""
    reply_preview: str = ""
    reply_sender: str = ""
    # Own direct-chat message read by the peer (set by a read_receipt):
    # survives restart so "已读" does not flip back after a relaunch.
    read: bool = False


class ChatStore:
    # Delete tombstones kept per group: enough for convergence after a short
    # offline period without growing the table forever.
    TOMBSTONE_CAP = 200

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
            if "read" not in cols:
                c.execute(
                    "ALTER TABLE saved_messages ADD COLUMN read INTEGER NOT NULL DEFAULT 0"
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
                read INTEGER NOT NULL DEFAULT 0,
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
                 replyTo, replyPreview, replySender, read)
            SELECT id, groupId, content, timestamp, senderId, senderName, isFromMe,
                   {_col('fileSize', '0')}, {_col('downloadHost', "''")},
                   {_col('downloadPort', '0')}, {_col('kind', "'file'")},
                   {_col('folderId', "''")}, {_col('folderName', "''")},
                   {_col('relativePath', "''")}, {_col('folderTotal', '0')}, 0,
                   {_col('replyTo', "''")}, {_col('replyPreview', "''")},
                   {_col('replySender', "''")}, {_col('read', '0')}
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
            self._conn.executemany(
                """
                INSERT OR REPLACE INTO saved_messages
                (id, groupId, content, timestamp, senderId, senderName, isFromMe,
                 fileSize, downloadHost, downloadPort, kind,
                 folderId, folderName, relativePath, folderTotal, pending,
                 replyTo, replyPreview, replySender, read)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        1 if m.read else 0,
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
                read=bool(r["read"]) if "read" in r.keys() else False,
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
                read=bool(r["read"]) if "read" in r.keys() else False,
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

    def move_messages(self, from_group_id: str, to_group_id: str) -> None:
        """Move every message row from one conversation key to another (used
        when a manually added "ip:..." placeholder chat is revealed to be a
        real device id by the handshake). OR REPLACE: the target chat's
        observer may already have re-inserted some of these rows, and a plain
        UPDATE would then abort on the composite-PK conflict."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO saved_messages
                    (id, groupId, content, timestamp, senderId, senderName, isFromMe,
                     fileSize, downloadHost, downloadPort, kind,
                     folderId, folderName, relativePath, folderTotal, pending,
                     replyTo, replyPreview, replySender, read)
                SELECT id, ?, content, timestamp, senderId, senderName, isFromMe,
                       fileSize, downloadHost, downloadPort, kind,
                       folderId, folderName, relativePath, folderTotal, pending,
                       replyTo, replyPreview, replySender, read
                FROM saved_messages WHERE groupId = ?
                """,
                (to_group_id, from_group_id),
            )
            self._conn.execute(
                "DELETE FROM saved_messages WHERE groupId = ?", (from_group_id,)
            )
            self._conn.commit()

    def delete_group(self, group_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM saved_messages WHERE groupId = ?", (group_id,))
            self._conn.execute("DELETE FROM deleted_messages WHERE group_id = ?", (group_id,))
            self._conn.execute("DELETE FROM saved_groups WHERE groupId = ?", (group_id,))
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
        read=msg.read,
    )
