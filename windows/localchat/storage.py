import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from .models import ChatMessage


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
    # True while an own direct-chat message still waits for the peer to come
    # online (pending send). Restored into the outbox at startup.
    pending: bool = False


class ChatStore:
    def __init__(self, db_path: str):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_tables()

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
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
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
                pending INTEGER NOT NULL DEFAULT 0,
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
                 fileSize, downloadHost, downloadPort, pending)
            SELECT id, groupId, content, timestamp, senderId, senderName, isFromMe,
                   {_col('fileSize', '0')}, {_col('downloadHost', "''")},
                   {_col('downloadPort', '0')}, 0
            FROM saved_messages_old
            """
        )
        c.execute("DROP TABLE saved_messages_old")

    def _row_to_group(self, row) -> SavedGroup:
        return SavedGroup(
            group_id=row["groupId"],
            group_name=row["groupName"],
            is_host=bool(row["isHost"]),
            host_ip=row["hostIp"],
            host_port=row["hostPort"] if "hostPort" in row.keys() else 0,
            my_name=row["myName"],
            member_count=row["memberCount"],
            last_message=row["lastMessage"],
            last_message_time=row["lastMessageTime"],
            created_at=row["createdAt"],
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
            )
            self._conn.execute(
                """
                INSERT INTO saved_groups
                (groupId, groupName, isHost, hostIp, hostPort, myName, memberCount, lastMessage, lastMessageTime, createdAt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(groupId) DO UPDATE SET
                    groupName = excluded.groupName,
                    isHost = excluded.isHost,
                    hostIp = excluded.hostIp,
                    hostPort = excluded.hostPort,
                    myName = excluded.myName,
                    memberCount = excluded.memberCount,
                    lastMessage = excluded.lastMessage,
                    lastMessageTime = excluded.lastMessageTime
                """,
                (
                    merged.group_id,
                    merged.group_name,
                    1 if merged.is_host else 0,
                    merged.host_ip,
                    merged.host_port,
                    merged.my_name,
                    merged.member_count,
                    merged.last_message,
                    merged.last_message_time,
                    merged.created_at,
                ),
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
                 fileSize, downloadHost, downloadPort, pending)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        m.id,
                        m.group_id,
                        m.content,
                        m.timestamp,
                        m.sender_id,
                        m.sender_name,
                        1 if m.is_from_me else 0,
                        m.file_size,
                        m.download_host,
                        m.download_port,
                        1 if m.pending else 0,
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
                content=r["content"],
                timestamp=r["timestamp"],
                sender_id=r["senderId"],
                sender_name=r["senderName"],
                is_from_me=bool(r["isFromMe"]),
                file_size=r["fileSize"] if "fileSize" in r.keys() else 0,
                download_host=r["downloadHost"] if "downloadHost" in r.keys() else "",
                download_port=r["downloadPort"] if "downloadPort" in r.keys() else 0,
                pending=bool(r["pending"]) if "pending" in r.keys() else False,
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
                content=r["content"],
                timestamp=r["timestamp"],
                sender_id=r["senderId"],
                sender_name=r["senderName"],
                is_from_me=bool(r["isFromMe"]),
                file_size=r["fileSize"] if "fileSize" in r.keys() else 0,
                download_host=r["downloadHost"] if "downloadHost" in r.keys() else "",
                download_port=r["downloadPort"] if "downloadPort" in r.keys() else 0,
                pending=True,
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
                     fileSize, downloadHost, downloadPort, pending)
                SELECT id, ?, content, timestamp, senderId, senderName, isFromMe,
                       fileSize, downloadHost, downloadPort, pending
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
            self._conn.execute("DELETE FROM saved_groups WHERE groupId = ?", (group_id,))
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
        pending=msg.pending,
    )
