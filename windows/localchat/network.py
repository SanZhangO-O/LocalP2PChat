import copy
import ipaddress
import logging
import os
import queue
import socket
import struct
import threading
import time
import uuid
from typing import Dict, List, Optional

from .crypto import (
    KEY_LEN,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    constant_time_equals,
    from_b64,
    hmac_sha256,
    random_bytes,
    to_b64,
)
from . import groupauth
from .hardware import get_hardware_id, get_local_ip_address
from .models import (
    FILE_KIND_FILE,
    MAX_FOLDER_FILES,
    MAX_GROUP_FILE_REMOVED,
    MAX_GROUP_FILES,
    MAX_LINE_LENGTH,
    TCP_PORT,
    ChatMessage,
    ContactRequest,
    FileInfo,
    ForwardedInfo,
    GroupFileInfo,
    GroupInfo,
    NetworkPacket,
    Peer,
    detect_media_kind,
    is_valid_content,
    sanitize_emoji,
    sanitize_file_name,
    sanitize_relative_path,
)
from .punch import (
    PUNCH_TIMEOUT,
    PunchError,
    ROLE_MEMBER,
    punch_connect,
)
from .securewire import (
    DeviceIdentity,
    Handshake,
    Protocol,
    Wire,
    WireException,
)

logger = logging.getLogger(__name__)

# Packet types that carry call signaling (see docs/video_call_protocol.md).
# These are delivered to the registered call listener (never shown as chat
# messages) and, when sent by a member with targetId set, are routed by the
# host only to the addressed member instead of being broadcast.
CALL_PACKET_TYPES = frozenset(
    {"call_offer", "call_answer", "call_reject", "call_hangup", "call_failed"}
)

# Group voice conference signaling (star topology: the meeting host mixes).
# Routed by the host like call packets (actor must be the authenticated
# sender; delivered only to the addressed member), but handed to the separate
# group_call listener so the 1:1 call path is untouched. Old peers ignore
# these unknown types (both read loops are if/elif chains without else).
GROUP_CALL_PACKET_TYPES = frozenset(
    {"group_call_invite", "group_call_join", "group_call_leave", "group_call_sync"}
)

# Hard cap on a single downloaded file. Protects storage from a broken or
# malicious sender that streams far more than the offer declared (Android
# parity: FileTransfer.MAX_DOWNLOAD_BYTES).
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024

# A file download server lives at most this long, even when nobody
# downloads: previously a sent file held its ServerSocket + accept thread
# forever (one FD/thread per file until the process died).
FILE_SERVER_TTL = 15 * 60.0

CHUNK_SIZE = 64 * 1024

# Largest ciphertext chunk frame on the wire: plaintext + nonce + GCM tag —
# exactly the overhead aes_gcm_encrypt adds per chunk.
MAX_CHUNK_WIRE = CHUNK_SIZE + 12 + 16

GCM_MIN_FRAME = 12 + 16

# file_download token domain separator (Android parity): the downloader proves
# it received the encrypted offer by HMAC-ing the fileId with the per-file key.
FILE_DL_TOKEN_PREFIX = "lc-file-dl-v1:"

# Tombstone intake bounds for ONE packet (see ChatStore.TOMBSTONE_CAP): the
# deleted_messages table keeps at most that many ids per group, so a peer gains
# nothing by sending more — but a hostile or merely large list must not make us
# do unbounded work (state updates, DB rows). Message ids are UUID-sized.
MAX_DELETED_IDS = 200
MAX_DELETED_ID_LEN = 128

# Group file share area (群文件) wire bounds. The index cap lives in models
# (MAX_GROUP_FILES); the push budget keeps ONE history_reply batch safely
# under the line cap — 500 entries with names/keys would not fit a single
# 64KB line, so pushes are split like history batches (Android parity:
# GroupFiles.PUSH_BUDGET_BYTES). join_ack is a single packet and carries at
# most the newest JOIN_ACK_GROUP_FILES_CAP entries; the rest converges over
# the mesh history push.
GROUP_FILE_PUSH_BUDGET_BYTES = 32 * 1024
JOIN_ACK_GROUP_FILES_CAP = 120


def sanitize_id_list(ids, cap: int, max_len: int = MAX_DELETED_ID_LEN) -> list:
    """Dedupe, drop blanks/oversized ids and cap an id list received from the
    wire (delete tombstones and group-file removal tombstones share the
    shape). Android parity: sanitizeDeletedIds / GroupFiles.sanitizeRemovedIds."""
    if isinstance(ids, str):
        # a malformed wire value ("removedIds": "abc") must not iterate into
        # single-character ids
        ids = [ids]
    out = []
    seen = set()
    for raw in ids or []:
        mid = str(raw)
        if not mid.strip() or len(mid) > max_len or mid in seen:
            continue
        seen.add(mid)
        out.append(mid)
        if len(out) >= cap:
            break
    return out


def sanitize_deleted_ids(deleted_ids) -> list:
    """Dedupe, drop blanks/oversized ids and cap the tombstone list of ONE
    packet (join_ack / history_reply). Android parity:
    P2PManager.sanitizeDeletedIds."""
    return sanitize_id_list(deleted_ids, MAX_DELETED_IDS)


def sanitize_group_files(raw) -> list:
    """Sanitize an inbound group-file index (join_ack / history_reply
    groupFiles): valid entries only, deduped by fileId (first wins), capped
    at MAX_GROUP_FILES. A malformed entry is skipped, never fatal — one
    crafted entry must not break the whole convergence packet."""
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    seen = set()
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        try:
            entry = GroupFileInfo.from_dict(item)
        except Exception:
            continue
        if entry.file_id in seen:
            continue
        seen.add(entry.file_id)
        out.append(entry)
        if len(out) >= MAX_GROUP_FILES:
            break
    return out


def can_remove_group_file(sender_id: str, entry_sender_id: str, creator_id: str) -> bool:
    """Removal authorization for the group file share area: the uploader or
    the group owner. Pure so both the relay host, the mesh path and the
    storage layer's caller agree (Android parity: GroupFiles.canRemove)."""
    if not sender_id:
        return False
    return sender_id == entry_sender_id or (bool(creator_id) and sender_id == creator_id)



def _configure_server_socket(srv: socket.socket) -> None:
    """Apply platform-appropriate exclusive-bind options to a LISTENING socket.

    On Windows SO_REUSEADDR has hijack semantics, so prefer
    SO_EXCLUSIVEADDRUSE there; on POSIX keep the normal SO_REUSEADDR behavior
    used by tests and quick restarts.
    """
    if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

def numeric_group_id_of(group_name: str, fingerprint: str) -> str:
    """Stable 8-digit numeric id for a group: FNV-1a hash of the machine
    fingerprint + group name, iterated over UTF-16 code units — Kotlin's
    `for (ch in s)` walks UTF-16 units (surrogate PAIRS for astral chars),
    while Python code points would differ once a name contains e.g. emoji.
    BMP-only names hash identically to the old code-point iteration, so
    existing groups keep their ids. Used as the join identifier — members
    type this instead of the group name."""
    hash_ = 0x811C9DC5
    units = f"{group_name}\u0000{fingerprint}".encode("utf-16-le")
    for i in range(0, len(units), 2):
        hash_ ^= units[i] | (units[i + 1] << 8)
        hash_ = (hash_ * 0x01000193) & 0xFFFFFFFF
    digits = (hash_ % 100_000_000 + 100_000_000) % 100_000_000
    return str(digits).zfill(8)


def format_numeric_group_id(group_id: str) -> str:
    """'1234 5678' display form of a numeric group id."""
    return " ".join(group_id[i : i + 4] for i in range(0, len(group_id), 4))


def _send_line_simple(sock: socket.socket, line: str) -> None:
    try:
        sock.sendall((line + "\n").encode("utf-8"))
    except OSError:
        pass


def _read_line_bounded(reader) -> Optional[str]:
    """Read one newline-terminated frame with Android-compatible boundary
    semantics: content up to MAX_LINE_LENGTH characters is accepted, anything
    longer returns None so callers close the connection. Shared by every
    line-based TCP reader (host server, group relay, direct chats, group mesh,
    file handshake) so no path can buffer without a bound.

    readline(MAX_LINE_LENGTH + 1) returns the trailing newline for a line
    that fits exactly, so an exactly-at-the-limit line is accepted while a
    longer line (no newline within the read budget) is rejected.
    """
    try:
        line = reader.readline(MAX_LINE_LENGTH + 1)
        if not line:
            return None
        if len(line) > MAX_LINE_LENGTH and not line.endswith("\n"):
            return None
        return line.rstrip("\r\n")
    except Exception:
        return None


def _read_raw_line(sock: socket.socket, limit: int = MAX_LINE_LENGTH) -> Optional[str]:
    """Read one newline-terminated line straight from the socket, one byte
    at a time, with no buffered wrapper. Used before streaming binary data
    (direct chat and call-media handshakes on raw sockets, file downloads) so
    the following bytes are never swallowed by read-ahead buffering. Returns
    None on EOF (or when the line exceeds [limit])."""
    buf = bytearray()
    try:
        while len(buf) <= limit:
            b = sock.recv(1)
            if not b:
                return None if not buf else bytes(buf).decode("utf-8", "replace")
            if b == b"\n":
                return bytes(buf).decode("utf-8", "replace").rstrip("\r")
            buf.extend(b)
    except OSError:
        return None
    return None


def make_wire(sock: socket.socket, reader=None) -> Wire:
    """Build a Wire over a socket. [reader] is a buffered text wrapper for
    connections that stay line-based after the handshake (host server, group
    relay, mesh, direct chats); omit it ONLY for connections that switch to
    binary framing after the handshake (call media) or read the meta line
    before a raw byte stream (file downloads) — the raw byte-at-a-time line
    reader never read-aheads, at the cost of one syscall per byte."""
    if reader is not None:
        read = lambda: _read_line_bounded(reader)
    else:
        read = lambda: _read_raw_line(sock)

    def write(line: str) -> None:
        sock.sendall((line + "\n").encode("utf-8"))

    return Wire(read, write)


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def file_download_token(file_key: bytes, file_id: str) -> str:
    """Sender-proof token for a file_download request (Android parity,
    contract "lc-file-dl-v1"): base64_std(HMAC-SHA256(key=fileKey (32B),
    msg=ascii("lc-file-dl-v1:" + fileId))). Only a peer that received the
    encrypted offer knows the per-file key, so the sender can refuse
    un-invited download probes that already know just the (plaintext)
    request format. Standard Base64 with padding, byte-comparable."""
    return to_b64(
        hmac_sha256(file_key, f"{FILE_DL_TOKEN_PREFIX}{file_id}".encode("ascii"))
    )


def _serve_file_download(
    sock: socket.socket, file_id: str, path: str, file_size: int, file_key: bytes
) -> None:
    """Serve one file-download connection (random-port chat file servers).
    Reads the plaintext handshake line, then delegates to
    [_serve_file_download_parsed]."""
    try:
        sock.settimeout(30)
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        handshake = _read_line_bounded(reader)
        if handshake is None:
            return
        try:
            req = NetworkPacket.from_json(handshake)
        except Exception:
            # includes a request without the (now mandatory) offset field
            return
        _serve_file_download_parsed(sock, req, file_id, path, file_size, file_key)
    except Exception:
        pass
    finally:
        _graceful_close_send(sock)


def _serve_file_download_parsed(
    sock: socket.socket, req: NetworkPacket, file_id: str, path: str, file_size: int, file_key: bytes
) -> None:
    """Validate an already-parsed file_download request and stream the file.
    Handshake (Android parity, see FileTransfer.kt):

        receiver -> "file_download" {fileId, token, offset}            (plaintext)
        sender   -> ENCRYPTED LINE: AES-GCM(fileKey, file_meta JSON)
        sender   -> [4B ctLen][12B nonce][AES-GCM chunk]... [4B zero EOF]

    [offset] (bytes the receiver already holds) is REQUIRED and the stream
    starts there: a fresh download sends 0, an interrupted one resumes from
    the size of its ".part" staging file. An offset past the declared size is
    refused outright (no meta, no bytes). Each chunk is an independent AEAD
    message with a fresh random nonce, so seeking does not change the GCM
    layout at all — no nonce/AAD state depends on the offset.

    The per-file key travels only inside the (itself encrypted) chat message
    that offered the file, so a passive sniffer sees ciphertext for the meta
    line AND the byte stream, and tampering anywhere trips the GCM tag and
    aborts the download. The token is MANDATORY: base64(HMAC-SHA256(key=
    fileKey, msg="lc-file-dl-v1:" + fileId)) proves the downloader received
    the encrypted offer — a request without it (or with a wrong one) is
    closed without meta or bytes, verified in constant time.

    Also used for the group file share area (群文件): the request line then
    arrives on the shared listener, which looks the fileId up in its
    shared-file registry before calling this."""
    try:
        if req.type != "file_download" or req.file_id != file_id:
            return
        if req.offset is None or req.offset < 0:
            return
        if file_size >= 0 and req.offset > file_size:
            logger.warning(
                "file download rejected: offset %s past size %s for %s",
                req.offset,
                file_size,
                file_id,
            )
            return
        if not req.token:
            # no token = the requester never received the (encrypted) offer:
            # refuse without sending meta or bytes
            logger.warning(
                "file download rejected: missing token for %s", file_id
            )
            return
        try:
            expected = hmac_sha256(
                file_key, f"{FILE_DL_TOKEN_PREFIX}{file_id}".encode("ascii")
            )
            provided = from_b64(req.token)
        except Exception:
            return
        if not constant_time_equals(provided, expected):
            logger.warning(
                "file download rejected: token mismatch for %s", file_id
            )
            return
        meta = NetworkPacket(
            type="file_meta",
            file_info=FileInfo(file_id, os.path.basename(path), file_size, "", 0),
        )
        _send_line_simple(
            sock, to_b64(aes_gcm_encrypt(file_key, meta.to_json().encode("utf-8")))
        )
        sock.settimeout(120)
        with open(path, "rb") as f:
            f.seek(req.offset)
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                blob = aes_gcm_encrypt(file_key, chunk)
                sock.sendall(struct.pack(">I", len(blob)) + blob)
        # explicit EOF marker: a truncated stream is detected before the
        # completeness check, not silently accepted
        sock.sendall(struct.pack(">I", 0))
    except Exception:
        pass
    finally:
        _graceful_close_send(sock)


def _graceful_close_send(sock: socket.socket) -> None:
    # Graceful close: flush the remaining send buffer before FIN. A
    # full shutdown(SHUT_RDWR) on Windows can discard buffered tail
    # bytes, which would corrupt the last chunk of a transfer.
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _download_file_offer(
    file_info: FileInfo,
    target_path: str,
    progress=None,
    cancel: Optional[threading.Event] = None,
    sock_holder: Optional[list] = None,
    offset: int = 0,
) -> tuple:
    """Download a file offered via [file_info] to [target_path]. Blocks the
    calling thread. Returns (ok: bool, message: str). The meta line and every
    chunk are decrypted with the per-file key from the (encrypted) offer; any
    tampering or key mismatch aborts.

    Resume: bytes are appended to "target_path + .part" and the request tells
    the sender to stream from that offset. [offset] is the receiver's
    persisted "already received" count; it is clamped down to the actual
    ".part" size, so a stale count can never skip unauthenticated bytes (a
    missing/short part just restarts lower, never higher). On cancel or any
    network failure the ".part" is KEPT and the caller persists the received
    count so the next attempt resumes; only a part that can never complete
    (bigger than the declared size) is dropped. On success the part is
    finished and atomically renamed onto [target_path].

    [progress](received, total) fires per decrypted chunk (the caller
    throttles; [received] is cumulative, i.e. it includes the resumed
    offset); [cancel] (threading.Event) aborts with "下载已取消"; and
    [sock_holder] (a caller-owned list) receives the socket so the canceler
    can shut a blocked read down immediately. The request always carries the
    file_download token."""
    if file_info.file_size < 0:
        return False, "文件大小无效"
    if cancel is not None and cancel.is_set():
        return False, "下载已取消"
    if file_info.file_size > MAX_DOWNLOAD_BYTES:
        return False, f"文件过大（超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB 限制）"
    try:
        file_key = from_b64(file_info.file_key) if file_info.file_key else None
    except Exception:
        file_key = None
    if file_key is None or len(file_key) != KEY_LEN:
        return False, "文件密钥缺失或无效"
    tmp_path = target_path + ".part"
    try:
        start = int(offset)
    except (TypeError, ValueError):
        start = 0
    if start < 0:
        start = 0
    # a count past the offer's declared size can never be completed (the
    # sender would refuse the request): restart from scratch instead of
    # looping forever on an unusable part
    if file_info.file_size > 0 and start > file_info.file_size:
        start = 0
    try:
        part_size = os.path.getsize(tmp_path)
    except OSError:
        part_size = 0
    if start > part_size:
        # persisted count is ahead of the staging file (crash between write
        # and state update): resume from what actually exists
        start = part_size
    try:
        sock = socket.create_connection(
            (file_info.download_host, file_info.download_port), timeout=10
        )
    except OSError as e:
        return False, f"连接失败: {e}"
    if sock_holder is not None:
        sock_holder.append(sock)
    try:
        if cancel is not None and cancel.is_set():
            # the cancel landed during connect: fall through (do NOT return
            # before the try) so the socket/tmp cleanup in `finally` runs
            return False, "下载已取消"
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(10)
        _send_line_simple(
            sock,
            NetworkPacket(
                type="file_download",
                file_id=file_info.file_id,
                token=file_download_token(file_key, file_info.file_id),
                offset=start,
            ).to_json(),
        )
        # read the meta line from the raw socket, byte by byte: a buffered
        # wrapper would read-ahead and swallow the file frames that the
        # sender streams immediately after the meta line
        line = _read_raw_line(sock)
        if line is None:
            if cancel is not None and cancel.is_set():
                return False, "下载已取消"
            return False, "发送方无响应"
        try:
            blob = from_b64(line)
            meta_json = aes_gcm_decrypt(file_key, blob)
        except Exception:
            return False, "文件密钥不匹配或数据损坏"
        try:
            meta = NetworkPacket.from_json(meta_json.decode("utf-8"))
        except Exception:
            return False, "无效的响应"
        if meta.type != "file_meta" or meta.file_info is None:
            return False, "无效的响应"
        if meta.file_info.file_size < 0:
            return False, "文件大小无效"
        if meta.file_info.file_id != file_info.file_id:
            return False, "文件不匹配"
        # The streamed size is governed by whichever declared size is SMALLER:
        # an oversized claim must not lift the cap, and an undersized claim
        # gets caught by the completeness check below.
        offer_size = (
            file_info.file_size if 0 < file_info.file_size <= MAX_DOWNLOAD_BYTES else 0
        )
        meta_size = (
            meta.file_info.file_size
            if 0 < meta.file_info.file_size <= MAX_DOWNLOAD_BYTES
            else 0
        )
        if offer_size > 0 and meta_size > 0:
            expected = min(offer_size, meta_size)
        else:
            expected = offer_size or meta_size
        # expected == 0 means the size is unknown (some content providers do
        # not report one): the completeness checks below are skipped, and
        # progress falls back to the offer's declared size (Android parity:
        # FileTransfer.totalForProgress).
        progress_total = expected if expected > 0 else file_info.file_size
        if expected > 0 and start > expected:
            # the staged bytes exceed the declared size: this part can never
            # complete, drop it so the next tap starts clean
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False, "断点数据损坏，请重新下载"
        sock.settimeout(120)
        received = start
        eof_marker = False
        # "r+b" appends onto the staged bytes (truncating any excess a stale
        # resume count might leave behind); "wb" is a fresh download
        mode = "r+b" if start > 0 else "wb"
        with open(tmp_path, mode) as f:
            if start > 0:
                f.truncate(start)
                f.seek(start)
            while not eof_marker:
                if cancel is not None and cancel.is_set():
                    return False, "下载已取消"
                header = _recv_exact(sock, 4)
                if header is None:
                    if cancel is not None and cancel.is_set():
                        return False, "下载已取消"
                    return False, "文件传输中断"
                frame_len = int.from_bytes(header, "big")
                if frame_len == 0:
                    eof_marker = True
                    break
                if frame_len < GCM_MIN_FRAME or frame_len > MAX_CHUNK_WIRE:
                    return False, "文件数据损坏"
                frame = _recv_exact(sock, frame_len)
                if frame is None:
                    if cancel is not None and cancel.is_set():
                        return False, "下载已取消"
                    return False, "文件传输中断"
                try:
                    plain = aes_gcm_decrypt(file_key, frame)
                except Exception:
                    return False, "文件数据校验失败（可能被篡改）"
                received += len(plain)
                if expected > 0 and received > expected:
                    return False, f"文件大小不符（已接收 {received} 字节，超过声明大小）"
                if received > MAX_DOWNLOAD_BYTES:
                    return False, f"文件超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB 限制"
                f.write(plain)
                if progress is not None:
                    try:
                        progress(received, progress_total)
                    except Exception:
                        pass
        # only verify a known target size; 0 means unknown (Android parity)
        if expected > 0 and received != expected:
            return False, f"文件不完整（{received}/{expected} 字节）"
        os.replace(tmp_path, target_path)
        return True, ""
    except Exception as e:
        return False, f"下载失败: {e}"
    finally:
        # Interrupted/cancelled downloads KEEP the .part staging file so the
        # next attempt resumes from its size; the ViewModel records the
        # received count for the UI. A completed download renamed the part
        # away; an unusable part was removed explicitly above.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def _spawn(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


def _allow_handshake_bucket(
    attempts: dict, ip: str, limit: int, window: float, now: float
) -> bool:
    """Token-bucket core shared by every TCP listener (HostGroupServer and
    the legacy per-instance P2PManager server): at most [limit] handshake
    attempts per source IP per [window] seconds, with dead IPs pruned so the
    map cannot grow without bound as LAN hosts probe and vanish."""
    with_attempts = attempts
    timestamps = with_attempts.setdefault(ip, [])
    timestamps[:] = [ts for ts in timestamps if now - ts < window]
    for dead_ip in [
        key
        for key, stamps in with_attempts.items()
        if key != ip and all(now - ts >= window for ts in stamps)
    ]:
        del with_attempts[dead_ip]
    if len(timestamps) >= limit:
        return False
    timestamps.append(now)
    return True


class HostGroupServer:
    """Single TCP listener for the whole program.

    The app uses ONE port: every host group registers here and the shared
    server dispatches incoming connections to the group named in the
    handshake. Only one listener socket exists, so multiple host groups stay
    reachable through the same address. Member connections are then owned by
    their group's P2PManager (per-group broadcast/heartbeats are unaffected).

    Every connection starts with the secured handshake from securewire.py
    (password-bound ECDH for query/join/mesh, identity-signed ECDH for direct
    chats); after it every line is AES-256-GCM encrypted. Legacy plaintext
    packets are rejected — there is no downgrade.

    The listener also serves direct member chats and mesh links; it keeps
    running even when no host group is registered (it is only stopped by an
    explicit [shutdown]).
    """

    MAX_ACTIVE_CONNECTIONS = 256
    HANDSHAKE_RATE_LIMIT = 60
    HANDSHAKE_RATE_WINDOW = 60.0

    def __init__(self, port: int):
        self.port = port
        self._groups: Dict[str, "P2PManager"] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
        self._active_handlers = 0
        self._handshake_attempts: Dict[str, list] = {}
        # Bind-retry generation: bumped by every stop()/restart so a retry
        # loop from a superseded listener can never commit a second binding
        # (Android parity: P2PManager's generation counter).
        self._listener_generation = 0
        # The direct-chat manager that auto-accepts secured direct sessions
        # (set by the ViewModel; every device runs it so members can pull up
        # 1:1 chats with no confirmation).
        self.direct_manager: Optional["DirectChatManager"] = None
        # The group-mesh manager that auto-accepts mesh links (set by the
        # ViewModel; members link directly so chat survives the host being
        # offline, and history backfills on connect).
        self.mesh_manager: Optional["GroupMeshManager"] = None
        # Resolves the group password for an incoming handshake (set by the
        # ViewModel, which sees host groups, member groups and mesh groups).
        # Returns None when this device knows no such group for that mode, ""
        # for a known group without a password.
        self.password_lookup = None
        # Optional handler for query/join packets that target a group this
        # device belongs to as a MEMBER (set by the ViewModel). Any member can
        # be the join entry point, not just the creator: the handler answers
        # group_info / join_ack (with the member list and the host's address)
        # and announces the newcomer over the mesh. Returns True when the
        # packet was handled. The wire is already secured (password verified
        # during the handshake).
        self.member_group_handler = None
        # Cross-NAT join support: one bridge per configured signaling server;
        # each registers the hosted groups there and feeds punched/relayed
        # member connections into _handle (see enable_signaling_servers).
        self._signaling_bridges: List = []
        # Group file share area (群文件): fileId -> {path, size, key} of files
        # this device shares into its groups' share areas. A file_download
        # request line arriving on the shared listener is served from here
        # (token + per-file-key GCM, same contract as the chat file servers)
        # without a group handshake: the listener port is stable across
        # restarts, so an index entry's offer stays valid while the app runs.
        self._shared_files: Dict[str, dict] = {}

    # -------------------------------------------------- group shared files

    def register_shared_file(self, file_id: str, path: str, size: int, file_key_b64: str) -> None:
        """Start serving [file_id] from [path] on the shared listener port.
        Registration is app-lifetime (not tied to a group connection), so a
        member that is "in the group but reconnecting" keeps serving its
        shared files. Re-registering the same id replaces the entry."""
        with self._lock:
            self._shared_files[file_id] = {
                "path": path,
                "size": int(size),
                "key": file_key_b64,
            }

    def unregister_shared_file(self, file_id: str) -> None:
        with self._lock:
            self._shared_files.pop(file_id, None)

    def _lookup_shared_file(self, file_id: Optional[str]) -> Optional[dict]:
        if not file_id:
            return None
        with self._lock:
            return self._shared_files.get(file_id)

    # ------------------------------------------------------------- lifecycle

    def register(self, p2p: "P2PManager") -> None:
        with self._lock:
            old = self._groups.get(p2p.group_name)
            self._groups[p2p.group_name] = p2p
            # A group registered while the listener sits in bind-retry must
            # see the CURRENT error (the retry loop only reports the first
            # failure, which may predate this registration — Android parity:
            # the error lives in each group's published state).
            error = self.error
        # A same-name re-host replaces the previous registration: stop the old
        # instance so its heartbeats and sockets do not leak. stop() unregisters
        # conditionally, so it cannot remove the fresh registration.
        if old is not None and old is not p2p:
            old.stop()
        if error is not None:
            # the group registered while the listener sits in bind-retry: hand
            # it the CURRENT error (Android parity — the retry loop only reports
            # the first failure, which may predate this registration)
            p2p.server_error = error
            try:
                p2p.listener.server_error(p2p, error)
            except Exception:
                pass
        self._notify_signaling()
        self._ensure_running()

    def unregister(self, p2p: "P2PManager") -> None:
        with self._lock:
            # conditional remove: an old same-name instance being stopped must
            # not unregister the replacement that is already registered
            if self._groups.get(p2p.group_name) is p2p:
                self._groups.pop(p2p.group_name, None)
        self._notify_signaling()
        # keep listening: the shared port also serves direct member chats

    def rename_registration(self, p2p: "P2PManager", old_name: str) -> None:
        """Re-key a host group after the owner renamed it (group_update): the
        registration key is the display name, and a stale entry under the old
        name would keep resolving join handshakes to a dead name."""
        with self._lock:
            if self._groups.get(old_name) is p2p:
                self._groups.pop(old_name, None)
            # never clobber another group that already registered under the
            # new name (p2p.group_name is already updated when this runs)
            current = self._groups.get(p2p.group_name)
            if current is None or current is p2p:
                self._groups[p2p.group_name] = p2p

    def restart(self, port: Optional[int] = None) -> None:
        """Rebind the listener (e.g. after a bind failure or a port change).
        Existing member connections stay alive — they are owned by the groups."""
        if port is not None:
            self.port = port
        self.stop()
        self._ensure_running()

    def shutdown(self) -> None:
        """Stop the listener for good (app teardown)."""
        self.disable_signaling()
        self.stop()

    def ensure_running(self) -> None:
        """Make sure the shared port is being listened on (direct chats need
        it even on devices with no host group)."""
        self._ensure_running()

    # ------------------------------------------------------------- signaling

    def enable_signaling(
        self, server_host: str, server_port: int, secret: str = ""
    ) -> None:
        """Register every hosted group on ONE signaling server (convenience
        form of enable_signaling_servers)."""
        self.enable_signaling_servers([(server_host, server_port, secret)])

    def enable_signaling_servers(self, specs: List) -> None:
        """Start one signaling bridge per server in [specs] — (host, port,
        secret) tuples of punch.py.SignalingHostBridge — so members on other
        NAT segments can join without this device being reachable directly.
        [secret] is that deployment's server access secret
        (challenge-response authenticated; see signaling_server.py)."""
        from .punch import SignalingHostBridge

        self.disable_signaling()
        for server_host, server_port, secret in specs:
            bridge = SignalingHostBridge(server_host, server_port, self, secret=secret)
            bridge.start()
            self._signaling_bridges.append(bridge)

    def disable_signaling(self) -> None:
        bridges, self._signaling_bridges = self._signaling_bridges, []
        for bridge in bridges:
            bridge.stop()

    def signaling_groups(self) -> list:
        """[(numeric join id, group name)] for every group currently hosted
        here (read by the signaling bridge on socket threads)."""
        with self._lock:
            return [
                (p2p.numeric_group_id, p2p.group_name)
                for p2p in self._groups.values()
                if p2p.is_host
            ]

    def _notify_signaling(self) -> None:
        for bridge in self._signaling_bridges:
            bridge.notify_groups_changed()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            # invalidate any in-flight bind retry of the current generation
            self._listener_generation += 1
            sock = self._server_socket
            self._server_socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def has_groups(self) -> bool:
        with self._lock:
            return bool(self._groups)

    def _ensure_running(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._server_loop, daemon=True)
            self._thread.start()

    # ------------------------------------------------------------- server

    def _allow_handshake(self, ip: str) -> bool:
        """Cheap token bucket that stops one LAN host from forcing unbounded
        PBKDF2 handshakes / thread spawns. Legitimate group traffic is far
        below the limit (60 handshakes/minute/IP)."""
        return _allow_handshake_bucket(
            self._handshake_attempts,
            ip,
            self.HANDSHAKE_RATE_LIMIT,
            self.HANDSHAKE_RATE_WINDOW,
            time.monotonic(),
        )

    def _handle_guarded(self, sock: socket.socket) -> None:
        try:
            self._handle(sock)
        finally:
            with self._lock:
                self._active_handlers = max(0, self._active_handlers - 1)

    # Bind-retry cadence (Android parity: P2PManager retries every 3s).
    BIND_RETRY_INTERVAL = 3.0

    def _server_loop(self) -> None:
        """Bind the shared port, retrying every BIND_RETRY_INTERVAL until it
        succeeds or the server is stopped/port-changed: a transient conflict
        (another instance, a port not yet released) previously killed the
        listener until the user manually retried. Only the FIRST failure is
        reported to the UI/log; a recovery is logged once."""
        with self._lock:
            gen = self._listener_generation
        srv = None
        error_reported = False
        while srv is None:
            if self._stop_event.is_set():
                return
            with self._lock:
                if gen != self._listener_generation:
                    return
            candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                _configure_server_socket(candidate)
                candidate.bind(("0.0.0.0", self.port))
                candidate.listen(16)
                candidate.settimeout(1.0)
                srv = candidate
            except OSError:
                try:
                    candidate.close()
                except OSError:
                    pass
                if not error_reported:
                    error_reported = True
                    logger.warning(
                        "failed to bind shared port %s, retrying every %ss",
                        self.port,
                        self.BIND_RETRY_INTERVAL,
                        exc_info=True,
                    )
                    self._set_error(
                        f"无法监听端口 {self.port}，请检查端口是否被占用或防火墙设置"
                        f"（Windows 防火墙需允许入站 TCP {self.port}）"
                    )
                self._stop_event.wait(self.BIND_RETRY_INTERVAL)
        with self._lock:
            if gen != self._listener_generation or self._server_socket is not None:
                # superseded by a stop()/restart() while we were retrying
                try:
                    srv.close()
                except OSError:
                    pass
                return
            self._server_socket = srv
        if error_reported:
            logger.info("shared port %s recovered after bind failures", self.port)
        self._set_error(None)
        while not self._stop_event.is_set():
            try:
                client, client_addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            ip = client_addr[0] if client_addr else ""
            if ip and not self._allow_handshake(ip):
                self._safe_close(client)
                continue
            with self._lock:
                if self._active_handlers >= self.MAX_ACTIVE_CONNECTIONS:
                    self._safe_close(client)
                    continue
                self._active_handlers += 1
            try:
                _spawn(self._handle_guarded, client)
            except Exception:
                # thread spawn failed (e.g. process thread limit): undo the
                # reservation or the cap leaks one slot per failure
                with self._lock:
                    self._active_handlers = max(0, self._active_handlers - 1)
                self._safe_close(client)
        try:
            srv.close()
        except OSError:
            pass
        self._server_socket = None

    def _handle(self, sock: socket.socket, allow_query_join: bool = False) -> None:
        """Read the hs_start handshake line, secure the connection, then
        dispatch by mode. Only the secured handshake is accepted — a legacy
        plaintext query_group/join/direct_hello/mesh_hello never works.

        [allow_query_join] (signaling-server path) lets one connection carry
        a query FIRST (the member learns the group display name) and then a
        follow-up join handshake on the SAME socket: the punched/relayed
        cross-NAT channel is far too expensive to establish twice. LAN
        connections keep the one-mode-per-connection behavior.
        """
        try:
            sock.settimeout(15)
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            while True:
                first_line = _read_line_bounded(reader)
                if first_line is None:
                    return
                try:
                    start = NetworkPacket.from_json(first_line)
                except Exception:
                    return
                if start.type == "file_download":
                    # group file share area (群文件): a download request for a
                    # shared file arrives on this listener without a group
                    # handshake. The token inside the request proves the
                    # downloader received the (encrypted) group_file_add, and
                    # every chunk is GCM-verified with the per-file key — the
                    # same security contract as the chat file servers, with
                    # no PBKDF2 work to protect, so the handshake rate limit
                    # bucket already spent on this connection covers it.
                    shared = self._lookup_shared_file(start.file_id)
                    if shared is None:
                        self._safe_close(sock)
                        return
                    try:
                        file_key = from_b64(shared["key"])
                    except Exception:
                        self._safe_close(sock)
                        return
                    _serve_file_download_parsed(
                        sock,
                        start,
                        start.file_id,
                        shared["path"],
                        shared["size"],
                        file_key,
                    )
                    return
                if start.type != Protocol.HS_START:
                    return
                wire = make_wire(sock, reader)
                mode = start.hs_mode
                if mode == Protocol.MODE_DIRECT:
                    secured = Handshake.accept_direct(wire, start, None)
                    if secured is None:
                        self._safe_close(sock)
                        return
                    try:
                        hello = wire.recv_packet()
                    except WireException:
                        self._safe_close(sock)
                        return
                    if hello is None or hello.type != Protocol.DIRECT_HELLO:
                        self._safe_close(sock)
                        return
                    dm = self.direct_manager
                    if dm is None:
                        self._safe_close(sock)
                        return
                    dm.handle_direct_hello(sock, wire, hello, secured.peer_ident)
                    return
                if mode == Protocol.MODE_MESH:
                    mm = self.mesh_manager
                    lookup = self.password_lookup
                    if mm is None or lookup is None:
                        self._safe_close(sock)
                        return
                    secured = Handshake.accept(wire, start, lookup)
                    if secured is None:
                        self._safe_close(sock)
                        return
                    try:
                        hello = wire.recv_packet()
                    except WireException:
                        self._safe_close(sock)
                        return
                    if hello is None or hello.type != "mesh_hello":
                        self._safe_close(sock)
                        return
                    if hello.group_id != start.group_id:
                        # same rule as query/join: the password was verified for
                        # start.groupId, so the hello must not name another group
                        # (whose mesh state and history it must not reach)
                        self._safe_close(sock)
                        return
                    mm.handle_mesh_hello(sock, wire, hello)
                    return
                if mode not in (Protocol.MODE_QUERY, Protocol.MODE_JOIN):
                    self._safe_close(sock)
                    return
                p2p = self.resolve_group(start.group_id)
                lookup = self.password_lookup
                if lookup is None:
                    self._safe_close(sock)
                    return
                secured = Handshake.accept(wire, start, lookup)
                if secured is None:
                    self._safe_close(sock)
                    return
                try:
                    packet = wire.recv_packet()
                except WireException:
                    self._safe_close(sock)
                    return
                if packet is None or packet.type != mode:
                    self._safe_close(sock)
                    return
                if packet.group_id != start.group_id:
                    # The inner packet must name the SAME group the handshake
                    # authenticated: the password was verified for
                    # start.groupId, so letting a different id through would
                    # let a member of group A use A's password to probe group
                    # B's data over the member-sponsor path.
                    self._safe_close(sock)
                    return
                if p2p is None:
                    handler = self.member_group_handler
                    handled = False
                    if handler is not None:
                        try:
                            handled = bool(handler(packet, sock, wire))
                        except Exception:
                            handled = False
                    if not handled:
                        try:
                            wire.send_packet(NetworkPacket(type="join_rejected"))
                        except Exception:
                            pass
                        self._safe_close(sock)
                    return
                if mode == Protocol.MODE_QUERY:
                    kept = p2p._handle_query_group(
                        sock, wire, packet, keep_open=allow_query_join
                    )
                    if not kept:
                        return
                    # query answered with the socket kept open: the next
                    # hs_start on this connection must be the join
                    continue
                # the group's P2PManager takes over the socket (join_ack,
                # member registration, then its read loop)
                p2p._handle_join(sock, wire, packet)
                return
        except Exception:
            self._safe_close(sock)

    def resolve_group(self, id_or_name: Optional[str]) -> Optional["P2PManager"]:
        """Lock-safe group resolution for callers on ANY thread (the
        handshake password lookup runs on socket threads, concurrently with
        register/unregister)."""
        with self._lock:
            return self._resolve_group(id_or_name)

    def _resolve_group(self, id_or_name: Optional[str]) -> Optional["P2PManager"]:
        """Resolve a group by its 8-digit numeric join id ONLY (Android
        parity, P2PManager.resolveGroup): the group name is a display label,
        never an addressable join identifier, so a name sent in a handshake
        must not resolve. Callers must hold self._lock or use
        resolve_group()."""
        if not id_or_name:
            return None
        for candidate in self._groups.values():
            if candidate.numeric_group_id == id_or_name:
                return candidate
        return None

    def _safe_close(self, sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _set_error(self, message: Optional[str]) -> None:
        """Publish the server state to every registered host group so the
        lobby can show (and clear) the bind error. A fresh successful bind is
        silent; only recoveries (previous error -> ok) notify the UI."""
        with self._lock:
            was_error = self.error is not None
            self.error = message
            groups = list(self._groups.values())
        for p2p in groups:
            p2p.server_error = message
            if message is None and not was_error:
                continue
            try:
                p2p.listener.server_error(p2p, message)
            except Exception:
                pass


class P2PListener:
    def peers_changed(self, p2p: "P2PManager") -> None:
        pass

    def messages_changed(self, p2p: "P2PManager") -> None:
        pass

    def connection_lost(self, p2p: "P2PManager") -> None:
        pass

    def server_error(self, p2p: "P2PManager", message: str) -> None:
        pass

    def query_result_changed(self, p2p: "P2PManager") -> None:
        pass

    def join_state_changed(self, p2p: "P2PManager") -> None:
        pass

    def deleted_ids_received(self, p2p: "P2PManager", deleted_ids) -> None:
        """join_ack carried tombstone convergence data: [deleted_ids] are
        message ids deleted in the group while this member was away."""
        pass

    # --------------------------------------------- group file share callbacks

    def group_file_add_received(self, p2p: "P2PManager", entry: GroupFileInfo) -> None:
        """A member shared a file into the group share area (group_file_add;
        the relay/mesh path has already validated the sender binding)."""
        pass

    def group_file_remove_received(
        self, p2p: "P2PManager", file_id: str, sender_id: str
    ) -> bool:
        """A group_file_remove arrived. The implementation authorizes
        (uploader or group owner), applies it and returns whether the removal
        was valid — the relay host only rebroadcasts when this returns True
        (LESSONS 2026-09-19 #3: a rejected packet must neither persist nor
        propagate)."""
        return False

    def group_files_received(
        self, p2p: "P2PManager", entries, removed_ids
    ) -> None:
        """join_ack carried share-area convergence data: [entries] is the
        peer's current index, [removed_ids] the file ids removed while this
        member was away."""
        pass

    def group_mesh_group_files(self, group_id: str, entries, removed_ids) -> None:
        """history_reply carried share-area convergence data (mesh path)."""
        pass

    def typing_changed(self, p2p: "P2PManager", sender_id: str, active: bool) -> None:
        """A member's typing indicator changed (host relay path). Called from
        the read-loop thread; [active] False means the member stopped (or sent
        its message). Advisory only — the ViewModel also expires an indicator
        that received no refresh."""
        pass

    def group_info_changed(self, p2p: "P2PManager") -> None:
        """The group owner published a new name/announcement (group_update);
        the ViewModel refreshes its GroupMeta and persists the change."""
        pass

    def kicked_from_group(self, p2p: "P2PManager") -> None:
        """The group owner removed this device (kick_member): every connection
        of the group must be torn down and the group hidden from the list
        (local history is kept)."""
        pass

    # ------------------------------------------- message-experience callbacks

    def message_edited(self, p2p: "P2PManager", message_id: str, new_content: str, sender_id: str, message_sig: Optional[str] = None) -> None:
        """A member edited its own message (edit_message; the relay path has
        already validated the author and the edit signature). [message_sig]
        is the packet's senderSig — the author's signature over the edited
        body's message transcript — for the mesh-copy mirror."""
        pass

    def reaction_changed(
        self, p2p: "P2PManager", message_id: str, emoji: str, sender_id: str, active: bool
    ) -> None:
        """A member toggled an emoji reaction on a message."""
        pass

    def pin_changed(self, p2p: "P2PManager", message_id: str, sender_id: str, active: bool) -> None:
        """A member pinned/unpinned a message in the group."""
        pass

    def group_read_receipt(self, p2p: "P2PManager", reader_id: str, up_to_id: str) -> None:
        """A member reported reading the group up to [up_to_id] (group-scope
        read_receipt); the receiver marks its OWN covered messages."""
        pass


class P2PManager:
    # Peer-presence heartbeat: both sides send a ping every interval; a read
    # loop that sees no traffic for HEARTBEAT_TIMEOUT seconds declares the peer
    # offline (detects half-open TCP connections instead of failing only when a
    # message is sent). Keep HEARTBEAT_TIMEOUT > HEARTBEAT_INTERVAL.
    HEARTBEAT_INTERVAL = 15.0
    HEARTBEAT_TIMEOUT = 45.0

    # Listener hardening constants (same values as HostGroupServer).
    MAX_ACTIVE_CONNECTIONS = 64
    HANDSHAKE_RATE_LIMIT = 60
    HANDSHAKE_RATE_WINDOW = 60.0

    def __init__(
        self,
        listener: P2PListener,
        port: int = TCP_PORT,
        password: str = "",
        host_server: Optional[HostGroupServer] = None,
        device_id: Optional[str] = None,
        hardware_id: Optional[str] = None,
    ):
        self.listener = listener
        self.port = port
        self.group_password = password
        # When set, this host group is served by the program-wide shared
        # listener (one port for the whole program) instead of its own
        # listener socket. The ViewModel always passes it for real hosting;
        # tests may omit it to keep a per-instance server.
        self._host_server = host_server
        # Stable per-device identity so a reconnect looks like the same member
        # to the host (peers lists, message attribution and delete rights all
        # key off the id). The ViewModel passes a persisted id; tests fall
        # back to a fresh random one.
        self.my_id: str = device_id or str(uuid.uuid4())
        self.my_name: str = ""
        self.my_ip_address: str = ""
        self.group_name: str = ""
        self.group_id: str = ""
        # Numeric join id: the identifier members type to join this group
        # (computed by the host from the machine fingerprint + group name).
        self.join_id: str = ""
        self.is_host: bool = False
        # Stable per-device fingerprint (persisted by the ViewModel so the
        # fallback never regenerates), used for the group id and numeric id.
        self.hardware_id: str = hardware_id or get_hardware_id()

        self.peers: Dict[str, Peer] = {}
        self.messages: list = []
        self.connection_lost: bool = False
        self.server_error: Optional[str] = None
        self.is_joining: bool = False
        self.is_querying: bool = False
        self.connection_result: Optional[tuple] = None
        self.queried_group_info: Optional[GroupInfo] = None
        self.query_error: Optional[str] = None
        # The group's real host (creator) address, learned when joining through
        # a member sponsor (the sponsor's join_ack reveals it). The ViewModel
        # persists this as the group's address so a later rejoin connects to
        # the HOST — otherwise the group would be saved with the sponsor's
        # address and show "connection failed" whenever that one member is
        # offline, even though the host is up (Android parity).
        self.connected_host: Optional[Peer] = None

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._server_socket: Optional[socket.socket] = None
        # One heartbeat thread per manager: a rejoin must not stack another
        # loop (reconnect churn would otherwise leak one thread per rejoin,
        # all poking the same wire).
        self._heartbeat_thread: Optional[threading.Thread] = None
        # One connected member on the host side: its socket plus the
        # per-connection encrypted wire (each join negotiated its own key).
        self._connected_clients: Dict[str, dict] = {}
        self._host_socket: Optional[socket.socket] = None
        # Encrypted wire of the client->host relay connection.
        self._host_wire: Optional[Wire] = None
        # Single worker thread serializes UI-originated outbound packets
        # (chat/delete) so their wire order matches submission order and no
        # socket I/O ever blocks the Qt main thread (mirrors the Android
        # single-threaded send scope).
        self._send_queue: "queue.Queue" = queue.Queue()
        self._send_worker: Optional[threading.Thread] = None
        # Outbound file servers keyed by fileId; each offers one file over a
        # separate short-lived listener until its TTL elapses or stop().
        self._file_servers: Dict[str, socket.socket] = {}
        # Optional call-signaling listener: callable(p2p, packet) invoked on
        # the network thread for call_* packets addressed to this node.
        self.call_listener = None
        # Optional group voice conference listener: callable(p2p, packet)
        # invoked on the network thread for group_call_* packets addressed to
        # this node (kept separate so the 1:1 call path is untouched).
        self.group_call_listener = None
        # Tombstone source for join_ack: callable(group_id) -> [msgId, ...]
        # naming messages deleted while members were away (the ViewModel
        # backs it with the deleted_messages table). None omits the field.
        self.deleted_ids_provider = None
        # Group file share area (群文件) sources for the host's join_ack:
        # callable(group_id) -> [GroupFileInfo, ...] (the share index, newest
        # first) and callable(group_id) -> [fileId, ...] (removed-file
        # tombstones). Backed by the ViewModel's store; None omits the field.
        self.group_files_provider = None
        self.removed_file_ids_provider = None
        # Listener hardening (parity with HostGroupServer): concurrent
        # handlers are capped and handshakes are rate-limited per source IP,
        # because every accepted connection runs a PBKDF2 handshake that a
        # hostile host would otherwise use as a CPU DoS. Reachable only via
        # the per-instance listener (tests); production always registers on
        # the shared HostGroupServer.
        self._active_handlers = 0
        self._handshake_attempts: Dict[str, list] = {}
        # The group owner's published announcement (group_update), mirrored
        # by every member and persisted by the ViewModel.
        self.group_announcement: str = ""
        # Callable() -> creator (group owner) device id for this group, set by
        # the ViewModel from its persisted group info. Only the creator may
        # send group_update / kick_member; an unknown creator means every such
        # packet is refused (fail-closed).
        self.creator_id_provider = None
        # Callable(NetworkPacket) that rebroadcasts a relayed admin packet over
        # the group mesh (set by the ViewModel), so members whose host relay is
        # down still receive owner commands forwarded by a connected member.
        self.admin_rebroadcast = None
        # Once the owner kicked this device the kicked flow must run exactly
        # once even when the same packet arrives over both paths.
        self._kicked_from_group = False

    @property
    def current_group_id(self) -> str:
        return self.group_id

    @property
    def current_group_name(self) -> str:
        return self.group_name

    @property
    def numeric_group_id(self) -> str:
        """Stable 8-digit group id derived from the machine fingerprint and the
        group name — the join identifier, separate from the display name.

        When a join id was set explicitly (set_join_id) it wins: the owner may
        RENAME the group (group_update), and the numeric id must stay the same
        or every member's saved join id would stop matching."""
        if self.join_id:
            return self.join_id
        return numeric_group_id_of(self.group_name, self.hardware_id)

    def initialize_as_host(
        self,
        user_name: str,
        group: str,
        password: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> None:
        self.my_name = user_name.strip()
        self.group_name = group.strip()
        # A re-host must keep the ORIGINAL group id even when the display name
        # was renamed by a group_update (the id keys the message history and
        # the group password); fresh hosts derive it from the name (so the
        # same group name on this device still maps to the same group).
        self.group_id = group_id or f"{self.group_name}@{self.hardware_id}"
        self.my_ip_address = get_local_ip_address()
        self.is_host = True  # persist paths read is_host right after init
        if password is not None:
            self.group_password = password

    def initialize_as_client(
        self, user_name: str, group: str, password: Optional[str] = None
    ) -> None:
        self.my_name = user_name.strip()
        self.group_name = group.strip()
        self.my_ip_address = get_local_ip_address()
        if password is not None:
            self.group_password = password

    def set_join_id(self, join_id: str) -> None:
        """The numeric id members type to join this group (host side: shown
        and shared; member side: sent in query/join handshakes)."""
        self.join_id = join_id

    def start_as_host(self) -> None:
        self.is_host = True
        if self._host_server is not None:
            # program-wide single-port server takes over listening
            self._host_server.register(self)
        else:
            self._spawn(self._server_loop)
        self._start_heartbeat()

    def _start_heartbeat(self) -> None:
        with self._lock:
            if (
                self._heartbeat_thread is not None
                and self._heartbeat_thread.is_alive()
            ):
                return  # the existing loop already pings the live wire
            t = threading.Thread(
                target=self._heartbeat_loop, name="LocalChat-heartbeat", daemon=True
            )
            self._heartbeat_thread = t
            t.start()

    def _heartbeat_loop(self) -> None:
        """Send a ping every interval so the peer's read loop keeps receiving
        traffic and can detect a half-open connection within HEARTBEAT_TIMEOUT."""
        while not self._stop_event.wait(self.HEARTBEAT_INTERVAL):
            try:
                if self.is_host:
                    self._broadcast_to_clients(NetworkPacket(type="ping"))
                else:
                    wire = self._host_wire
                    if wire is not None:
                        wire.send_packet(NetworkPacket(type="ping"))
            except Exception:
                pass

    def stop(self) -> None:
        self._stop_event.set()
        if self._host_server is not None:
            self._host_server.unregister(self)
        if self._server_socket is not None:
            try:
                self._server_socket.close()
            except OSError:
                pass
            self._server_socket = None
        with self._lock:
            file_socks = list(self._file_servers.values())
            self._file_servers.clear()
        for fs in file_socks:
            try:
                fs.close()
            except OSError:
                pass
        if self._host_socket is not None:
            self._safe_close(self._host_socket)
            self._host_socket = None
        self._host_wire = None
        self.connected_host = None
        with self._lock:
            conns = list(self._connected_clients.values())
            self._connected_clients.clear()
            self.peers.clear()
        for c in conns:
            self._safe_close(c["sock"])
        self.connection_lost = False
        self.is_joining = False
        self.is_querying = False

    # -------------------------------------------------------------- joining

    def query_group(self, target_ip: str, target_port: int = TCP_PORT) -> None:
        if self.is_querying:
            return
        self.is_querying = True
        self.queried_group_info = None
        self.query_error = None
        self.listener.query_result_changed(self)

        def run() -> None:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.create_connection((target_ip, target_port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(15)
                reader = sock.makefile("r", encoding="utf-8", newline="\n")
                # Secured handshake (password-bound when a password was typed):
                # the group_info response is never plaintext.
                wire = make_wire(sock, reader)
                Handshake.initiate(
                    wire,
                    Protocol.MODE_QUERY,
                    self.join_id or self.group_name,
                    self.group_password,
                )
                wire.send_packet(
                    NetworkPacket(
                        type=Protocol.MODE_QUERY,
                        group_id=self.join_id or self.group_name,
                    )
                )
                try:
                    response = wire.recv_packet()
                except WireException:
                    response = None
                if response is None:
                    self.query_error = "无响应"
                elif response.type == "group_info" and response.group_info is not None:
                    self.queried_group_info = response.group_info
                    # the display name comes from the host; the numeric id is
                    # the join identifier
                    self.group_name = response.group_info.group_name
                elif response.type == "join_rejected":
                    self.query_error = "该设备不存在此群组"
                else:
                    self.query_error = "未知的响应"
            except WireException as e:
                self.query_error = str(e)
            except Exception as e:
                self.query_error = f"查询失败: {e}"
            finally:
                if sock is not None:
                    self._safe_close(sock)
                self.is_querying = False
                self.listener.query_result_changed(self)

        self._spawn(run)

    def clear_query_state(self) -> None:
        self.queried_group_info = None
        self.query_error = None

    def confirm_join(self, target_ip: str, target_port: int = TCP_PORT) -> None:
        """Join by the host's direct (LAN or public) IP:port."""
        if self.is_joining:
            return
        self._begin_join()

        def run() -> None:
            sock: Optional[socket.socket] = None
            consumed = False
            try:
                sock = socket.create_connection((target_ip, target_port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                consumed = self._join_exchange(sock, typed_endpoint=(target_ip, target_port))
            except WireException as e:
                self._set_join_result(False, str(e))
            except Exception as e:
                self._set_join_result(False, f"连接失败: {e}")
            finally:
                if sock is not None and not consumed:
                    self._safe_close(sock)
                self.is_joining = False
                self.listener.join_state_changed(self)

        self._spawn(run)

    def confirm_join_via_server(
        self,
        server_host: str,
        server_port: int,
        punch_timeout: float = PUNCH_TIMEOUT,
        secret: str = "",
    ) -> None:
        """Join through the public signaling server (punch.py): punch a
        direct hole to the host (TCP simultaneous open) or fall back to an
        encrypted server relay. The group is identified by its numeric join
        id — no host address needed. [secret] authenticates this client to
        the server. The host display name is learned with a keep-open query
        on the SAME channel, then the regular secured join handshake runs on
        it."""
        if self.is_joining:
            return
        self._begin_join()

        def run() -> None:
            sock: Optional[socket.socket] = None
            consumed = False
            try:
                sock = punch_connect(
                    server_host,
                    server_port,
                    self.join_id or self.group_name,
                    ROLE_MEMBER,
                    self.my_id,
                    self.my_name,
                    punch_timeout=punch_timeout,
                    secret=secret,
                )
                sock.settimeout(15)
                reader = self._server_path_query(sock)
                consumed = self._join_exchange(sock, reader=reader)
            except PunchError as e:
                self._set_join_result(False, str(e))
            except WireException as e:
                self._set_join_result(False, str(e))
            except Exception as e:
                self._set_join_result(False, f"连接失败: {e}")
            finally:
                if sock is not None and not consumed:
                    self._safe_close(sock)
                self.is_joining = False
                self.listener.join_state_changed(self)

        self._spawn(run)

    def _begin_join(self) -> None:
        self.is_joining = True
        self.connection_lost = False
        self.connection_result = None
        self.listener.join_state_changed(self)

    def _server_path_query(self, sock: socket.socket):
        """Run a secured MODE_QUERY on the punched/relayed socket so the
        group display name (and member count) is known before joining. The
        host keeps the connection open ([_handle allow_query_join]) and the
        returned reader is reused by the follow-up join exchange."""
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        wire = make_wire(sock, reader)
        Handshake.initiate(
            wire,
            Protocol.MODE_QUERY,
            self.join_id or self.group_name,
            self.group_password,
        )
        wire.send_packet(
            NetworkPacket(
                type=Protocol.MODE_QUERY,
                group_id=self.join_id or self.group_name,
            )
        )
        try:
            response = wire.recv_packet()
        except WireException:
            response = None
        if response is None:
            raise WireException("无响应")
        if response.type == "group_info" and response.group_info is not None:
            # display name only — deliberately NOT p2p.queried_group_info:
            # the setup page shows the confirm dialog whenever a queried
            # info is present, and this path has no confirm step
            self.group_name = response.group_info.group_name
        elif response.type == "join_rejected":
            raise WireException("该设备不存在此群组")
        else:
            raise WireException("未知的响应")
        return reader

    def _join_exchange(
        self,
        sock: socket.socket,
        reader=None,
        typed_endpoint: Optional[tuple] = None,
    ) -> bool:
        """Run the secured MODE_JOIN handshake and join_ack handling on an
        already-connected socket. [typed_endpoint] is the address the user
        typed (direct joins only): a join_ack naming a DIFFERENT host means
        the join went through a member sponsor. Returns True when this
        method took ownership of [sock] (the host relay path keeps it);
        False on rejection (result already recorded, caller closes)."""
        if reader is None:
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
        wire = make_wire(sock, reader)
        # password-bound secured handshake: the join (and everything
        # after it) is encrypted and the host is authenticated by its
        # knowledge of the group password
        Handshake.initiate(
            wire,
            Protocol.MODE_JOIN,
            self.join_id or self.group_name,
            self.group_password,
        )
        my_peer = Peer(self.my_id, self.my_name, self.my_ip_address, self.port)
        # no password field: the password-bound handshake already
        # authenticated the joiner — it never appears in a packet
        wire.send_packet(
            NetworkPacket(
                type=Protocol.MODE_JOIN,
                group_id=self.join_id or self.group_name,
                peer=my_peer,
            )
        )
        try:
            response = wire.recv_packet()
        except WireException:
            response = None
        if response is None:
            self._set_join_result(False, "连接被关闭")
            return False
        if response.type == "join_ack" and response.members is not None:
            with self._lock:
                self.group_id = response.group_id or self.group_id
                for peer in response.members:
                    if peer.id != self.my_id:
                        self.peers[peer.id] = peer
            # the owner's current announcement travels with the ack so a
            # newcomer sees it right away (group_update only carries changes)
            if response.announcement is not None:
                self.group_announcement = response.announcement
            self.listener.peers_changed(self)
            if response.deleted_ids:
                # tombstone convergence: drop copies we still hold and record
                # the tombstones — never rebroadcast (Android parity); runs
                # before the join result so the replay after it cannot
                # resurrect the deleted messages. The sanitized list is what
                # reaches the database.
                ids = sanitize_deleted_ids(response.deleted_ids)
                if ids:
                    self.apply_deleted_ids(ids)
                    self.listener.deleted_ids_received(self, ids)
            self._apply_join_group_files(response)
            host = response.host
            if host is not None and (
                typed_endpoint is None
                or host.ip_address != typed_endpoint[0]
                or host.port != typed_endpoint[1]
            ):
                # joined through a member sponsor: the ack reveals the
                # host, so complete the join by connecting to the host
                # for the relay path (best effort — mesh works without
                # it). Record the REAL host address synchronously
                # (before the join result) so the ViewModel can persist
                # the right rejoin address — never the sponsor the
                # user typed (Android parity).
                self.connected_host = host
                self.is_joining = False
                self._set_join_result(True, "")
                # the sponsor socket only served the join ack; it is
                # not the relay path, so close it (a leak otherwise)
                # and let _connect_to_host establish the real link.
                self._safe_close(sock)
                self._connect_to_host(host)
                return True
            self._host_socket = sock
            self._host_wire = wire
            self.is_joining = False
            self._set_join_result(True, "")
            self.listener.peers_changed(self)
            self._start_heartbeat()
            self._read_loop_from_host(sock, wire)
            return True
        if response.type == "join_rejected":
            self._set_join_result(False, "群组不匹配，连接被拒绝")
        elif response.type == "error":
            self._set_join_result(False, response.error_message or "加入被拒绝")
        else:
            self._set_join_result(False, "未知的响应")
        return False

    def clear_join_result(self) -> None:
        self.connection_result = None

    def _connect_to_host(self, host: Peer) -> None:
        """Join the group's HOST after a member-sponsored join revealed its
        address: establishes the standard host relay path. Best effort — when
        the host is unreachable the member stays mesh-only."""

        def run() -> None:
            sock: Optional[socket.socket] = None
            try:
                sock = socket.create_connection((host.ip_address, host.port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(15)
                reader = sock.makefile("r", encoding="utf-8", newline="\n")
                wire = make_wire(sock, reader)
                Handshake.initiate(
                    wire,
                    Protocol.MODE_JOIN,
                    self.join_id or self.group_name,
                    self.group_password,
                )
                my_peer = Peer(self.my_id, self.my_name, self.my_ip_address, self.port)
                wire.send_packet(
                    NetworkPacket(
                        type=Protocol.MODE_JOIN,
                        group_id=self.join_id or self.group_name,
                        peer=my_peer,
                    )
                )
                try:
                    response = wire.recv_packet()
                except WireException:
                    response = None
                if response is None or response.type != "join_ack":
                    raise OSError("host rejected join")
                with self._lock:
                    for peer in (response.members or []):
                        if peer.id != self.my_id:
                            self.peers[peer.id] = peer
                    self._host_socket = sock
                    self._host_wire = wire
                if response.announcement is not None:
                    self.group_announcement = response.announcement
                sock = None
                if response.deleted_ids:
                    # tombstone convergence from the host (no rebroadcast);
                    # only the sanitized/capped list reaches the database
                    ids = sanitize_deleted_ids(response.deleted_ids)
                    if ids:
                        self.apply_deleted_ids(ids)
                        self.listener.deleted_ids_received(self, ids)
                self._apply_join_group_files(response)
                self._start_heartbeat()
                self._read_loop_from_host(self._host_socket, wire)
            except Exception:
                if sock is not None:
                    self._safe_close(sock)
                with self._lock:
                    self._host_socket = None
                    self._host_wire = None

        self._spawn(run)

    # -------------------------------------------------- group file share area

    def _apply_join_group_files(self, response: NetworkPacket) -> None:
        """join_ack share-area convergence (both join paths): the store
        applies removal tombstones first and skips entries they cover, so a
        member that was offline for BOTH a share and its removal converges
        exactly. Never rebroadcast (Android parity)."""
        removed = sanitize_id_list(response.removed_ids, MAX_GROUP_FILE_REMOVED)
        entries = sanitize_group_files(response.group_files)
        if not removed and not entries:
            return
        try:
            self.listener.group_files_received(self, entries, removed)
        except Exception:
            pass

    def send_group_packet(self, packet: NetworkPacket) -> None:
        """Queue one group-scoped control packet (group_file_add/remove) onto
        the serialized send worker: the host broadcasts it, a member sends it
        over the host relay. The mesh mirror is the ViewModel's job (same
        split as a chat send)."""
        self._enqueue_send(packet)

    @staticmethod
    def _group_file_entry_from_packet(packet: NetworkPacket) -> Optional[GroupFileInfo]:
        """Build a sanitized share-index entry from a group_file_add packet.
        Returns None when required fields are missing — the packet is then
        ignored entirely (all fields are optional on the wire so old peers
        decode and ignore it; a packet without usable content is just
        noise)."""
        if not packet.file_id or not packet.name:
            return None
        if packet.size is None or packet.size < 0:
            return None
        if not packet.sender_id:
            return None
        file_info = packet.file_info
        return GroupFileInfo(
            file_id=packet.file_id[:MAX_FILE_ID_LEN],
            name=sanitize_file_name(packet.name),
            size=packet.size,
            sender_id=packet.sender_id[:MAX_DELETED_ID_LEN],
            sender_name=(packet.sender_name or "")[:64],
            ts=packet.ts or 0,
            download_host=file_info.download_host if file_info else "",
            download_port=file_info.download_port if file_info else 0,
            file_key=file_info.file_key if file_info else "",
        )

    def _handle_group_file_add_from_client(self, packet: NetworkPacket, sender_id: str) -> None:
        """A member shared a file into the share area: bind the claimed
        senderId to the authenticated connection, apply locally (persist via
        the listener) and relay to the other members. Sharing is open to any
        group member."""
        if packet.sender_id != sender_id or packet.group_id != self.group_id:
            logger.warning(
                "reject group_file_add from %s: packet senderId=%r groupId=%r",
                sender_id, packet.sender_id, packet.group_id,
            )
            return
        entry = self._group_file_entry_from_packet(packet)
        if entry is None or entry.sender_id != sender_id:
            logger.warning("reject group_file_add from %s: unusable entry", sender_id)
            return
        self.listener.group_file_add_received(self, entry)
        self._broadcast_to_clients(packet, exclude=sender_id)

    def _handle_group_file_remove_from_client(self, packet: NetworkPacket, sender_id: str) -> None:
        """A member removed a shared file. Authorization (uploader or group
        owner) lives in the listener implementation and gates BOTH the
        persistence and the rebroadcast: a rejected remove must neither
        propagate nor mint a tombstone (LESSONS 2026-09-19 #3)."""
        if (
            not packet.file_id
            or packet.sender_id != sender_id
            or packet.group_id != self.group_id
        ):
            logger.warning(
                "reject group_file_remove from %s: packet senderId=%r groupId=%r",
                sender_id, packet.sender_id, packet.group_id,
            )
            return
        applied = bool(
            self.listener.group_file_remove_received(self, packet.file_id, sender_id)
        )
        if not applied:
            logger.warning(
                "group_file_remove %s from %s not authorized; not relayed",
                packet.file_id, sender_id,
            )
            return
        self._broadcast_to_clients(packet, exclude=sender_id)

    # -------------------------------------------------------- host handlers

    def _matches_group(self, id_or_name: str) -> bool:
        """True when [id_or_name] identifies this group: the numeric join id
        (primary) or the legacy group name."""
        return id_or_name == self.numeric_group_id or id_or_name == self.group_name

    def _handle_query_group(
        self,
        sock: socket.socket,
        wire: Wire,
        packet: NetworkPacket,
        keep_open: bool = False,
    ) -> bool:
        """Answer a group query. Returns True only when the answer was sent
        AND [keep_open] is set (signaling-server path): the socket stays
        open for the follow-up join handshake on the same connection.
        Otherwise the socket is closed and False returned."""
        matched = bool(packet.group_id) and self._matches_group(packet.group_id)
        answered = False
        try:
            if not matched:
                wire.send_packet(NetworkPacket(type="join_rejected"))
                return False
            with self._lock:
                count = len(self.peers) + 1
            info = GroupInfo(self.group_name, self.my_name, self.my_id, count)
            wire.send_packet(NetworkPacket(type="group_info", group_info=info))
            answered = True
            return bool(keep_open)
        except Exception:
            return False
        finally:
            if not answered:
                self._safe_close(sock)

    def _handle_join(self, sock: socket.socket, wire: Wire, packet: NetworkPacket) -> None:
        # The handshake already verified the group password (password-bound
        # ECDH); only the packet shape is validated here.
        peer = packet.peer
        if (
            not packet.group_id
            or not self._matches_group(packet.group_id)
            or peer is None
            # reject unusable identities: an empty id cannot be addressed or
            # cleaned up, and a join claiming the HOST's own id would let
            # the sender impersonate the host towards every member
            or not peer.id
            or peer.id == self.my_id
        ):
            try:
                wire.send_packet(NetworkPacket(type="join_rejected"))
            except Exception:
                pass
            self._safe_close(sock)
            return
        with self._lock:
            self.peers[peer.id] = peer
            # A rejoin with the same stable peer id replaces the old
            # connection: the stale connection would otherwise keep a live
            # input channel for that identity (duplicate messages, forged
            # packets, or its read-loop finally broadcasting peer_left for a
            # member that just rejoined).
            conn = {"sock": sock, "wire": wire, "alive": True}
            previous = self._connected_clients.get(peer.id)
            self._connected_clients[peer.id] = conn
            members = [Peer(self.my_id, self.my_name, self.my_ip_address, self.port)] + [
                p for pid, p in self.peers.items() if pid != self.my_id and pid != peer.id
            ]
        if previous is not None and previous is not conn:
            self._safe_close(previous["sock"])
        try:
            ack = NetworkPacket(
                type="join_ack",
                group_id=self.group_id,
                members=members,
                # the owner's current announcement so a newcomer sees it at
                # once (omitted when empty, matching kotlinx.serialization)
                announcement=self.group_announcement or None,
            )
            deleted_ids = None
            if self.deleted_ids_provider is not None:
                try:
                    deleted_ids = self.deleted_ids_provider(self.group_id) or None
                except Exception:
                    deleted_ids = None
            if deleted_ids:
                # tombstone convergence: ids deleted while this member was
                # away, so it drops/resists them instead of resurrecting
                ack.deleted_ids = deleted_ids
            group_files = None
            if self.group_files_provider is not None:
                try:
                    group_files = sanitize_group_files(
                        self.group_files_provider(self.group_id)
                    )
                except Exception:
                    group_files = []
            if group_files:
                # share-area convergence: the join_ack is a single packet, so
                # only the newest slice rides it — the rest converges over the
                # mesh history push (both sides push on link establishment)
                ack.group_files = group_files[:JOIN_ACK_GROUP_FILES_CAP]
            removed_file_ids = None
            if self.removed_file_ids_provider is not None:
                try:
                    removed_file_ids = sanitize_id_list(
                        self.removed_file_ids_provider(self.group_id),
                        MAX_GROUP_FILE_REMOVED,
                    )
                except Exception:
                    removed_file_ids = []
            if removed_file_ids:
                ack.removed_ids = removed_file_ids
            wire.send_packet(ack)
        except Exception:
            # the member is gone before the ack: undo the registration above,
            # otherwise a dead socket lingers as a ghost member (its read loop
            # never started, so nothing would ever clean it up)
            with self._lock:
                if self._connected_clients.get(peer.id) is conn:
                    self._connected_clients.pop(peer.id, None)
                    self.peers.pop(peer.id, None)
            self._safe_close(sock)
            return
        self._broadcast_to_clients(NetworkPacket(type="announce", peer=peer), exclude=peer.id)
        self.listener.peers_changed(self)
        self._read_loop_from_client(sock, wire, conn, peer.id)

    # ---------------------------------------------------------- read loops

    def _read_loop_from_host(self, sock: socket.socket, wire: Wire) -> None:
        try:
            # no traffic for HEARTBEAT_TIMEOUT means the host is gone
            # (half-open connection); the host's pings keep this from firing
            sock.settimeout(self.HEARTBEAT_TIMEOUT)
            while not self._stop_event.is_set():
                try:
                    packet = wire.recv_packet()
                except WireException:
                    break
                if packet is None:
                    break
                try:
                    self._process_packet_as_client(packet)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._safe_close(sock)
            # Order matters: the ViewModel's peers_changed keys its "keep
            # last-known members" guard off connection_lost, so the flag must
            # be set BEFORE the peer map is cleared — otherwise a listener that
            # runs between the two updates would see an empty map with
            # lost=false and tear down the mesh + persisted peers (Android
            # parity).
            self.connection_lost = True
            with self._lock:
                self._host_socket = None
                self._host_wire = None
                self.peers.clear()
            self.listener.peers_changed(self)
            self.listener.connection_lost(self)

    def _read_loop_from_client(
        self, sock: socket.socket, wire: Wire, conn: dict, peer_id: str
    ) -> None:
        try:
            # no traffic for HEARTBEAT_TIMEOUT means this member is gone
            # (half-open connection); the member's pings keep this from firing
            sock.settimeout(self.HEARTBEAT_TIMEOUT)
            while not self._stop_event.is_set():
                try:
                    packet = wire.recv_packet()
                except WireException:
                    break
                if packet is None:
                    break
                try:
                    self._process_packet_from_client(packet, peer_id)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._safe_close(sock)
            replaced = False
            with self._lock:
                # Only clean up when this connection is still the registered
                # one: a rejoin with the same stable peer id may have replaced
                # it with a fresh socket, and this old loop's cleanup must not
                # remove the just-rejoined member.
                current = self._connected_clients.get(peer_id)
                if current is conn:
                    self._connected_clients.pop(peer_id, None)
                    self.peers.pop(peer_id, None)
                else:
                    replaced = True
            if not replaced:
                self._broadcast_to_clients(
                    NetworkPacket(type="peer_left", peer=Peer(peer_id, "", "", 0)),
                    exclude=peer_id,
                )
            self.listener.peers_changed(self)

    def _process_packet_as_client(self, packet: NetworkPacket) -> None:
        if packet.type in ("chat", "file_message") and packet.message is not None:
            msg = packet.message
            # author identity gate (TOFU): a bound sender must sign, a valid
            # signature must match the remembered key — BEFORE the message
            # reaches the list / the listener (LESSONS 2026-09-19 #3)
            if not groupauth.verify_message(self.current_group_id, msg):
                return
            with self._lock:
                # idempotent insert: a member's message reaches us over the
                # host relay AND over the mesh (whoever arrives first wins),
                # so a plain append would show duplicate bubbles
                if not any(m.id == packet.message.id for m in self.messages):
                    self.messages.append(packet.message.marked_from_me(self.my_id))
            self.listener.messages_changed(self)
        elif packet.type == "announce" and packet.peer is not None:
            if packet.peer.id != self.my_id:
                with self._lock:
                    self.peers[packet.peer.id] = packet.peer
                self.listener.peers_changed(self)
        elif packet.type == "peer_left" and packet.peer is not None:
            with self._lock:
                self.peers.pop(packet.peer.id, None)
            self.listener.peers_changed(self)
        elif packet.type == "group_update":
            self._handle_group_update_as_client(packet)
        elif packet.type == "kick_member":
            self._handle_kick_as_client(packet)
        elif packet.type == "delete_message" and packet.message_id is not None:
            target = None
            with self._lock:
                target = next(
                    (m for m in self.messages if m.id == packet.message_id), None
                )
            if (
                target is not None
                and (
                    packet.sender_id is None
                    or packet.sender_id != target.sender_id
                )
            ):
                # the relayed delete must come from the message's original
                # author (same authorization as the host applies before
                # forwarding); a missing or mismatched senderId is a forged
                # request
                logger.warning(
                    "reject relay delete_message %s: packet senderId=%r, message senderId=%r",
                    packet.message_id,
                    packet.sender_id,
                    target.sender_id,
                )
                return
            if not groupauth.verify_delete(
                self.current_group_id,
                packet.sender_id or "",
                packet.message_id,
                packet.sender_pub_id,
                packet.sender_sig,
            ):
                return
            with self._lock:
                self.messages = [m for m in self.messages if m.id != packet.message_id]
            self.listener.messages_changed(self)
        elif packet.type == "typing" and packet.sender_id and packet.active is not None:
            # advisory typing indicator for a member, attributed by the host
            # relay (a client's own senderId was validated by the host before
            # forwarding, so it cannot be forged here)
            self.listener.typing_changed(self, packet.sender_id, bool(packet.active))
        elif packet.type == "edit_message" and packet.message_id and packet.new_content is not None:
            self._apply_relayed_edit(packet)
        elif packet.type == "reaction" and packet.message_id and packet.emoji:
            self._apply_relayed_reaction(packet)
        elif packet.type == "pin_message" and packet.message_id:
            self._apply_relayed_pin(packet)
        elif packet.type == "group_file_add":
            self._apply_relayed_group_file_add(packet)
        elif packet.type == "group_file_remove":
            self._apply_relayed_group_file_remove(packet)
        elif (
            packet.type == "read_receipt"
            and packet.up_to_id
            and packet.reader_id
            and packet.group_id == self.current_group_id
        ):
            # group-scope read receipt relayed by the host; the reader id was
            # validated by the host against the member's connection
            self.listener.group_read_receipt(self, packet.reader_id, packet.up_to_id)
        elif packet.type == "ping":
            wire = self._host_wire
            if wire is not None:
                try:
                    wire.send_packet(NetworkPacket(type="pong"))
                except Exception:
                    pass
        elif packet.type in CALL_PACKET_TYPES:
            # Call packets are never broadcast on the relay path: a client only
            # accepts packets explicitly addressed to this node and whose
            # callId/participant fields are consistent with its role.
            call = packet.call
            if call is None:
                return
            if packet.target_id != self.my_id:
                return
            if packet.type == "call_offer":
                if call.callee_id != self.my_id:
                    return
            elif packet.type in ("call_answer", "call_reject", "call_failed"):
                if call.caller_id != self.my_id:
                    return
            elif packet.type == "call_hangup":
                if call.caller_id != self.my_id and call.callee_id != self.my_id:
                    return
            self._dispatch_call(packet)
        elif packet.type in GROUP_CALL_PACKET_TYPES:
            # Group voice conference signaling relayed by the host: only
            # packets explicitly addressed to this node whose declared
            # parties include this node reach the listener (semantic
            # checks — group id / meeting id / host identity — belong to
            # the GroupCallManager).
            call = packet.call
            if call is None:
                return
            if packet.target_id != self.my_id:
                return
            if call.callee_id != self.my_id and call.caller_id != self.my_id:
                return
            self._dispatch_group_call(packet)
        # "pong": traffic only; keeps the read loop alive

    # ------------------------------------------------- message-experience apply

    def _apply_relayed_edit(self, packet: NetworkPacket) -> None:
        """A relayed edit_message: the host already checked the sender's
        identity; only the message's original author may change its content,
        and the author's device signature must verify (TOFU)."""
        target = None
        with self._lock:
            target = next(
                (m for m in self.messages if m.id == packet.message_id), None
            )
        if target is None or packet.sender_id != target.sender_id:
            logger.warning(
                "reject edit_message %s: packet senderId=%r, message senderId=%r",
                packet.message_id,
                packet.sender_id,
                target.sender_id if target is not None else None,
            )
            return
        if not groupauth.verify_message_fields(
            self.current_group_id,
            packet.sender_id,
            packet.message_id,
            target.timestamp,
            packet.new_content,
            packet.sender_pub_id,
            packet.sender_sig,
        ):
            logger.warning(
                "reject edit_message %s: unsigned or invalid edit signature",
                packet.message_id,
            )
            return
        with self._lock:
            target.content = packet.new_content
            target.edited = True
        self.listener.message_edited(
            self, packet.message_id, packet.new_content, packet.sender_id,
            message_sig=packet.sender_sig,
        )

    def _apply_relayed_reaction(self, packet: NetworkPacket) -> None:
        target = None
        with self._lock:
            target = next(
                (m for m in self.messages if m.id == packet.message_id), None
            )
        if target is None or packet.sender_id is None:
            return
        self.listener.reaction_changed(
            self,
            packet.message_id,
            packet.emoji,
            packet.sender_id,
            bool(packet.active),
        )

    def _apply_relayed_pin(self, packet: NetworkPacket) -> None:
        target = None
        with self._lock:
            target = next(
                (m for m in self.messages if m.id == packet.message_id), None
            )
        if target is None or packet.sender_id is None:
            return
        self.listener.pin_changed(
            self, packet.message_id, packet.sender_id, bool(packet.active)
        )

    # --------------------------------------------- relayed share-area packets

    def _apply_relayed_group_file_add(self, packet: NetworkPacket) -> None:
        """A relayed group_file_add: the host bound the sender's identity
        before forwarding; the entry must still name that sender and carry
        usable fields before it reaches the index."""
        if packet.group_id != self.group_id or not packet.sender_id:
            return
        entry = self._group_file_entry_from_packet(packet)
        if entry is None or entry.sender_id != packet.sender_id:
            logger.warning(
                "reject relayed group_file_add: senderId=%r unusable entry",
                packet.sender_id,
            )
            return
        self.listener.group_file_add_received(self, entry)

    def _apply_relayed_group_file_remove(self, packet: NetworkPacket) -> None:
        """A relayed group_file_remove: authorization (uploader or group
        owner) is re-checked by the listener implementation before anything
        is applied — the host's check never lifts the receiver's own."""
        if (
            packet.group_id != self.group_id
            or not packet.file_id
            or not packet.sender_id
        ):
            return
        self.listener.group_file_remove_received(
            self, packet.file_id, packet.sender_id
        )

    def _process_packet_from_client(self, packet: NetworkPacket, sender_id: str) -> None:
        if packet.type in ("group_update", "kick_member"):
            # only the group owner (creator) may send management packets; the
            # owner is this host, so a member sending one is a protocol
            # violation: drop it and detach that member
            logger.warning(
                "reject %s from member %s: only the group owner may send it",
                packet.type,
                sender_id,
            )
            self._drop_client(sender_id)
            return
        if packet.type == "group_file_add":
            # share area: any member may share (sender identity is the
            # authenticated connection; removal is the authorized one)
            self._handle_group_file_add_from_client(packet, sender_id)
            return
        if packet.type == "group_file_remove":
            self._handle_group_file_remove_from_client(packet, sender_id)
            return
        if packet.type in ("chat", "file_message") and packet.message is not None:
            msg = packet.message
            if msg.sender_id != sender_id or not is_valid_content(msg.content):
                logger.warning(
                    "drop invalid %s from %s: senderId=%r content_len=%d",
                    packet.type,
                    sender_id,
                    msg.sender_id,
                    len(msg.content),
                )
                return
            # author identity gate before the message is stored or relayed
            if not groupauth.verify_message(self.current_group_id, msg):
                return
            with self._lock:
                # idempotent insert (same message id can never arrive twice on
                # the relay path, but being defensive costs nothing)
                if not any(m.id == msg.id for m in self.messages):
                    self.messages.append(msg.marked_from_me(self.my_id))
            self.listener.messages_changed(self)
            self._broadcast_to_clients(packet, exclude=sender_id)
        elif packet.type == "delete_message" and packet.message_id is not None:
            target = None
            with self._lock:
                for m in self.messages:
                    if m.id == packet.message_id:
                        target = m
                        break
            if (
                target is not None
                and packet.sender_id == sender_id
                and target.sender_id == sender_id
            ):
                if not groupauth.verify_delete(
                    self.current_group_id,
                    sender_id,
                    packet.message_id,
                    packet.sender_pub_id,
                    packet.sender_sig,
                ):
                    return
                with self._lock:
                    self.messages = [m for m in self.messages if m.id != packet.message_id]
                self.listener.messages_changed(self)
                # Exclude the sender, matching the Android host: the deleting
                # member already removed the message locally, so echoing the
                # delete back to it would only add redundant traffic.
                self._broadcast_to_clients(packet, exclude=sender_id)
            else:
                logger.warning(
                    "reject delete_message %s from %s: packet senderId=%r, message senderId=%r",
                    packet.message_id,
                    sender_id,
                    packet.sender_id,
                    target.sender_id if target is not None else None,
                )
        elif packet.type == "typing" and packet.active is not None:
            # a member is composing: attribute the indicator to the
            # authenticated connection (never to a packet-carried id we did
            # not verify), show it to the host itself, and relay it to the
            # other members (exclude the sender, like chat/delete)
            if packet.sender_id != sender_id:
                logger.warning(
                    "drop typing from %s: packet senderId=%r",
                    sender_id, packet.sender_id,
                )
            else:
                self.listener.typing_changed(self, sender_id, bool(packet.active))
                self._broadcast_to_clients(packet, exclude=sender_id)
        elif packet.type == "edit_message" and packet.message_id and packet.new_content is not None:
            # only the message's original author may edit, and the claimed
            # sender must be the authenticated connection (host relay rule);
            # the packet signature must verify as the edited body's message
            # transcript against the host's copy (strict — no unsigned edit)
            target = None
            with self._lock:
                target = next(
                    (m for m in self.messages if m.id == packet.message_id), None
                )
            if (
                target is not None
                and packet.sender_id == sender_id
                and target.sender_id == sender_id
                and groupauth.verify_message_fields(
                    self.current_group_id,
                    sender_id,
                    packet.message_id,
                    target.timestamp,
                    packet.new_content,
                    packet.sender_pub_id,
                    packet.sender_sig,
                )
            ):
                with self._lock:
                    target.content = packet.new_content
                    target.edited = True
                self.listener.message_edited(
                    self, packet.message_id, packet.new_content, sender_id,
                    message_sig=packet.sender_sig,
                )
                self._broadcast_to_clients(packet, exclude=sender_id)
            else:
                logger.warning(
                    "reject edit_message %s from %s: packet senderId=%r, message senderId=%r",
                    packet.message_id,
                    sender_id,
                    packet.sender_id,
                    target.sender_id if target is not None else None,
                )
        elif packet.type == "reaction" and packet.message_id and packet.emoji:
            if packet.sender_id == sender_id:
                with self._lock:
                    known = any(m.id == packet.message_id for m in self.messages)
                if known:
                    self.listener.reaction_changed(
                        self,
                        packet.message_id,
                        packet.emoji,
                        sender_id,
                        bool(packet.active),
                    )
                    self._broadcast_to_clients(packet, exclude=sender_id)
        elif packet.type == "pin_message" and packet.message_id:
            if packet.sender_id == sender_id:
                with self._lock:
                    known = any(m.id == packet.message_id for m in self.messages)
                if known:
                    self.listener.pin_changed(
                        self, packet.message_id, sender_id, bool(packet.active)
                    )
                    self._broadcast_to_clients(packet, exclude=sender_id)
        elif (
            packet.type == "read_receipt"
            and packet.up_to_id
            and packet.reader_id == sender_id
            and packet.group_id == self.current_group_id
        ):
            # a member read the group up to up_to_id: mark the HOST's own
            # covered messages, then relay so the other members do the same
            self.listener.group_read_receipt(self, sender_id, packet.up_to_id)
            self._broadcast_to_clients(packet, exclude=sender_id)
        elif packet.type == "ping":
            with self._lock:
                conn = self._connected_clients.get(sender_id)
            if conn is not None:
                try:
                    conn["wire"].send_packet(NetworkPacket(type="pong"))
                except Exception:
                    pass
        elif packet.type in CALL_PACKET_TYPES:
            self._route_call_packet(packet, sender_id)
        elif packet.type in GROUP_CALL_PACKET_TYPES:
            self._route_group_call_packet(packet, sender_id)
        # "pong": traffic only; keeps the read loop alive

    def _route_call_packet(self, packet: NetworkPacket, sender_id: str) -> None:
        """Host-side routing for call signaling: validate the sender identity
        and deliver the packet either locally (the host is the peer) or to the
        addressed member's socket (never broadcast)."""
        call = packet.call
        if call is None:
            return
        if packet.type == "call_offer":
            if call.caller_id != sender_id:
                logger.warning("drop call_offer from %s: callerId mismatch", sender_id)
                return
            expected_target = call.callee_id
        elif packet.type in ("call_answer", "call_reject", "call_failed"):
            if call.callee_id != sender_id:
                logger.warning("drop call %s from %s: calleeId mismatch", packet.type, sender_id)
                return
            expected_target = call.caller_id
        elif packet.type == "call_hangup":
            if call.caller_id != sender_id and call.callee_id != sender_id:
                logger.warning("drop call_hangup from %s: not a call participant", sender_id)
                return
            expected_target = (
                call.callee_id if sender_id == call.caller_id else call.caller_id
            )
        else:
            return
        target_id = packet.target_id
        if target_id is not None and target_id != expected_target:
            logger.warning(
                "drop call %s: targetId %r does not match expected %r",
                packet.type, target_id, expected_target,
            )
            return
        if target_id is None:
            if expected_target != self.my_id:
                logger.warning(
                    "drop call %s: missing targetId but expected %r is not this host",
                    packet.type, expected_target,
                )
                return
            self._dispatch_call(packet)
            return
        if target_id == self.my_id:
            self._dispatch_call(packet)
            return
        with self._lock:
            conn = self._connected_clients.get(target_id)
        if conn is not None:
            try:
                conn["wire"].send_packet(packet)
            except Exception:
                pass

    def _dispatch_call(self, packet: NetworkPacket) -> None:
        listener = self.call_listener
        if listener is None:
            return
        try:
            listener(self, packet)
        except Exception:
            logger.exception("call listener failed")

    def _route_group_call_packet(self, packet: NetworkPacket, sender_id: str) -> None:
        """Host-side routing for group voice conference signaling: the packet
        actor (call.callerId — invite sender / joiner / leaver / meeting
        host) must be the authenticated sender connection; deliver locally
        when this host is the addressee, otherwise forward to the targeted
        member's socket (never broadcast)."""
        call = packet.call
        if call is None:
            return
        if call.caller_id != sender_id:
            logger.warning(
                "drop group call %s from %s: callerId mismatch",
                packet.type, sender_id,
            )
            return
        target_id = packet.target_id
        if target_id is None:
            # invite/sync address the invited member, join/leave address the
            # meeting host — both are call.calleeId
            target_id = call.callee_id
        if target_id == self.my_id:
            self._dispatch_group_call(packet)
            return
        with self._lock:
            conn = self._connected_clients.get(target_id)
        if conn is not None:
            try:
                conn["wire"].send_packet(packet)
            except Exception:
                pass

    def _dispatch_group_call(self, packet: NetworkPacket) -> None:
        listener = self.group_call_listener
        if listener is None:
            return
        try:
            listener(self, packet)
        except Exception:
            logger.exception("group call listener failed")

    def _set_join_result(self, success: bool, message: str) -> None:
        self.connection_result = (success, message)
        self.listener.join_state_changed(self)

    # -------------------------------------------------------------- sending

    def send_message(
        self,
        content: str,
        reply_to: Optional[str] = None,
        reply_preview: Optional[str] = None,
        reply_sender: Optional[str] = None,
        mentions: Optional[List[str]] = None,
        quote_sender: Optional[str] = None,
        quote_kind: Optional[str] = None,
        forwarded: Optional[ForwardedInfo] = None,
    ) -> Optional[ChatMessage]:
        """Send a chat message through the host relay; returns the created
        message (or None for invalid content) so the caller can also broadcast
        it over the group mesh. The optional reply_* triple attaches a quoted
        header (see ChatMessage); quote_sender/quote_kind fill the nested
        quote object's original-sender id and content kind. A forward passes
        [forwarded] (display-only provenance) and no reply fields. [mentions]
        carries the @-mentioned peer ids ("all" = everyone)."""
        if not is_valid_content(content):
            return None
        message = ChatMessage(
            id=str(uuid.uuid4()),
            content=content,
            timestamp=int(time.time() * 1000),
            sender_id=self.my_id,
            sender_name=self.my_name,
            is_from_me=True,
            reply_to=reply_to,
            reply_preview=reply_preview,
            reply_sender=reply_sender,
            quote_sender=quote_sender,
            quote_kind=quote_kind,
            forwarded=forwarded,
            mentions=mentions or None,
        )
        # author identity signature (TOFU binding, groupauth.py): no-op when
        # this device has no identity key yet (legacy behavior)
        groupauth.sign_message(self.current_group_id, message)
        with self._lock:
            self.messages.append(message)
        self.listener.messages_changed(self)
        packet = NetworkPacket(type="chat", message=message)
        self._enqueue_send(packet)
        return message

    def send_typing(self, active: bool) -> None:
        """Broadcast a typing indicator to the group over the host relay (the
        ViewModel mirrors it over the mesh). Advisory and never queued
        offline: without a live connection there is nobody to show it to."""
        packet = NetworkPacket(
            type="typing",
            group_id=self.current_group_id,
            sender_id=self.my_id,
            active=bool(active),
        )
        self._enqueue_send(packet)

    def merge_incoming(self, msgs) -> None:
        """Merge messages that arrived over the group mesh (or history sync)
        into the group's list in ONE update, deduplicating against
        host-relayed copies."""
        if not msgs:
            return
        for m in msgs:
            m.marked_from_me(self.my_id)
        with self._lock:
            ids = {m.id for m in self.messages}
            fresh = [m for m in msgs if m.id not in ids]
            if not fresh:
                return
            self.messages.extend(fresh)
            self.messages.sort(key=lambda m: m.timestamp)
        self.listener.messages_changed(self)

    def remove_message(self, message_id: str) -> bool:
        with self._lock:
            target = next((m for m in self.messages if m.id == message_id), None)
            if target is None or target.sender_id != self.my_id:
                return False
            self.messages = [m for m in self.messages if m.id != message_id]
        self.listener.messages_changed(self)
        packet = NetworkPacket(
            type="delete_message", message_id=message_id, sender_id=target.sender_id
        )
        groupauth.sign_packet(
            packet,
            groupauth.delete_parts(self.current_group_id, target.sender_id, message_id),
        )
        self._enqueue_send(packet)
        return True

    def edit_message(self, message_id: str, new_content: str) -> bool:
        """Author-only text edit: applies locally (content + edited flag) and
        broadcasts edit_message so every member's copy follows. The group mesh
        is mirrored by the ViewModel (like a chat send). The packet signature
        covers the EDITED body's message transcript (groupauth.
        message_fields_parts): receivers verify it against their local copy
        and store it, so later mesh history pushes pass verify_message and
        the edit converges to members that were offline. Unsigned (identity
        not initialized) edits are refused — an edit rewrites stored history
        and is never sent without a signature."""
        if not is_valid_content(new_content):
            return False
        with self._lock:
            target = next((m for m in self.messages if m.id == message_id), None)
            if target is None or target.sender_id != self.my_id:
                return False
            pub, sig = groupauth.sign_parts(
                groupauth.message_fields_parts(
                    self.current_group_id, self.my_id,
                    message_id, target.timestamp, new_content,
                )
            )
            if not pub:
                return False
            target.content = new_content
            target.edited = True
            # the packet signature verifies as the new content's message
            # signature: store it on the local copy so mesh history pushes
            # (which serialize this object) stay verify_message-valid
            target.sender_pub_id = pub
            target.sender_sig = sig
        self.listener.messages_changed(self)
        packet = NetworkPacket(
            type="edit_message",
            group_id=self.current_group_id,
            message_id=message_id,
            sender_id=self.my_id,
            new_content=new_content,
            sender_pub_id=pub,
            sender_sig=sig,
        )
        self._enqueue_send(packet)
        return True

    def send_reaction(
        self, message_id: str, emoji: str, active: bool, on_failed=None
    ) -> bool:
        """Toggle an emoji reaction of ours on [message_id]. Advisory: never
        queued offline by the relay itself (a member with no live path simply
        misses it) — the ViewModel stages the op and replays it when the group
        becomes reachable again. [on_failed] fires on the sender worker when
        the packet could not be handed to the host relay."""
        emoji = sanitize_emoji(emoji)
        if not emoji:
            return False
        with self._lock:
            known = any(m.id == message_id for m in self.messages)
        if not known:
            return False
        self._enqueue_send(
            NetworkPacket(
                type="reaction",
                group_id=self.current_group_id,
                message_id=message_id,
                sender_id=self.my_id,
                emoji=emoji,
                active=bool(active),
            ),
            on_failed=on_failed,
        )
        return True

    def send_pin(self, message_id: str, active: bool, on_failed=None) -> bool:
        """Pin/unpin a message in the group. Any member may pin (the group's
        trust model is its password), the claimed sender is validated by every
        receiver against its authenticated identity. Same staging contract as
        send_reaction: [on_failed] lets the ViewModel park the op when the
        relay is unreachable."""
        with self._lock:
            known = any(m.id == message_id for m in self.messages)
        if not known:
            return False
        self._enqueue_send(
            NetworkPacket(
                type="pin_message",
                group_id=self.current_group_id,
                message_id=message_id,
                sender_id=self.my_id,
                active=bool(active),
            ),
            on_failed=on_failed,
        )
        return True

    def send_group_read_receipt(self, up_to_id: str) -> None:
        """Tell the group we have read up to [up_to_id] (host relay path; the
        ViewModel mirrors it over the mesh). Receivers mark only their OWN
        covered messages."""
        if not up_to_id:
            return
        self._enqueue_send(
            NetworkPacket(
                type="read_receipt",
                group_id=self.current_group_id,
                up_to_id=up_to_id,
                reader_id=self.my_id,
            )
        )

    def remove_local_message(self, message_id: str, sender_id: str) -> bool:
        """Remove a message locally because a delete arrived over the group
        mesh (the mesh path validated the sender). Only the original sender
        may delete — mirrors the host relay's authorization. Does NOT
        rebroadcast: the mesh path forwards the delete to every other link
        itself."""
        with self._lock:
            target = next((m for m in self.messages if m.id == message_id), None)
            if target is None:
                return False
            if target.sender_id != sender_id:
                logger.warning(
                    "reject mesh delete %s: message senderId=%r != claimed %r",
                    message_id, target.sender_id, sender_id,
                )
                return False
            self.messages = [m for m in self.messages if m.id != message_id]
        self.listener.messages_changed(self)
        return True

    def apply_edit_local(self, message_id: str, new_content: str, sender_id: str) -> bool:
        """Apply an author edit to the local list because an edit_message
        arrived (relay or mesh path; both validated the author). Idempotent:
        applying the same content twice just re-notifies."""
        changed = False
        with self._lock:
            target = next((m for m in self.messages if m.id == message_id), None)
            if target is None or target.sender_id != sender_id:
                return False
            if target.content != new_content or not target.edited:
                target.content = new_content
                target.edited = True
                changed = True
        if changed:
            self.listener.messages_changed(self)
        return changed

    def apply_deleted_ids(self, deleted_ids) -> list:
        """Locally drop messages that a peer's tombstone data (join_ack /
        history_reply deletedIds) reports as deleted; returns the ids that
        existed here. Never rebroadcasts: convergence is not a new delete
        event (Android parity). The list is sanitized/capped first so one
        packet cannot force unbounded work."""
        wanted = sanitize_deleted_ids(deleted_ids)
        if not wanted:
            return []
        id_set = set(wanted)
        with self._lock:
            gone = [m.id for m in self.messages if m.id in id_set]
            if gone:
                self.messages = [m for m in self.messages if m.id not in id_set]
        if gone:
            self.listener.messages_changed(self)
        return gone

    def _enqueue_send(self, packet: NetworkPacket, on_failed=None) -> None:
        """Queue an outbound packet for the single sender worker thread.
        [on_failed] fires on the worker thread when the packet could not be
        handed to the wire at all (e.g. no host connection)."""
        with self._lock:
            if self._send_worker is None:
                self._send_worker = threading.Thread(
                    target=self._send_worker_loop, name="LocalChat-sender", daemon=True
                )
                self._send_worker.start()
            self._send_queue.put((packet, on_failed))

    def enqueue_packet(self, packet: NetworkPacket, on_failed=None) -> None:
        """Public enqueue for packets the ViewModel builds itself (the staged
        offline op replay). Same delivery semantics as _enqueue_send."""
        self._enqueue_send(packet, on_failed=on_failed)

    def send_targeted(self, peer_id: str, packet: NetworkPacket) -> None:
        """Send a packet addressed to a specific member (call signaling).

        As the host the packet goes straight to that member's wire; as a
        client it is relayed through the host with targetId set. Never
        broadcast — the receiving side validates the target id.
        """
        packet.target_id = peer_id
        self._enqueue_send(packet)

    # ------------------------------------------------------- group management

    def send_group_update(
        self, group_name: str = "", announcement: Optional[str] = None
    ) -> bool:
        """Owner-only: publish a new display name and/or announcement to every
        member. Applies locally first (so the owner's UI and persisted row
        follow), then broadcasts a group_update carrying senderId — members
        only accept it when senderId is the group's creator."""
        if not self.is_host:
            return False
        new_name = (group_name or "").strip()
        if not new_name and announcement is None:
            return False
        if new_name:
            old_name = self.group_name
            self.group_name = new_name
            if self._host_server is not None:
                try:
                    self._host_server.rename_registration(self, old_name)
                except Exception:
                    pass
        if announcement is not None:
            self.group_announcement = announcement
        packet = NetworkPacket(
            type="group_update",
            group_id=self.group_id,
            sender_id=self.my_id,
            group_name=new_name or None,
            announcement=announcement,
        )
        groupauth.sign_packet(
            packet,
            groupauth.group_update_parts(self.group_id, self.my_id, new_name, announcement or ""),
        )
        if self.is_host:
            # socket writes go to a worker thread: a slow member must not
            # block the GUI thread that runs this owner action
            self._spawn(self._broadcast_to_clients, packet)
        self.listener.group_info_changed(self)
        return True

    def kick_member(self, target_id: str) -> bool:
        """Owner-only: remove a member. The target receives a directed
        kick_member (then its connection is closed) and every other member
        gets the broadcast so it drops the target from its member list/mesh."""
        if not self.is_host or not target_id:
            return False
        with self._lock:
            known = target_id in self.peers
            # pop the connection BEFORE closing: the read loop's finally only
            # broadcasts peer_left when it still owns the registration, and
            # the owner already announces the removal itself
            conn = self._connected_clients.pop(target_id, None)
            self.peers.pop(target_id, None)
        if not known and conn is None:
            return False
        packet = NetworkPacket(
            type="kick_member",
            group_id=self.group_id,
            sender_id=self.my_id,
            target_id=target_id,
        )
        groupauth.sign_packet(
            packet,
            groupauth.kick_parts(self.group_id, self.my_id, target_id),
        )
        try:
            # all socket writes go to a worker thread: a slow/stalled member
            # must not freeze the GUI thread that runs this owner action
            self._spawn(
                self._send_kick_packet,
                packet,
                conn["wire"] if conn is not None else None,
                target_id,
            )
        except Exception:
            pass
        if conn is not None:
            # give the directed packet a moment to flush before the socket
            # dies; the close is what actually detaches the kicked member
            # (detached on purpose: the closer must not hold the conn/socket
            # across the grace window)
            self._spawn(self._delayed_close, conn["sock"], 0.5)
        self.listener.peers_changed(self)
        return True

    def _send_kick_packet(
        self,
        packet: NetworkPacket,
        wire,
        target_id: str,
    ) -> None:
        """Owner kick delivery off the GUI thread: the directed packet to the
        target (best effort — a dropped member must not skip the broadcast),
        then the broadcast to the remaining members. Holds no socket beyond
        the sends themselves."""
        if wire is not None:
            try:
                wire.send_packet(packet)
            except Exception:
                pass
        self._broadcast_to_clients(packet, exclude=target_id)

    @staticmethod
    def _delayed_close(sock, delay: float) -> None:
        try:
            time.sleep(delay)
        except Exception:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _drop_client(self, peer_id: str) -> None:
        """Detach one connected member (a protocol violation): close its
        socket and tell the remaining members it left."""
        with self._lock:
            conn = self._connected_clients.pop(peer_id, None)
            self.peers.pop(peer_id, None)
        if conn is not None:
            self._safe_close(conn["sock"])
        self._broadcast_to_clients(
            NetworkPacket(type="peer_left", peer=Peer(peer_id, "", "", 0)),
            exclude=peer_id,
        )
        self.listener.peers_changed(self)

    def _creator_id(self) -> str:
        provider = self.creator_id_provider
        if provider is None:
            return ""
        try:
            return str(provider() or "")
        except Exception:
            return ""

    def _handle_group_update_as_client(self, packet: NetworkPacket) -> None:
        creator = self._creator_id()
        if not creator or not packet.sender_id or packet.sender_id != creator:
            logger.warning(
                "reject group_update from %r: not the group owner (%r)",
                packet.sender_id,
                creator,
            )
            self._disconnect_from_host()
            return
        # owner identity binding (TOFU): a signed owner packet must verify
        # against the creatorId's remembered key; unsigned stays legacy
        if not groupauth.verify_group_update(
            self.current_group_id,
            creator,
            packet.group_name or "",
            packet.announcement or "",
            packet.sender_pub_id,
            packet.sender_sig,
        ):
            self._disconnect_from_host()
            return
        changed = False
        new_name = (packet.group_name or "").strip()
        if new_name and new_name != self.group_name:
            self.group_name = new_name
            changed = True
        if packet.announcement is not None:
            self.group_announcement = packet.announcement
            changed = True
        if changed:
            self.listener.group_info_changed(self)
        self._rebroadcast_admin(packet)

    def _handle_kick_as_client(self, packet: NetworkPacket) -> None:
        creator = self._creator_id()
        if not creator or not packet.sender_id or packet.sender_id != creator:
            logger.warning(
                "reject kick_member from %r: not the group owner (%r)",
                packet.sender_id,
                creator,
            )
            self._disconnect_from_host()
            return
        if not groupauth.verify_kick(
            self.current_group_id,
            creator,
            packet.target_id or "",
            packet.sender_pub_id,
            packet.sender_sig,
        ):
            self._disconnect_from_host()
            return
        target = packet.target_id
        if not target:
            return
        if target == self.my_id:
            # the owner removed this device: run the teardown once
            if not self._kicked_from_group:
                self._kicked_from_group = True
                self.listener.kicked_from_group(self)
            return
        with self._lock:
            self.peers.pop(target, None)
        self._rebroadcast_admin(packet)
        self.listener.peers_changed(self)

    def _rebroadcast_admin(self, packet: NetworkPacket) -> None:
        """Forward an owner admin packet over the group mesh so members whose
        host relay is down still receive it (set by the ViewModel)."""
        hook = self.admin_rebroadcast
        if hook is None:
            return
        try:
            hook(packet)
        except Exception:
            pass

    def _disconnect_from_host(self) -> None:
        """Drop the host relay after a protocol violation: the read loop's
        cleanup then publishes connection_lost so the UI can reconnect."""
        with self._lock:
            sock = self._host_socket
        if sock is not None:
            self._safe_close(sock)

    def _send_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                packet, on_failed = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if packet.target_id is not None:
                    # targeted delivery: host writes directly to the member's
                    # wire, client hands the packet to the host for routing
                    if self.is_host:
                        with self._lock:
                            conn = self._connected_clients.get(packet.target_id)
                        if conn is not None:
                            conn["wire"].send_packet(packet)
                    else:
                        wire = self._host_wire
                        if wire is not None:
                            wire.send_packet(packet)
                elif self.is_host:
                    self._broadcast_to_clients(packet)
                else:
                    wire = self._host_wire
                    if wire is None:
                        raise OSError("no host connection")
                    wire.send_packet(packet)
            except Exception:
                if on_failed is not None:
                    try:
                        on_failed()
                    except Exception:
                        pass

    def replay_saved_messages(self, messages: list) -> None:
        with self._lock:
            existing_ids = {m.id for m in self.messages}
            for m in messages:
                if m.id not in existing_ids:
                    self.messages.append(m)

    # -------------------------------------------------------- file transfer

    def send_file(
        self,
        path: str,
        folder_id: str = "",
        folder_name: str = "",
        relative_path: str = "",
        folder_total: int = 0,
        forwarded: Optional[ForwardedInfo] = None,
    ) -> Optional[ChatMessage]:
        """Offer a local file to the group. Returns the created file message
        (or None if the file cannot be served) so the caller can also
        broadcast it over the group mesh. The file bytes travel over a
        separate download server, not over the message stream; a per-file
        random key travels INSIDE the encrypted message channel and protects
        the raw download stream.

        A folder entry passes folder_id/folder_name/relative_path/folder_total
        so receivers can group the offers and rebuild the tree; folder entries
        are always plain "file" kind (they are never rendered inline)."""
        if not path:
            return None
        if not os.path.isfile(path):
            return None
        try:
            file_size = os.path.getsize(path)
            file_name = os.path.basename(path)
        except OSError:
            return None
        if not is_valid_content(file_name):
            return None
        if file_size > MAX_DOWNLOAD_BYTES:
            logger.warning(
                "sendFile rejected: %s bytes exceeds the %s cap", file_size, MAX_DOWNLOAD_BYTES
            )
            return None
        if folder_id:
            relative_path = sanitize_relative_path(relative_path)
            if not relative_path or folder_total > MAX_FOLDER_FILES:
                return None
        file_id = str(uuid.uuid4())
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", 0))
            srv.listen(8)
        except OSError:
            try:
                srv.close()
            except OSError:
                pass
            return None
        srv.settimeout(30.0)
        port = srv.getsockname()[1]
        # refresh the advertised address at offer time: my_ip_address was
        # snapshotted when the group was joined/started, and a client that
        # switched Wi-Fi since would otherwise advertise a stale, unreachable
        # download host
        advertised = get_local_ip_address() or self.my_ip_address
        file_key = random_bytes(KEY_LEN)
        file_info = FileInfo(
            file_id,
            file_name,
            file_size,
            advertised,
            port,
            to_b64(file_key),
            kind=FILE_KIND_FILE if folder_id else detect_media_kind(file_name),
            folder_id=folder_id,
            folder_name=folder_name,
            relative_path=relative_path,
            folder_total=folder_total,
        )
        with self._lock:
            self._file_servers[file_id] = srv
        message = ChatMessage(
            id=file_id,
            content=file_name,
            timestamp=int(time.time() * 1000),
            sender_id=self.my_id,
            sender_name=self.my_name,
            is_from_me=True,
            file_info=file_info,
            forwarded=forwarded,
        )
        groupauth.sign_message(self.current_group_id, message)
        with self._lock:
            self.messages.append(message)
        self.listener.messages_changed(self)
        self._enqueue_send(
            NetworkPacket(type="file_message", message=message),
            on_failed=lambda: self._fail_file_send(message.id),
        )
        self._spawn(self._file_server_loop, file_id, path, file_size, file_key)
        return message

    def _fail_file_send(self, file_id: str) -> None:
        """The file_message never reached the group relay: close its download
        server and retract the local bubble so the UI cannot claim it was
        delivered (mirrors the direct-chat path)."""
        with self._lock:
            srv = self._file_servers.pop(file_id, None)
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        with self._lock:
            self.messages = [m for m in self.messages if m.id != file_id]
        self.listener.messages_changed(self)

    def _file_server_loop(self, file_id: str, path: str, file_size: int, file_key: bytes) -> None:
        deadline = time.time() + FILE_SERVER_TTL
        while not self._stop_event.is_set():
            with self._lock:
                srv = self._file_servers.get(file_id)
            if srv is None:
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                srv.settimeout(min(30.0, remaining))
                client, _ = srv.accept()
            except socket.timeout:
                # idle window with no downloader: keep waiting until the TTL
                # expires (or the server is closed / deactivated), instead of
                # treating the timeout as a fatal error
                continue
            except OSError:
                break
            self._spawn(
                lambda c=client: _serve_file_download(c, file_id, path, file_size, file_key)
            )
        with self._lock:
            if self._file_servers.get(file_id) is not None:
                self._file_servers.pop(file_id, None)
                try:
                    srv.close()
                except OSError:
                    pass

    def download_file(
        self,
        file_info: FileInfo,
        target_path: str,
        progress=None,
        cancel: Optional[threading.Event] = None,
        sock_holder: Optional[list] = None,
        offset: int = 0,
    ) -> tuple:
        """Download a file offered via [file_info] to [target_path]. Blocks the
        calling thread. Returns (ok: bool, message: str); see
        _download_file_offer for [progress]/[cancel]/[sock_holder]/[offset]."""
        return _download_file_offer(
            file_info,
            target_path,
            progress=progress,
            cancel=cancel,
            sock_holder=sock_holder,
            offset=offset,
        )

    # -------------------------------------------------------------- server

    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _configure_server_socket(srv)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(16)
            srv.settimeout(1.0)
            self._server_socket = srv
            self.server_error = None
        except OSError:
            self.server_error = (
                f"无法监听端口 {self.port}，请检查端口是否被占用或防火墙设置（Windows 防火墙需允许入站 TCP {self.port}）"
            )
            self.listener.server_error(self, self.server_error)
            return
        while not self._stop_event.is_set():
            try:
                client, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            ip = addr[0] if addr else ""
            with self._lock:
                rate_ok = (
                    not ip
                    or _allow_handshake_bucket(
                        self._handshake_attempts,
                        ip,
                        self.HANDSHAKE_RATE_LIMIT,
                        self.HANDSHAKE_RATE_WINDOW,
                        time.monotonic(),
                    )
                )
                if rate_ok and self._active_handlers < self.MAX_ACTIVE_CONNECTIONS:
                    self._active_handlers += 1
                    admitted = True
                else:
                    admitted = False
            if not admitted:
                self._safe_close(client)
                continue
            self._spawn(self._handle_incoming_guarded, client)

    def _handle_incoming_guarded(self, sock: socket.socket) -> None:
        try:
            self._handle_incoming(sock)
        finally:
            with self._lock:
                self._active_handlers = max(0, self._active_handlers - 1)

    def _handle_incoming(self, sock: socket.socket) -> None:
        try:
            sock.settimeout(15)
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            first_line = _read_line_bounded(reader)
            if first_line is None:
                self._safe_close(sock)
                return
            try:
                start = NetworkPacket.from_json(first_line)
            except Exception:
                self._safe_close(sock)
                return
            if start.type != Protocol.HS_START or start.hs_mode not in (
                Protocol.MODE_QUERY, Protocol.MODE_JOIN
            ):
                self._safe_close(sock)
                return
            wire = make_wire(sock, reader)
            secured = Handshake.accept(wire, start, lambda mode, gid: self.group_password)
            if secured is None:
                self._safe_close(sock)
                return
            try:
                packet = wire.recv_packet()
            except WireException:
                packet = None
            if packet is None or packet.type != start.hs_mode:
                self._safe_close(sock)
                return
            if start.hs_mode == Protocol.MODE_QUERY:
                self._handle_query_group(sock, wire, packet)
            else:
                self._handle_join(sock, wire, packet)
        except Exception:
            self._safe_close(sock)

    def _broadcast_to_clients(self, packet: NetworkPacket, exclude: Optional[str] = None) -> None:
        with self._lock:
            targets = [
                (pid, c) for pid, c in self._connected_clients.items() if pid != exclude
            ]
        for pid, c in targets:
            try:
                c["wire"].send_packet(packet)
            except Exception:
                pass

    def _safe_close(self, sock: Optional[socket.socket]) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _spawn(self, target, *args) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()


class DirectChatListener:
    """Bridge from the direct-chat worker threads back to the UI thread."""

    def direct_contacts_changed(self) -> None:
        pass

    def direct_messages_changed(self, peer_id: str) -> None:
        pass

    def direct_connect_failed(self, peer, reason: str) -> None:
        pass

    def direct_typing_changed(self, peer_id: str, active: bool) -> None:
        """The peer's typing indicator changed. Advisory: the ViewModel also
        expires an indicator that received no refresh, and a session that
        drops clears it."""
        pass

    def direct_message_edited(self, peer_id: str, message_id: str, new_content: str) -> None:
        """The peer edited its own message (author + session validated by the
        DirectChatManager)."""
        pass

    def direct_reaction_changed(self, peer_id: str, message_id: str, emoji: str, active: bool) -> None:
        """The peer toggled an emoji reaction on a message of this chat."""
        pass

    def direct_pin_changed(self, peer_id: str, message_id: str, active: bool) -> None:
        """The peer pinned/unpinned a message of this chat."""
        pass


class DirectChatManager:
    """Direct member-to-member chat: the management unit is the member, not
    the group. Picking a member immediately pulls up a 1:1 chat over a direct
    TCP connection. A first contact from an UNKNOWN peer is not opened
    automatically: it parks in the contact-request message box and the
    session opens only after the user accepts it; known members reconnect
    directly without confirmation.

    Handshake (see securewire.py): the connection starts with an identity
    handshake — ephemeral ECDH signed by both devices' long-term identity
    keys. Every line after it is AES-256-GCM encrypted. Then the connector
    sends "direct_hello" {peer} and the listener replies "direct_ack" {peer},
    both INSIDE the encrypted channel; a known peer whose identity key changed
    is rejected (possible MITM). Packets reuse the group types:
    chat / file_message / delete_message / ping / pong.

    Pending send: messages composed while the peer is offline are appended to
    the local list (marked pending) and parked in a per-peer outbox. A
    background redial loop keeps trying the peer with growing backoff; the
    moment a session is established — by our dial OR by the peer dialing us —
    the outbox flushes over it in order. A manually added contact only knows a
    placeholder "ip:..." id until the handshake reveals the real device id;
    queued state keyed by the placeholder is migrated to the real id then.

    Presence: "the app is running" IS "online". At start (and on window
    activation / network change) the manager announces itself to every saved
    contact by dialing the ones without a live session — the dial IS the
    notification: it rides the identity handshake, so the peer flips us to
    online with no polling and both sides' outboxes flush. Between events a
    low-frequency sweep repairs sessions that died mid-air; deterministic
    dialing (the member with the smaller id dials, placeholder contacts
    always dial) keeps simultaneous announces from both apps from racing
    into mismatched session pairs (Android parity). A contact the local
    user removed is MARKED (id + endpoint): the removed peer's announces
    must not resurrect it.

    Each session has a single sender thread draining a FIFO queue — enqueue
    order IS wire order, so a chat sent right before a delete reaches the peer
    in that order (Android parity).
    """

    CONNECT_TIMEOUT = 8.0
    READ_TIMEOUT = 45.0
    PING_INTERVAL = 15.0
    SEND_POLL = 0.5
    # Redial cadence while an outbox waits for the peer: starts at
    # REDIAL_BACKOFF after a failed dial and doubles up to REDIAL_MAX_BACKOFF;
    # the loop gives up entirely (message stays queued) after REDIAL_LIFETIME
    # so it never polls forever.
    REDIAL_BACKOFF = 5.0
    REDIAL_MAX_BACKOFF = 30.0
    REDIAL_LIFETIME = 600.0
    # Presence sweep cadence: how often a dead contact session is retried
    # between announce events (app start, window activation). Quiet and
    # low-frequency by design -- link REPAIR, not status polling.
    PRESENCE_SWEEP = 60.0
    # How long a removed-contact mark survives (seconds): blocks the removed
    # peer's announces from resurrecting the contact, but a stale endpoint
    # mark (DHCP gave the address to a new device) must not block a
    # legitimate first contact forever.
    REMOVED_MARK_TTL = 30 * 24 * 3600.0

    def __init__(self):
        self._lock = threading.RLock()
        self._listener: Optional[DirectChatListener] = None
        self._sessions: Dict[str, dict] = {}  # peer_id -> session state
        self._contacts: Dict[str, Peer] = {}
        self._messages: Dict[str, list] = {}  # peer_id -> [ChatMessage]
        self._my_id = ""
        self._my_name = ""
        self._my_ip = ""
        self._my_port = 0
        # Outbound file servers keyed by fileId; each offers one file until
        # its TTL elapses or the session dies / shutdown().
        self._file_servers: Dict[str, socket.socket] = {}
        self._stop_event = threading.Event()
        # Pending-send outbox per peer key: messages composed while offline,
        # flushed in order when a session comes up.
        self._outbox: Dict[str, list] = {}
        # peer key -> "ip:port" endpoint it was last associated with. Lets a
        # handshake migrate alias keys (manually added "ip:..." placeholders)
        # to the peer's REAL device id.
        self._chat_endpoints: Dict[str, str] = {}
        # Peers whose typing indicator is currently on (last event forwarded).
        # Used to avoid duplicate notifications and to emit a final False when
        # the session drops.
        self._typing_peers: set = set()
        # Keys with a redial loop currently running.
        self._redial_loops: set = set()
        # Per-peer dial locks: the presence sweep, the outbox redial loop and
        # an explicit open-chat can all dial the same member at once, and two
        # concurrent handshakes interleave into MISMATCHED session pairs —
        # each side's "replace the old session" then lands on a different
        # one of the two connections, so writes go to a socket the peer
        # already closed and messages silently never arrive.
        self._dial_locks: Dict[str, threading.Lock] = {}
        # RLock (not Lock): the redial loop's finally may re-enter
        # _ensure_redial_loop while still holding this guard to relaunch for a
        # message that raced with loop shutdown.
        self._redial_guard = threading.RLock()
        # Presence: wake semaphore + started flag + in-flight dedupe for the
        # reconnect sweep (see announce_online). A Semaphore (not Event):
        # permits accumulate, so a wake issued while a sweep is still dialing
        # is never lost (Event's clear-after-wait races announce_online and
        # can delay the dial a whole sweep).
        self._presence_wake = threading.Semaphore(0)
        self._presence_started = False
        self._presence_lock = threading.Lock()
        self._presence_dialing: set = set()
        # Scanned contact-QR expectations: (ip, port) -> 安全码 fingerprint the
        # QR declared. The next completed handshake with that endpoint must
        # present exactly this identity key (the QR is an out-of-band channel,
        # so this pins TOFU *before* the first connection — a MITM between the
        # scan and the first dial is rejected instead of silently remembered).
        self._qr_expected_fps: Dict[tuple, str] = {}
        # Removed-contact marks: id / endpoint -> removal time. A peer that
        # keeps announcing must not resurrect a contact the local user
        # deleted; add_contact clears the marks (explicit re-add, group
        # sync, or a handshake from a re-added address). The id->endpoint
        # link makes re-adding clear BOTH marks even when the peer's
        # address changed between removal and re-add (DHCP churn).
        self._removed_ids: Dict[str, float] = {}
        self._removed_endpoints: Dict[str, float] = {}
        self._removed_id_endpoint: Dict[str, str] = {}
        # Marks changed (contact removed / re-added): callable() persisting
        # them, fired on whichever thread made the change.
        self.removed_marks_changed = None
        # Contact-request message box: incoming first-contact / removed-
        # member dials parked for the user to accept or ignore (Android
        # parity). Oldest entries fall off past REQUEST_BOX_MAX so a LAN
        # scanner hammering the port cannot grow the box without bound.
        self._contact_requests: List[ContactRequest] = []
        # Box changed: callable() persisting it, fired on the changing thread.
        self.contact_requests_changed = None
        # Call-signaling bridge: callable(packet) invoked on the session read
        # thread for call_* packets that involve this node (Android parity —
        # direct calls ride the session socket, not the host relay).
        self.on_call_signal = None
        # Session-closed callback: callable(peer_id) invoked on the session
        # read thread so the ViewModel can end any call riding the session.
        self.on_session_closed = None
        # Session-established callback (either direction): the ViewModel uses
        # this to start persistence for sessions the LOCAL user never opened.
        self.on_session_established = None
        # Fired AFTER a freshly established session's outbox flush: callable
        # (peer_id). The ViewModel replays the staged pending-op log here so
        # edits/reactions/pins reach the peer strictly AFTER the messages they
        # reference (receivers drop extras for unknown message ids).
        self.on_ops_flush = None
        # A chat's state moved from an alias key (manually added "ip:..."
        # placeholder id) to the member's real device id, revealed by a
        # handshake: callable(from_id, to_id).
        self.on_chat_migrated = None
        # Transient user-facing events ("已连接 X", offline notices, security
        # warnings): callable(text).
        self.on_event = None

    # ------------------------------------------------------------- identity

    def attach(self, listener: DirectChatListener) -> None:
        self._listener = listener

    @property
    def my_id_value(self) -> str:
        with self._lock:
            return self._my_id

    @property
    def my_name_value(self) -> str:
        with self._lock:
            return self._my_name

    def configure(
        self, my_id: str, my_name: str, my_ip: str, my_port: int, saved_contacts=None
    ) -> None:
        with self._lock:
            self._my_id = my_id
            self._my_name = my_name
            self._my_ip = my_ip
            self._my_port = my_port
            self._contacts = {c.id: c for c in (saved_contacts or [])}
        # identity + saved contacts ready: the app is ONLINE the moment it
        # starts, so announce to every saved contact right away (idempotent:
        # re-configure on nickname/port changes only re-checks dead sessions)
        self.announce_online()

    def is_configured(self) -> bool:
        with self._lock:
            return bool(self._my_id)

    def my_peer(self) -> Peer:
        with self._lock:
            # refresh on every handshake: the local IP may have changed
            # (Wi-Fi switch) since configure() ran (Android parity)
            ip = get_local_ip_address() or self._my_ip
            self._my_ip = ip
            return Peer(self._my_id, self._my_name, ip, self._my_port)

    # ------------------------------------------------------------- contacts

    def contacts_list(self) -> list:
        with self._lock:
            return sorted(self._contacts.values(), key=lambda c: c.name.lower())

    def add_contact(self, contact: Peer) -> None:
        # a contact (re-)added is no longer "removed": clear the marks so
        # the peer's announces are accepted again
        self._unmark(
            contact.id,
            f"{contact.ip_address}:{contact.port}" if contact.ip_address else None,
        )
        changed = False
        with self._lock:
            # dedupe by endpoint: a manually added placeholder (id from ip) is
            # replaced by the real contact once a handshake reveals the id.
            # The reverse must NOT happen: a manual "ip:..." add of an
            # endpoint already known under its REAL device id keeps the real
            # contact — the placeholder knows nothing the real one doesn't,
            # and clobbering it would orphan the chat history keyed by the
            # real id ("re-adding my contact by IP wiped it").
            for old_id in list(self._contacts):
                old = self._contacts[old_id]
                if (
                    old.ip_address == contact.ip_address
                    and old.port == contact.port
                    and old.id != contact.id
                ):
                    if contact.id.startswith("ip:") and not old_id.startswith("ip:"):
                        # keep the known real contact; the re-add still
                        # cleared the removal marks above
                        return
                    del self._contacts[old_id]
                    changed = True
            old_same_id = self._contacts.get(contact.id)
            if self._contacts.get(contact.id) != contact:
                if (
                    old_same_id is not None
                    and old_same_id.ip_address
                    and contact.ip_address
                    and old_same_id.ip_address != contact.ip_address
                ):
                    # A peer's endpoint changed via a trusted group membership
                    # update; the old TOFU binding may have been created by an
                    # impostor, so it must not block the real peer later.
                    # NOTE this hands the group HOST the power to reset TOFU
                    # bindings for member ids — accepted because the host
                    # already controls routing/addressing for its groups (see
                    # DeviceIdentity.forget_peer).
                    DeviceIdentity.forget_peer(contact.id)
                self._contacts[contact.id] = contact
                changed = True
        if changed:
            self._notify_contacts()

    def remove_contact(self, contact_id: str) -> None:
        with self._lock:
            contact = self._contacts.pop(contact_id, None)
        if contact is not None:
            # remember the removal: the peer's app keeps announcing (its
            # presence dials forever), and an incoming hello must not
            # resurrect the contact the user just deleted
            self._mark_removed(
                contact_id,
                f"{contact.ip_address}:{contact.port}" if contact.ip_address else None,
            )
            self._notify_contacts()

    # ------------------------------------------------------- removal marks

    def _mark_removed(self, peer_id: str, endpoint: Optional[str]) -> None:
        now = time.time()
        with self._lock:
            self._removed_ids[peer_id] = now
            if endpoint:
                self._removed_endpoints[endpoint] = now
                self._removed_id_endpoint[peer_id] = endpoint
        self._fire_removed_marks_changed()

    def _unmark(self, peer_id: str, endpoint: Optional[str]) -> None:
        changed = False
        with self._lock:
            changed = self._removed_ids.pop(peer_id, None) is not None
            # clear the endpoint recorded WITH the id mark: the peer's
            # address may have changed between removal and re-add, so
            # clearing only the newly advertised endpoint would leave the
            # stale one blocking the peer's announces
            linked = self._removed_id_endpoint.pop(peer_id, None)
            if linked and self._removed_endpoints.pop(linked, None) is not None:
                changed = True
            if endpoint and self._removed_endpoints.pop(endpoint, None) is not None:
                changed = True
        if changed:
            self._fire_removed_marks_changed()

    def _fire_removed_marks_changed(self) -> None:
        cb = self.removed_marks_changed
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _is_removed(self, peer: Peer) -> bool:
        """True when [peer] is a contact the local user removed (id match, or
        endpoint match for a contact removed under an "ip:..." placeholder
        whose real device id was never learned)."""
        now = time.time()
        with self._lock:
            t = self._removed_ids.get(peer.id)
            if t is not None and now - t < self.REMOVED_MARK_TTL:
                return True
            if peer.ip_address:
                t = self._removed_endpoints.get(f"{peer.ip_address}:{peer.port}")
                if t is not None and now - t < self.REMOVED_MARK_TTL:
                    return True
        return False

    def restore_removed_marks(
        self, ids: Dict[str, float], endpoints: Dict[str, float]
    ) -> None:
        """Restore removal marks persisted by a previous process (entries
        past the TTL are dropped). Must run before announce_online()."""
        now = time.time()
        with self._lock:
            self._removed_ids.update(
                {k: v for k, v in ids.items() if now - v < self.REMOVED_MARK_TTL}
            )
            self._removed_endpoints.update(
                {k: v for k, v in endpoints.items() if now - v < self.REMOVED_MARK_TTL}
            )

    def removed_marks(self) -> tuple:
        """Snapshot of the removal marks (expired entries filtered), for
        persistence by the ViewModel."""
        now = time.time()
        with self._lock:
            ids = {
                k: v for k, v in self._removed_ids.items()
                if now - v < self.REMOVED_MARK_TTL
            }
            endpoints = {
                k: v for k, v in self._removed_endpoints.items()
                if now - v < self.REMOVED_MARK_TTL
            }
        return ids, endpoints

    # ------------------------------------------------ contact-request box

    # Box capacity: a LAN scanner hammering the port must not be able to
    # grow the box without bound. Oldest entries fall off.
    REQUEST_BOX_MAX = 50

    def contact_requests(self) -> list:
        """Snapshot of the parked contact requests, newest last."""
        with self._lock:
            return list(self._contact_requests)

    def _fire_contact_requests_changed(self) -> None:
        cb = self.contact_requests_changed
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def record_contact_request(
        self, peer: Peer, from_removed: bool, peer_fingerprint: str = ""
    ) -> None:
        """Park an incoming request in the message box. Deduped by device id
        AND by endpoint (a placeholder-era peer re-requesting from a new
        address must not stack two rows); only a NEW entry raises the
        user-facing event, so a peer's presence sweep re-dialing every
        minute cannot toast in a loop. [peer_fingerprint] is the dialer's
        identity-key 安全码 proven by the secured handshake — surfaced in
        the request card so the user can verify it out-of-band before
        accepting (first-contact MITM mitigation)."""
        entry = ContactRequest(
            id=peer.id,
            name=peer.name,
            ip=peer.ip_address,
            port=peer.port,
            from_removed=from_removed,
            timestamp=int(time.time() * 1000),
            peer_fingerprint=peer_fingerprint,
        )

        def same_slot(other: ContactRequest) -> bool:
            return other.id == entry.id or (other.ip == entry.ip and other.port == entry.port)

        with self._lock:
            is_new = not any(same_slot(r) for r in self._contact_requests)
            self._contact_requests = (
                [r for r in self._contact_requests if not same_slot(r)] + [entry]
            )[-self.REQUEST_BOX_MAX:]
        if is_new:
            suffix = "（已移除的成员）" if from_removed else ""
            self._emit_event(f"{peer.name} 请求添加你为成员{suffix}")
        self._fire_contact_requests_changed()

    def accept_contact_request(self, request_id: str) -> None:
        """The user accepted a request: add the member (clearing any removal
        marks — acceptance is the explicit un-block), dial the peer RIGHT AWAY
        (quiet; start_chat reuses the per-peer dial lock, so a concurrent
        sweep dial cannot double-connect), and announce. Without the immediate
        dial, convergence waited for the peer's announce or the local 60s
        sweep — and the deterministic dialer rule can pick the OTHER side,
        stretching it to a full sweep period."""
        with self._lock:
            req = next(
                (r for r in self._contact_requests if r.id == request_id), None
            )
            if req is None:
                return
            self._contact_requests = [
                r for r in self._contact_requests if r.id != request_id
            ]
        peer = Peer(req.id, req.name, req.ip, req.port)
        self.add_contact(peer)
        self._emit_event(f"已添加 {req.name}")

        def run(p=peer) -> None:
            try:
                self.start_chat(p, quiet=True)
            except Exception:
                pass

        _spawn(run)
        self.announce_online()
        self._fire_contact_requests_changed()

    def ignore_contact_request(self, request_id: str) -> None:
        """The user ignored a request: drop the entry. The peer is never
        auto-added, so no removal mark is needed; if it insists, its next
        dial simply re-parks the (single, deduped) entry."""
        with self._lock:
            before = len(self._contact_requests)
            self._contact_requests = [
                r for r in self._contact_requests if r.id != request_id
            ]
        if len(self._contact_requests) != before:
            self._fire_contact_requests_changed()

    def restore_contact_requests(self, saved: list) -> None:
        """Restore the request box persisted by a previous process (startup).
        Deduped by id AND endpoint — the same slot semantics the live box
        uses — against BOTH the live rows and the saved list, so a stale
        saved row cannot sit beside its live replacement."""
        if not saved:
            return
        with self._lock:
            live = self._contact_requests

            def taken(other: ContactRequest) -> bool:
                return any(
                    k.id == other.id or (k.ip == other.ip and k.port == other.port)
                    for k in live
                ) or any(
                    k.id == other.id or (k.ip == other.ip and k.port == other.port)
                    for k in kept
                )

            kept: List[ContactRequest] = []
            for r in saved:
                if taken(r):
                    continue
                kept.append(r)
            if kept:
                self._contact_requests = (live + kept)[-self.REQUEST_BOX_MAX:]
        self._fire_contact_requests_changed()

    # -------------------------------------------------------------- presence

    def announce_online(self) -> None:
        """The app is up (start, window activated, contact added): announce
        to every contact by dialing the ones without a live session. The
        dial IS the notification -- it rides the identity handshake, so the
        peer flips us to online with no polling and both sides' outboxes
        flush. Wakes the reconnect sweep immediately; between events the
        sweep alone repairs dead sessions (PRESENCE_SWEEP)."""
        with self._presence_lock:
            if not self._presence_started:
                self._presence_started = True
                _spawn(self._presence_loop)
        self._presence_wake.release()

    def _presence_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._dial_dead_contacts()
            except Exception:
                pass
            # wait for the next wake, or the fallback sweep timeout; extra
            # permits piled up during the dial are drained so N wakes
            # coalesce into one immediate extra pass (Semaphore permits are
            # never lost, unlike an Event's clear)
            if not self._presence_wake.acquire(timeout=self.PRESENCE_SWEEP):
                continue
            while self._presence_wake.acquire(blocking=False):
                pass

    def _dial_dead_contacts(self) -> None:
        """Dial every contact without a live session. Deterministic dialer:
        with real ids on both sides only the member with the SMALLER id
        dials, so a simultaneous announce from both apps cannot race into
        mismatched session pairs (same rule as the group mesh). A
        placeholder "ip:..." contact always dials -- the other side does not
        know this device yet, so nobody else would establish the session."""
        with self._lock:
            my_id = self._my_id
            contacts = list(self._contacts.values())
            live = {pid for pid, s in self._sessions.items() if s["alive"]}
        if not my_id:
            return
        for contact in contacts:
            if not contact.ip_address or contact.port <= 0:
                continue
            if contact.id in live:
                continue
            if not contact.id.startswith("ip:") and my_id >= contact.id:
                continue
            # a redial loop already hammers this peer for the outbox
            if contact.id in self._redial_loops:
                continue
            with self._presence_lock:
                if contact.id in self._presence_dialing:
                    continue
                self._presence_dialing.add(contact.id)

            def run(c=contact):
                try:
                    self.start_chat(c, quiet=True)
                except Exception:
                    pass
                finally:
                    with self._presence_lock:
                        self._presence_dialing.discard(c.id)

            _spawn(run)

    # -------------------------------------------------------------- messages

    def messages_for(self, peer_id: str) -> list:
        with self._lock:
            return list(self._messages.get(peer_id, []))

    def seed_messages(self, peer_id: str, messages) -> None:
        """Seed a freshly opened chat with its persisted history, MERGING into
        any messages already received live (a session may have delivered
        messages before the chat UI opened — overwriting would lose them;
        Android parity)."""
        with self._lock:
            current = self._messages.get(peer_id, [])
            ids = {m.id for m in current}
            merged = current + [m for m in messages if m.id not in ids]
            merged.sort(key=lambda m: m.timestamp)
            self._messages[peer_id] = merged
        self._notify_messages(peer_id)

    def seed_last_message(self, peer_id: str, message: ChatMessage) -> None:
        """Restore just the LAST message of a peer's history after a process
        restart, without opening a full chat — so home page previews are
        populated even before the user reconnects to that peer."""
        with self._lock:
            current = self._messages.get(peer_id, [])
            if not any(m.id == message.id for m in current):
                current = current + [message]
                current.sort(key=lambda m: m.timestamp)
                self._messages[peer_id] = current

    def _append_message(self, peer_id: str, msg: ChatMessage) -> None:
        with self._lock:
            self._messages.setdefault(peer_id, []).append(msg)
        self._notify_messages(peer_id)

    def _remove_message(self, peer_id: str, message_id: str) -> None:
        changed = False
        with self._lock:
            msgs = self._messages.get(peer_id)
            if msgs is None:
                return
            before = len(msgs)
            self._messages[peer_id] = [m for m in msgs if m.id != message_id]
            changed = before != len(self._messages[peer_id])
        if changed:
            self._notify_messages(peer_id)

    def _set_pending(self, peer_id: str, message_id: str, pending: bool) -> None:
        changed = False
        with self._lock:
            msgs = self._messages.get(peer_id)
            if msgs is None:
                return
            for m in msgs:
                if m.id == message_id:
                    if m.pending != pending:
                        m.pending = pending
                        changed = True
                    break
        if changed:
            self._notify_messages(peer_id)

    def _edit_message(self, peer_id: str, message_id: str, new_content: str, editor_id: str) -> bool:
        """Apply an edit to the local direct-chat copy. Only the message's own
        author may rewrite it; returns False (no change) otherwise. A no-op
        edit (same text) still raises the edited flag — the author went
        through the edit flow, and Android shows the marker immediately."""
        changed = False
        with self._lock:
            msgs = self._messages.get(peer_id)
            if msgs is None:
                return False
            for m in msgs:
                if m.id == message_id:
                    if m.sender_id == editor_id and (
                        m.content != new_content or not m.edited
                    ):
                        m.content = new_content
                        m.edited = True
                        changed = True
                    break
        if changed:
            self._notify_messages(peer_id)
        return changed

    def _set_peer_typing(self, peer_id: str, active: bool) -> None:
        """Record/forward a peer typing change, deduped to real transitions."""
        with self._lock:
            was = peer_id in self._typing_peers
            if active:
                self._typing_peers.add(peer_id)
            else:
                self._typing_peers.discard(peer_id)
        if was == active:
            return
        listener = self._listener
        if listener is not None:
            try:
                listener.direct_typing_changed(peer_id, active)
            except Exception:
                pass

    def _clear_peer_typing(self, peer_id: str) -> None:
        """A session ended: whatever it claimed about typing is stale."""
        self._set_peer_typing(peer_id, False)

    def _apply_read_receipt(self, peer_id: str, up_to_id: str) -> None:
        """Mark every OWN message up to the receipt's message id as read. The
        receipt names a peer message id, so the cut-off is that message's
        timestamp; an id we no longer hold (history pruned) is ignored."""
        with self._lock:
            msgs = self._messages.get(peer_id)
            if not msgs:
                return
            target = next((m for m in msgs if m.id == up_to_id), None)
            if target is None:
                return
            changed = False
            for m in msgs:
                if (
                    m.is_from_me
                    and not m.read
                    and m.timestamp <= target.timestamp
                ):
                    m.read = True
                    changed = True
        if changed:
            self._notify_messages(peer_id)

    # ---------------------------------------------------------------- actions

    def open_chat(self, contact: Peer) -> None:
        """Record the endpoint a chat key refers to (called when the chat is
        opened or a message is queued): a later handshake uses it to migrate
        alias keys to the revealed real device id."""
        with self._lock:
            self._chat_endpoints[contact.id] = f"{contact.ip_address}:{contact.port}"

    def set_qr_expected_fingerprint(self, ip: str, port: int, fingerprint: str) -> None:
        """Pin the 安全码 a scanned contact QR declared for [ip]:[port]. The
        next completed handshake with that endpoint must present this exact
        identity key, or the session is refused (see _dial_peer)."""
        with self._lock:
            self._qr_expected_fps[(ip, port)] = (fingerprint or "").upper()

    def _take_qr_expected_fingerprint(self, ip: str, port: int) -> str:
        with self._lock:
            return self._qr_expected_fps.pop((ip, port), "")

    def start_chat(self, peer: Peer, quiet: bool = False) -> Optional[str]:
        """Pull up a chat with a member: connect and run the identity
        handshake. The other side auto-accepts. Returns the member's REAL
        device id on success (a manually added contact only knows the
        placeholder "ip:..." id until the handshake reveals the real one), or
        None when unreachable. [quiet] suppresses the user-facing failure
        event (background redials would otherwise toast every few seconds)."""
        with self._lock:
            if not self._my_id or peer.id == self._my_id:
                return None
            existing = self._sessions.get(peer.id)
            if existing is not None and existing["alive"]:
                return peer.id
            dial_lock = self._dial_locks.setdefault(peer.id, threading.Lock())
        self.add_contact(peer)
        # serialize dials per peer (see _dial_locks): a concurrent dial may
        # have established the session while we waited for the lock
        with dial_lock:
            with self._lock:
                existing = self._sessions.get(peer.id)
                if existing is not None and existing["alive"]:
                    return peer.id
            return self._dial_peer(peer, quiet)

    def _dial_peer(self, peer: Peer, quiet: bool) -> Optional[str]:
        """The actual dial + secured handshake (runs under the peer's dial
        lock)."""
        sock = None
        try:
            sock = socket.create_connection(
                (peer.ip_address, peer.port), timeout=self.CONNECT_TIMEOUT
            )
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.CONNECT_TIMEOUT)
            # Identity-based secured handshake: ephemeral ECDH signed by both
            # devices' long-term identity keys — the session (chat, files,
            # call signaling) is encrypted end-to-end and a MITM fails the
            # signature / TOFU check. The session stays line-based for its
            # whole life, so a buffered reader is safe (and far cheaper than
            # the byte-at-a-time raw reader).
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            wire = make_wire(sock, reader)
            secured = Handshake.initiate_direct(
                wire,
                expected_peer_id=(
                    peer.id if peer.id and not peer.id.startswith("ip:") else None
                ),
                on_identity_mismatch=lambda: self._emit_event(
                    f"安全警告：{peer.name} 的设备身份发生变化，连接已拒绝（可能存在中间人攻击）"
                ),
            )
            # Scanned-QR pin (out-of-band TOFU): the handshake signature just
            # proved WHICH key the endpoint holds; if a QR declared a different
            # one, someone is intercepting between the scan and this dial.
            qr_fp = self._take_qr_expected_fingerprint(peer.ip_address, peer.port)
            if qr_fp and DeviceIdentity.peer_fingerprint(
                secured.peer_ident or ""
            ) != qr_fp:
                self._emit_event(
                    f"安全警告：{peer.name} 的安全码与二维码不一致，连接已拒绝（可能存在中间人攻击）"
                )
                raise WireException("对方安全码与二维码不一致")
            wire.send_packet(
                NetworkPacket(type=Protocol.DIRECT_HELLO, peer=self.my_peer())
            )
            ack = wire.recv_packet()
            if ack is not None and ack.type == Protocol.DIRECT_PENDING:
                # The peer parked our request in its contact-request message
                # box instead of accepting (first contact or removed-and-
                # re-requesting): NOT a failure. Loud dials surface "waiting
                # for confirmation"; the session comes up once the peer
                # accepts and dials back / our sweep redials.
                if not quiet:
                    self._emit_event(
                        f"已向 {peer.name} 发送连接请求，等待对方在其设备上确认"
                    )
                self._safe_close(sock)
                return None
            if ack is None or ack.type != Protocol.DIRECT_ACK or ack.peer is None:
                raise OSError("bad direct_ack")
            remote = ack.peer
            # the handshake revealed the peer's identity key; bind it to the
            # real device id the ack just disclosed (TOFU)
            if secured.peer_ident and not DeviceIdentity.check_peer(
                remote.id, secured.peer_ident
            ):
                self._emit_event(
                    f"安全警告：{remote.name} 的设备身份发生变化，连接已拒绝（可能存在中间人攻击）"
                )
                raise OSError("peer identity changed")
            self._on_established(sock, wire, remote)
            # _on_established migrated aliases by the peer's ADVERTISED
            # address; a multi-homed peer advertises a different local IP than
            # the one we dialed, so also migrate by the dialed endpoint — our
            # dial reaching THAT device is proof enough of identity — and
            # flush again: _on_established's flush ran before this merge
            self._migrate_aliases_for(f"{peer.ip_address}:{peer.port}", remote.id)
            with self._lock:
                session = self._sessions.get(remote.id)
            if session is not None and session["alive"]:
                self._flush_outbox(remote.id, session)
                # the alias merge above may have queued more messages after
                # _on_established's flush: replay ops once more behind them
                # (idempotent — receivers absorb repeats)
                self._fire_ops_flush(remote.id)
            sock = None  # ownership transferred to the session
            return remote.id
        except Exception as e:
            if not quiet:
                reason = "未知错误"
                if isinstance(e, socket.timeout):
                    reason = "无响应（请确认对方应用在运行且在同一网络）"
                elif isinstance(e, ConnectionRefusedError):
                    reason = "连接被拒绝（对方应用未运行或端口不对）"
                elif str(e) == "bad direct_ack":
                    # the handshake SUCCEEDED but the peer hung up instead of
                    # acking: the classic signature of a peer that dropped the
                    # hello on purpose (removed contact) — or died mid-handshake
                    reason = "对方未接受连接（应用刚退出、不在同一网络，或已被对方移除）"
                elif isinstance(e, OSError):
                    reason = f"网络不可达（{e}）"
                elif isinstance(e, WireException):
                    reason = str(e)
                listener = self._listener
                if listener is not None:
                    try:
                        listener.direct_connect_failed(peer, reason)
                    except Exception:
                        pass
            self._safe_close(sock)
            return None

    def handle_direct_hello(self, sock, wire: Wire, packet: NetworkPacket, peer_ident) -> None:
        """Listener side: a secured direct_hello arrived on the shared port.
        A KNOWN member is accepted right away (the handshake already
        authenticated its identity key); a first contact parks in the
        request box and the session opens only after the user confirms."""
        peer = packet.peer
        if peer is None or peer.id == self._my_id:
            self._safe_close(sock)
            return
        # First contact / removed member -> the request box. Nothing here is
        # dropped silently: the box entry is visible (deduped, one row per
        # peer, one event per NEW entry) and the dialer gets a definitive
        # "pending" answer instead of a hung-up connection. A KNOWN member
        # (id or endpoint) is auto-accepted below — the handshake already
        # authenticated the dialer's identity key.
        removed = self._is_removed(peer)
        with self._lock:
            known = peer.id in self._contacts or any(
                c.ip_address == peer.ip_address and c.port == peer.port
                for c in self._contacts.values()
            )
        if removed or not known:
            fingerprint = (
                DeviceIdentity.peer_fingerprint(peer_ident) if peer_ident else ""
            )
            self.record_contact_request(
                peer, from_removed=removed, peer_fingerprint=fingerprint
            )
            try:
                wire.send_packet(
                    NetworkPacket(type=Protocol.DIRECT_PENDING)
                )
            except Exception:
                pass
            self._safe_close(sock)
            return
        # Identity-first: when the handshake already proves the peer's KNOWN
        # long-term key, the peer is authenticated by CRYPTOGRAPHY — an
        # address mismatch is then multi-homing (VPN/second NIC) or DHCP
        # churn, not impersonation, and must not block the session. The
        # address binding below therefore only guards FIRST CONTACT (unknown
        # identity), where an attacker claiming another member's UUID could
        # otherwise poison the TOFU map before the real member ever connects.
        identity_proven = (
            peer_ident is not None
            and DeviceIdentity.has_peer(peer.id)
            and DeviceIdentity.check_peer(peer.id, peer_ident, remember=False)
        )
        if not identity_proven:
            # Bind the hello to the TCP peer that actually dialed us.
            # Loopback is exempt: a 127.x source is necessarily THIS machine
            # (tests, local dev; the dialer advertises its LAN address but
            # connects over loopback), never a LAN impostor.
            try:
                actual_ip = sock.getpeername()[0]
            except OSError:
                actual_ip = ""
            try:
                from_loopback = ipaddress.ip_address(actual_ip).is_loopback
            except ValueError:
                from_loopback = False
            if (
                not from_loopback
                and actual_ip
                and peer.ip_address
                and actual_ip != peer.ip_address
            ):
                self._emit_event(
                    f"安全警告：{peer.name} 声称的地址与连接来源不一致，连接已拒绝"
                )
                self._safe_close(sock)
                return
            with self._lock:
                existing = self._contacts.get(peer.id)
            if (
                existing is not None
                and existing.ip_address
                and peer.ip_address
                and existing.ip_address != peer.ip_address
            ):
                self._emit_event(
                    f"安全警告：{peer.name} 的地址与已知成员不一致，连接已拒绝"
                )
                self._safe_close(sock)
                return
        # TOFU: a changed identity key for a KNOWN peer id means someone is
        # impersonating or intercepting it — refuse the session. (For an
        # unknown peer this is also the moment the key gets remembered: the
        # address binding above has already passed.)
        if peer_ident and not DeviceIdentity.check_peer(peer.id, peer_ident):
            self._emit_event(
                f"安全警告：{peer.name} 的设备身份发生变化，连接已拒绝（可能存在中间人攻击）"
            )
            self._safe_close(sock)
            return
        try:
            wire.send_packet(
                NetworkPacket(type=Protocol.DIRECT_ACK, peer=self.my_peer())
            )
        except Exception:
            self._safe_close(sock)
            return
        self.add_contact(peer)
        self._on_established(sock, wire, peer)

    def send_packet(self, peer_id: str, packet: NetworkPacket) -> bool:
        """Send an arbitrary packet over a live session (call signaling rides
        the direct connection). Returns False when there is no session.
        Enqueued, not written: the session's single sender thread drains the
        queue in FIFO order, so concurrent callers cannot interleave or
        reorder their lines inside the TCP stream."""
        with self._lock:
            session = self._sessions.get(peer_id)
        if session is None or not session["alive"]:
            return False
        self._put_send(session, packet)
        return True

    def send_message(
        self,
        peer_id: str,
        content: str,
        reply_to: Optional[str] = None,
        reply_preview: Optional[str] = None,
        reply_sender: Optional[str] = None,
        quote_sender: Optional[str] = None,
        quote_kind: Optional[str] = None,
        forwarded: Optional[ForwardedInfo] = None,
    ) -> bool:
        """Send a text message. The peer does NOT have to be online: with no
        live session the message is appended locally (marked pending), parked
        in the outbox, and delivered automatically once a session comes up —
        our redial or the peer dialing us. Returns False only when there is
        no known contact AND no session to deliver to. The optional reply_*
        triple attaches a quoted header (see ChatMessage);
        quote_sender/quote_kind fill the nested quote object; [forwarded]
        carries display-only forward provenance."""
        # validate with the SAME rule the receiver enforces: the receiver
        # drops content longer than MAX_CONTENT_LENGTH, so without this check
        # a too-long message would "send" locally but silently never arrive
        if not is_valid_content(content):
            return False
        with self._lock:
            contact = self._contacts.get(peer_id)
            session = self._sessions.get(peer_id)
            my_id, my_name = self._my_id, self._my_name
        alive = session is not None and session["alive"]
        if session is None and contact is None:
            return False
        msg = ChatMessage(
            id=str(uuid.uuid4()),
            content=content,
            timestamp=int(time.time() * 1000),
            sender_id=my_id,
            sender_name=my_name,
            is_from_me=True,
            pending=not alive,
            reply_to=reply_to,
            reply_preview=reply_preview,
            reply_sender=reply_sender,
            quote_sender=quote_sender,
            quote_kind=quote_kind,
            forwarded=forwarded,
        )
        # show the message locally right away (pending until delivered)
        self._append_message(peer_id, msg)
        if alive:
            self._put_send(
                session,
                NetworkPacket(type="chat", message=msg),
                # a dead socket must not swallow the message: park it as
                # pending again and let the redial loop re-deliver (the
                # receiver dedups by id, so an uncertain send is safe)
                on_failed=(lambda m=msg: self._restore_undelivered(peer_id, [m])),
            )
        elif contact is not None:
            self._enqueue_pending(peer_id, contact, msg)
        return True

    def send_typing(self, peer_id: str, active: bool) -> None:
        """Send a typing indicator to a direct-chat peer. Advisory and never
        queued offline: with no live session it is dropped (nobody to show it
        to). The scope names THIS device's conversation key for the chat, so
        the receiver's "direct:<peer>" key matches it exactly."""
        with self._lock:
            session = self._sessions.get(peer_id)
            my_id = self._my_id
        if session is None or not session["alive"]:
            return
        try:
            self._put_send(
                session,
                NetworkPacket(
                    type="typing",
                    group_id="direct:" + my_id,
                    sender_id=my_id,
                    active=bool(active),
                ),
            )
        except Exception:
            pass

    def _send_read_receipt(self, peer_id: str, up_to_id: str) -> None:
        """Tell [peer_id] that everything up to [up_to_id] has been read.
        Only sent on a live session (a receipt for a message that arrived
        while offline is meaningless)."""
        with self._lock:
            session = self._sessions.get(peer_id)
            my_id = self._my_id
        if session is None or not session["alive"]:
            return
        try:
            self._put_send(
                session,
                NetworkPacket(
                    type="read_receipt",
                    group_id="direct:" + my_id,
                    up_to_id=up_to_id,
                    reader_id=my_id,
                ),
            )
        except Exception:
            pass

    def edit_message(self, peer_id: str, message_id: str, new_content: str) -> bool:
        """Edit one of OUR direct-chat messages on a LIVE session (Android
        parity, deletes semantics). Online-only on purpose: an offline edit
        can never reach the peer, and applying half of it (local list only)
        used to diverge from the persisted copy — the edit visibly reverted
        on restart while the peer kept the original forever. Returns False
        when there is no live session (nothing changes at all), the message
        is not ours, or the wire rejected the packet."""
        if not is_valid_content(new_content):
            return False
        with self._lock:
            session = self._sessions.get(peer_id)
            my_id = self._my_id
        if session is None or not session["alive"]:
            return False
        target = next(
            (m for m in self._messages.get(peer_id, []) if m.id == message_id), None
        )
        if target is None or target.sender_id != my_id:
            return False
        try:
            self._put_send(
                session,
                NetworkPacket(
                    type="edit_message",
                    group_id="direct:" + peer_id,
                    message_id=message_id,
                    sender_id=my_id,
                    new_content=new_content,
                ),
            )
        except Exception:
            return False
        # applied only after the packet was handed to the live wire: local
        # list and persisted copy can never disagree about an accepted edit
        self._edit_message(peer_id, message_id, new_content, my_id)
        # mark edited even when the new text equals the old one (the user
        # still went through the edit flow)
        with self._lock:
            target.edited = True
        self._notify_messages(peer_id)
        return True

    def send_direct_reaction(self, peer_id: str, message_id: str, emoji: str, active: bool) -> bool:
        """Toggle an emoji reaction on a live direct session. Advisory: no
        outbox — reactions are only meaningful while the peer is online."""
        emoji = sanitize_emoji(emoji)
        if not emoji:
            return False
        with self._lock:
            session = self._sessions.get(peer_id)
            my_id = self._my_id
        if session is None or not session["alive"]:
            return False
        try:
            self._put_send(
                session,
                NetworkPacket(
                    type="reaction",
                    group_id="direct:" + peer_id,
                    message_id=message_id,
                    sender_id=my_id,
                    emoji=emoji,
                    active=bool(active),
                ),
            )
        except Exception:
            pass
        return True

    def send_direct_pin(self, peer_id: str, message_id: str, active: bool) -> bool:
        """Pin/unpin a message of a direct chat on a live session."""
        with self._lock:
            session = self._sessions.get(peer_id)
            my_id = self._my_id
        if session is None or not session["alive"]:
            return False
        try:
            self._put_send(
                session,
                NetworkPacket(
                    type="pin_message",
                    group_id="direct:" + peer_id,
                    message_id=message_id,
                    sender_id=my_id,
                    active=bool(active),
                ),
            )
        except Exception:
            pass
        return True

    def _enqueue_pending(self, peer_id: str, contact: Peer, msg: ChatMessage) -> None:
        """Park a message for a currently-offline peer and start the redial loop."""
        with self._lock:
            q = self._outbox.setdefault(peer_id, [])
            first = not q
            q.append(msg)
            self._chat_endpoints[peer_id] = f"{contact.ip_address}:{contact.port}"
        if first:
            self._emit_event("对方未在线，消息将在对方上线后自动发送")
        self._ensure_redial_loop(peer_id)
        # race heal: a session may have come up between the alive check and
        # the enqueue — flush inline so the message is never stranded (the
        # flush only enqueues onto the session's sender queue; no socket I/O
        # happens on the caller's, possibly main, thread)
        with self._lock:
            session = self._sessions.get(peer_id)
        if session is not None and session["alive"]:
            self._flush_outbox(peer_id, session)
            self._fire_ops_flush(peer_id)

    def _ensure_redial_loop(self, peer_id: str) -> None:
        """Keep dialing a peer while messages wait in its outbox, with growing
        backoff. Deliberately quiet: the UI already shows the offline state,
        so failures only reach the log (a loud toast every few seconds would
        be noise, not information)."""
        with self._redial_guard:
            if peer_id in self._redial_loops:
                return
            self._redial_loops.add(peer_id)

        def run() -> None:
            try:
                started = time.time()
                backoff = self.REDIAL_BACKOFF
                while time.time() - started < self.REDIAL_LIFETIME:
                    with self._lock:
                        q = self._outbox.get(peer_id)
                        queued = bool(q)
                        contact = self._contacts.get(peer_id)
                        session = self._sessions.get(peer_id)
                    if not queued:
                        break
                    if session is not None and session["alive"]:
                        self._flush_outbox(peer_id, session)
                        self._fire_ops_flush(peer_id)
                        break
                    # the contact may have been removed meanwhile — then there
                    # is no address left to dial and the loop must stop
                    if contact is None:
                        break
                    try:
                        self.start_chat(contact, quiet=True)
                    except Exception:
                        pass
                    with self._lock:
                        q = self._outbox.get(peer_id)
                        still = bool(q)
                        session = self._sessions.get(peer_id)
                    if not still or (session is not None and session["alive"]):
                        break
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.REDIAL_MAX_BACKOFF)
            finally:
                with self._redial_guard:
                    self._redial_loops.discard(peer_id)
                    # a message enqueued between the loop's last check and the
                    # flag removal must not strand the queue: relaunch at once
                    with self._lock:
                        queued = bool(self._outbox.get(peer_id))
                        contact = self._contacts.get(peer_id)
                        session = self._sessions.get(peer_id)
                    if queued and (session is None or not session["alive"]) and contact:
                        self._ensure_redial_loop(peer_id)

        _spawn(run)

    def _flush_outbox(self, peer_id: str, session: dict) -> None:
        """Deliver every queued message for [peer_id] over the session, in
        order. Each message only flips to delivered (pending=false) once its
        line is actually WRITTEN; a write failure parks it back in the outbox
        as pending (at-least-once delivery — the receiver dedups by id)."""
        with self._lock:
            q = self._outbox.get(peer_id)
            if not q:
                return
            to_send = list(q)
            q.clear()
        for msg in to_send:
            self._put_send(
                session,
                NetworkPacket(type="chat", message=msg),
                on_sent=(lambda m=msg: self._set_pending(peer_id, m.id, False)),
                on_failed=(lambda m=msg: self._restore_undelivered(peer_id, [m])),
            )

    def _put_send(self, session: dict, packet: NetworkPacket, on_sent=None, on_failed=None) -> None:
        """Enqueue (packet, on_sent, on_failed) for the session's sender
        thread: on_sent runs after a successful write, on_failed when the
        write failed (or the sender is already gone, so nothing is silently
        stranded in a queue nobody drains)."""
        session["send_queue"].put((packet, on_sent, on_failed))
        if not session["alive"]:
            self._run_send_cb(on_failed)

    @staticmethod
    def _run_send_cb(cb) -> None:
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    def _restore_undelivered(self, peer_id: str, msgs: list) -> None:
        """Put messages whose send FAILED back into the outbox as pending:
        they re-deliver on the next session (ours via the redial loop, or the
        peer dialing us). Re-inserted in call order, so overall FIFO order is
        preserved; the receiver's id dedup absorbs any re-send."""
        with self._lock:
            q = self._outbox.setdefault(peer_id, [])
            ids = {m.id for m in q}
            restored = [m for m in msgs if m.id not in ids]
            if not restored:
                return
            q.extend(restored)
        for m in restored:
            self._set_pending(peer_id, m.id, True)
        if not self._stop_event.is_set():
            self._ensure_redial_loop(peer_id)

    def _merge_chat_state(self, from_key: str, to_key: str) -> None:
        """Merge one chat key's in-memory state (list + outbox) into another
        key: used when a handshake reveals that an "ip:..." alias and a real
        device id are the same member."""
        if from_key == to_key:
            return
        with self._lock:
            from_msgs = self._messages.pop(from_key, None)
            if from_msgs is None:
                return
            current = self._messages.setdefault(to_key, [])
            ids = {m.id for m in current}
            merged = current + [m for m in from_msgs if m.id not in ids]
            merged.sort(key=lambda m: m.timestamp)
            self._messages[to_key] = merged
            q = self._outbox.pop(from_key, None)
            if q:
                self._outbox.setdefault(to_key, []).extend(q)

    def _migrate_aliases_for(self, endpoint: str, real_id: str) -> None:
        """Re-key every chat alias recorded for [endpoint] to [realId].
        Endpoint matching is the only safe signal for alias identity — the
        endpoint a handshake reveals vs. the one recorded at queue time must
        AGREE, so mismatched advertisement (multi-homed peers advertise a
        different local IP than the dialed one) simply skips the migration
        rather than merging two different members' chats."""
        with self._lock:
            keys = list(self._chat_endpoints.keys()) + list(self._outbox.keys())
            aliases = [
                k for k in keys
                if k != real_id and self._chat_endpoints.get(k) == endpoint
            ]
            for alias in aliases:
                self._chat_endpoints.pop(alias, None)
            # The endpoint now belongs to [real_id]: drop a manually added
            # "ip:<endpoint>" placeholder CONTACT for it. add_contact's
            # dedupe cannot do this when the peer advertises a DIFFERENT
            # local IP (multi-homed / DHCP churn) — the placeholder then
            # survived every handshake as a duplicate member row that
            # dialed forever and never merged.
            dropped_placeholder = self._contacts.pop(f"ip:{endpoint}", None)
        if dropped_placeholder is not None:
            self._notify_contacts()
        for alias in aliases:
            self._merge_chat_state(alias, real_id)
            cb = self.on_chat_migrated
            if cb is not None:
                try:
                    cb(alias, real_id)
                except Exception:
                    pass

    def restore_pending(self, peer_id: str, messages) -> None:
        """Re-queue messages persisted as pending by a previous process;
        called at startup for each chat with undelivered messages."""
        if not messages:
            return
        self.seed_messages(peer_id, messages)
        with self._lock:
            self._outbox.setdefault(peer_id, []).extend(messages)
            contact = self._contacts.get(peer_id)
            endpoint = (
                f"{contact.ip_address}:{contact.port}" if contact is not None else None
            )
            # placeholder ids encode their endpoint themselves
            if endpoint is None and peer_id.startswith("ip:"):
                endpoint = peer_id[3:]
            if endpoint is not None:
                self._chat_endpoints[peer_id] = endpoint
        if contact is not None:
            self._ensure_redial_loop(peer_id)

    def send_file(
        self,
        peer_id: str,
        path: str,
        folder_id: str = "",
        folder_name: str = "",
        relative_path: str = "",
        folder_total: int = 0,
    ) -> Optional[ChatMessage]:
        """Offer a local file to a direct-chat member. The bytes are NOT sent
        over the message stream: this opens a short-lived download server on a
        random port and sends a file_message carrying [FileInfo] (incl. the
        download address and per-file key). The receiver connects back to
        download the file (shared encrypted file_download protocol, Android
        parity). Requires a live session (files cannot queue offline). A
        folder entry adds the optional folder metadata (see group send_file)."""
        if not path:
            return None
        if not os.path.isfile(path):
            return None
        try:
            file_size = os.path.getsize(path)
            file_name = os.path.basename(path)
        except OSError:
            return None
        if not is_valid_content(file_name):
            return None
        if file_size > MAX_DOWNLOAD_BYTES:
            return None
        if folder_id:
            relative_path = sanitize_relative_path(relative_path)
            if not relative_path or folder_total > MAX_FOLDER_FILES:
                return None
        with self._lock:
            session = self._sessions.get(peer_id)
            if session is None or not session["alive"]:
                return None
            my_id = self._my_id
            my_name = self._my_name
        my_ip = get_local_ip_address() or self._my_ip
        file_id = str(uuid.uuid4())
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", 0))
            srv.listen(8)
        except OSError:
            try:
                srv.close()
            except OSError:
                pass
            return None
        srv.settimeout(30.0)
        port = srv.getsockname()[1]
        # per-file random key: travels INSIDE the encrypted message channel
        # and protects the raw download stream
        file_key = random_bytes(KEY_LEN)
        file_info = FileInfo(
            file_id,
            file_name,
            file_size,
            my_ip,
            port,
            to_b64(file_key),
            kind=FILE_KIND_FILE if folder_id else detect_media_kind(file_name),
            folder_id=folder_id,
            folder_name=folder_name,
            relative_path=relative_path,
            folder_total=folder_total,
        )
        with self._lock:
            self._file_servers[file_id] = srv
        msg = ChatMessage(
            id=file_id,
            content=file_name,
            timestamp=int(time.time() * 1000),
            sender_id=my_id,
            sender_name=my_name,
            is_from_me=True,
            file_info=file_info,
        )
        self._append_message(peer_id, msg)
        self._put_send(
            session,
            NetworkPacket(type="file_message", message=msg),
            on_failed=lambda: self._fail_file_send(peer_id, file_id),
        )
        self._spawn(self._direct_file_server_loop, file_id, srv, path, file_size, file_key)
        return msg

    def _fail_file_send(self, peer_id: str, file_id: str) -> None:
        """A file_message never made it onto the wire: close its download
        server and remove the local bubble so the UI cannot claim it was
        delivered."""
        with self._lock:
            srv = self._file_servers.pop(file_id, None)
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        self._remove_message(peer_id, file_id)
        self._emit_event("文件发送失败，请重试")

    def download_file(
        self,
        file_info: FileInfo,
        target_path: str,
        progress=None,
        cancel: Optional[threading.Event] = None,
        sock_holder: Optional[list] = None,
        offset: int = 0,
    ) -> tuple:
        """Download a file offered via [file_info] to [target_path]. Blocks the
        calling thread. Returns (ok: bool, message: str); see
        _download_file_offer for [progress]/[cancel]/[sock_holder]/[offset]."""
        return _download_file_offer(
            file_info,
            target_path,
            progress=progress,
            cancel=cancel,
            sock_holder=sock_holder,
            offset=offset,
        )

    def _direct_file_server_loop(
        self, file_id: str, srv, path: str, file_size: int, file_key: bytes
    ) -> None:
        """Accept loop for a direct-chat file offer: serve downloaders until
        the app shuts down or the TTL elapses (Android parity — the offer stays
        valid while the sender's process lives, even across session
        reconnects), then remove and close itself."""
        deadline = time.time() + FILE_SERVER_TTL
        while not self._stop_event.is_set():
            with self._lock:
                current = self._file_servers.get(file_id)
            if current is not srv:
                return
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                srv.settimeout(min(30.0, remaining))
                client, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self._spawn(
                lambda c=client: _serve_file_download(c, file_id, path, file_size, file_key)
            )
        with self._lock:
            if self._file_servers.get(file_id) is srv:
                self._file_servers.pop(file_id, None)
        try:
            srv.close()
        except OSError:
            pass

    def delete_message(self, peer_id: str, message_id: str, sender_id: str) -> None:
        """Delete a message in a direct chat: the sender broadcasts it,
        everyone (including the sender) removes it locally. A still-queued
        (pending) message is dropped from the outbox instead of being
        broadcast."""
        with self._lock:
            session = self._sessions.get(peer_id)
            my_id = self._my_id
            q = self._outbox.get(peer_id)
            was_pending = False
            if sender_id == my_id and q is not None:
                before = len(q)
                q[:] = [m for m in q if m.id != message_id]
                was_pending = before != len(q)
        if sender_id == my_id and session is not None and not was_pending:
            self._put_send(
                session,
                NetworkPacket(type="delete_message", message_id=message_id, sender_id=my_id),
            )
        self._remove_message(peer_id, message_id)

    def close_chat(self, peer_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(peer_id, None)
        if session is not None:
            session["alive"] = False
            self._safe_close(session["sock"])
        self._clear_peer_typing(peer_id)

    def is_chat_alive(self, peer_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(peer_id)
            return bool(session and session["alive"])

    def send_packet_to(
        self, peer_id: str, packet: NetworkPacket, on_sent=None, on_failed=None
    ) -> bool:
        """Hand one packet to the LIVE session with [peer_id] (False when the
        session is gone — the caller keeps its pending op staged). The
        session's writer thread runs [on_sent] after the line is written and
        [on_failed] when the write failed, so the caller can delete a staged
        op only once it actually left this device."""
        with self._lock:
            session = self._sessions.get(peer_id)
            if session is None or not session["alive"]:
                self._run_send_cb(on_failed)
                return False
        try:
            self._put_send(session, packet, on_sent=on_sent, on_failed=on_failed)
        except Exception:
            self._run_send_cb(on_failed)
            return False
        return True

    def shutdown(self) -> None:
        self._stop_event.set()
        # release the presence loop so a shutdown process exits promptly
        self._presence_wake.release()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            file_socks = list(self._file_servers.values())
            self._file_servers.clear()
        for s in sessions:
            s["alive"] = False
            self._safe_close(s["sock"])
        for fs in file_socks:
            try:
                fs.close()
            except OSError:
                pass

    # ------------------------------------------------------------- internals

    def _on_established(self, sock, wire: Wire, peer: Peer) -> None:
        # learn the real member identity: dedupe-by-endpoint replaces a
        # manually added "ip:..." placeholder contact with the real one
        self.add_contact(peer)
        # re-key any alias chat state (queued under a placeholder id) to the
        # real device id BEFORE flushing, so the outbox drains under it
        self._migrate_aliases_for(f"{peer.ip_address}:{peer.port}", peer.id)
        session = {
            "peer_id": peer.id,
            "peer_name": peer.name,
            "sock": sock,
            "wire": wire,
            "alive": True,
            "send_queue": queue.Queue(),
        }
        with self._lock:
            # A reconnect with the same peer id replaces the OLD session: the
            # old socket is closed, and its read-loop finally must not tear
            # down the freshly established one (guarded via conditional map
            # removal).
            previous = self._sessions.get(peer.id)
            self._sessions[peer.id] = session
        if previous is not None and previous is not session:
            previous["alive"] = False
            self._safe_close(previous["sock"])
        self._emit_event(f"已连接 {peer.name}")
        cb = self.on_session_established
        if cb is not None:
            try:
                cb(peer.id)
            except Exception:
                pass
        # deliver everything that piled up while the peer was offline, then
        # let the ViewModel replay its staged pending ops (they must follow
        # the messages they reference)
        self._flush_outbox(peer.id, session)
        self._fire_ops_flush(peer.id)
        _spawn(self._send_loop, session)
        _spawn(self._read_loop, session)
        _spawn(self._ping_loop, session)

    def _fire_ops_flush(self, peer_id: str) -> None:
        cb = self.on_ops_flush
        if cb is None:
            return
        try:
            cb(peer_id)
        except Exception:
            pass

    def _send_loop(self, session: dict) -> None:
        """The session's ONLY writer: one thread per session draining the send
        queue in order. A write failure means the socket is dead — close it so
        the read loop unblocks immediately and the session tears down instead
        of lingering until the peer read timeout. Items still queued when the
        failure happens get their on_failed callback (undelivered messages go
        back to the outbox) in FIFO order."""
        q = session["send_queue"]
        try:
            while session["alive"]:
                try:
                    packet, on_sent, on_failed = q.get(timeout=self.SEND_POLL)
                except queue.Empty:
                    continue
                try:
                    session["wire"].send_packet(packet)
                except Exception:
                    self._safe_close(session["sock"])
                    self._run_send_cb(on_failed)
                    while True:
                        try:
                            _, _, pending_cb = q.get_nowait()
                        except queue.Empty:
                            break
                        self._run_send_cb(pending_cb)
                    break
                self._run_send_cb(on_sent)
        except Exception:
            pass
        finally:
            # Session closed/replaced between enqueue and write: don't strand
            # items in a queue nobody drains. Running on_failed restores chat
            # messages to the outbox as pending and fails file offers cleanly.
            if not session["alive"]:
                while True:
                    try:
                        _, _, pending_cb = q.get_nowait()
                    except queue.Empty:
                        break
                    self._run_send_cb(pending_cb)

    def _read_loop(self, session: dict) -> None:
        try:
            session["sock"].settimeout(self.READ_TIMEOUT)
            while session["alive"]:
                try:
                    packet = session["wire"].recv_packet()
                except WireException:
                    break
                if packet is None:
                    break
                peer_id = session["peer_id"]
                if packet.type in ("chat", "file_message") and packet.message is not None:
                    msg = packet.message
                    # identity + content validation, matching the host relay
                    # and the Android direct reader: only the linked member may
                    # speak as itself
                    if msg.sender_id != peer_id or not is_valid_content(msg.content):
                        logger.warning(
                            "drop %s on session %s: senderId=%r content_len=%d",
                            packet.type, peer_id, msg.sender_id, len(msg.content),
                        )
                        continue
                    with self._lock:
                        dup = any(
                            m.id == msg.id for m in self._messages.get(peer_id, [])
                        )
                    if dup:
                        # idempotent redelivery (e.g. the sender died between
                        # flushing its outbox and writing the pending=0 flag,
                        # then re-queued on restart): drop instead of
                        # duplicating the bubble
                        logger.info("drop duplicate message %s on session %s", msg.id, peer_id)
                        continue
                    self._append_message(peer_id, msg.marked_from_me(self._my_id))
                    if packet.type == "chat":
                        # receiving a plain chat IS reading it (the protocol
                        # defines the automatic receipt on delivery): ack so
                        # the sender flips its bubble to 已读
                        self._send_read_receipt(peer_id, msg.id)
                elif packet.type == "typing" and packet.active is not None:
                    # a member's typing indicator; only the session peer may
                    # speak for itself and the scope must name this chat
                    if (
                        packet.sender_id == peer_id
                        and packet.group_id == "direct:" + peer_id
                    ):
                        self._set_peer_typing(peer_id, bool(packet.active))
                elif packet.type == "read_receipt" and packet.up_to_id:
                    # the peer read up to up_to_id; only the session peer may
                    # report its own reading, and only for this chat's scope
                    if (
                        packet.reader_id == peer_id
                        and packet.group_id == "direct:" + peer_id
                    ):
                        self._apply_read_receipt(peer_id, packet.up_to_id)
                elif packet.type == "delete_message" and packet.message_id:
                    sender = packet.sender_id
                    if sender != peer_id:
                        continue  # forged senderId
                    with self._lock:
                        target = next(
                            (
                                m
                                for m in self._messages.get(peer_id, [])
                                if m.id == packet.message_id
                            ),
                            None,
                        )
                    if target is not None and target.sender_id == sender:
                        self._remove_message(peer_id, packet.message_id)
                elif packet.type == "edit_message" and packet.message_id and packet.new_content is not None:
                    # only the session peer may edit as itself, and only its
                    # own message (author check inside _edit_message). The
                    # listener persists the new text, so it must fire ONLY for
                    # an accepted edit — otherwise a peer could rewrite the
                    # stored copy of MY message while the in-memory list
                    # correctly refuses it (Android gates on the same result).
                    if packet.sender_id == peer_id and self._edit_message(
                        peer_id, packet.message_id, packet.new_content, peer_id
                    ):
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.direct_message_edited(
                                    peer_id, packet.message_id, packet.new_content
                                )
                            except Exception:
                                logger.exception("direct edit listener failed")
                elif packet.type == "reaction" and packet.message_id and packet.emoji:
                    if packet.sender_id == peer_id:
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.direct_reaction_changed(
                                    peer_id,
                                    packet.message_id,
                                    packet.emoji,
                                    bool(packet.active),
                                )
                            except Exception:
                                logger.exception("direct reaction listener failed")
                elif packet.type == "pin_message" and packet.message_id:
                    if packet.sender_id == peer_id:
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.direct_pin_changed(
                                    peer_id, packet.message_id, bool(packet.active)
                                )
                            except Exception:
                                logger.exception("direct pin listener failed")
                elif packet.type in CALL_PACKET_TYPES:
                    # 1:1 session: call signaling must involve THIS member and
                    # the packet's sender role must match the linked peer.
                    call = packet.call
                    if call is None:
                        continue
                    if call.caller_id != self._my_id and call.callee_id != self._my_id:
                        continue
                    if packet.type == "call_offer":
                        if call.caller_id != peer_id or call.callee_id != self._my_id:
                            continue
                    elif packet.type in ("call_answer", "call_reject", "call_failed"):
                        if call.callee_id != peer_id or call.caller_id != self._my_id:
                            continue
                    elif packet.type == "call_hangup":
                        if call.caller_id != peer_id and call.callee_id != peer_id:
                            continue
                    handler = self.on_call_signal
                    if handler is not None:
                        try:
                            handler(packet)
                        except Exception:
                            logger.exception("direct call signal handler failed")
                elif packet.type == "ping":
                    try:
                        session["wire"].send_packet(NetworkPacket(type="pong"))
                    except Exception:
                        pass
                # "pong": traffic only
        except Exception:
            pass
        finally:
            session["alive"] = False
            replaced = False
            with self._lock:
                # only when THIS session is still the registered one: a
                # reconnect may have replaced it, and the old loop's cleanup
                # must not mark the fresh session offline or end a call
                # riding it
                if self._sessions.get(session["peer_id"]) is session:
                    self._sessions.pop(session["peer_id"], None)
                else:
                    replaced = True
            self._safe_close(session["sock"])
            if not replaced:
                # a session that died can no longer be typing
                self._clear_peer_typing(session["peer_id"])
                self._emit_event(f"与 {session['peer_name']} 的直聊连接已断开")
                closed = self.on_session_closed
                if closed is not None:
                    try:
                        closed(session["peer_id"])
                    except Exception:
                        pass

    def _ping_loop(self, session: dict) -> None:
        while session["alive"]:
            time.sleep(self.PING_INTERVAL)
            if not session["alive"]:
                break
            try:
                session["wire"].send_packet(NetworkPacket(type="ping"))
            except Exception:
                break

    def _emit_event(self, text: str) -> None:
        cb = self.on_event
        if cb is not None:
            try:
                cb(text)
            except Exception:
                pass

    def _notify_contacts(self) -> None:
        listener = self._listener
        if listener is not None:
            try:
                listener.direct_contacts_changed()
            except Exception:
                pass

    def _notify_messages(self, peer_id: str) -> None:
        listener = self._listener
        if listener is not None:
            try:
                listener.direct_messages_changed(peer_id)
            except Exception:
                pass

    def _safe_close(self, sock) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _spawn(self, target, *args) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()


class GroupMeshListener:
    """Bridge from mesh worker threads back to the ViewModel."""

    def group_mesh_message(self, group_id: str, msgs) -> None:
        pass

    def group_mesh_links_changed(self, group_id: str) -> None:
        pass

    def group_mesh_delete(self, group_id: str, message_id: str, sender_id: str) -> None:
        pass

    def group_mesh_deleted_ids(self, group_id: str, deleted_ids) -> None:
        """history_reply carried tombstone convergence data: [deleted_ids]
        were deleted in the group while this member was away."""
        pass

    def group_mesh_typing(self, group_id: str, sender_id: str, active: bool) -> None:
        """A linked member's typing indicator changed (host-offline path)."""
        pass

    def group_mesh_admin(self, group_id: str, packet: NetworkPacket) -> None:
        """A group owner management packet (group_update / kick_member) arrived
        over a mesh link. The mesh layer already validated that senderId is the
        group's creator; the ViewModel applies the change (announcement/name,
        or the kicked member's teardown)."""
        pass

    def group_mesh_edit(self, group_id: str, message_id: str, new_content: str, sender_id: str, message_sig: Optional[str] = None) -> None:
        """A member edited its own message (edit_message over a mesh link;
        the mesh layer validated sender against the link and the edit
        signature). [message_sig] is the packet's senderSig (signature over
        the edited body's message transcript) for the mesh-copy mirror; None
        from the history-merge path (which already updated the mesh copy
        itself)."""
        pass

    def group_mesh_reaction(self, group_id: str, message_id: str, emoji: str, sender_id: str, active: bool) -> None:
        """A member toggled an emoji reaction over a mesh link."""
        pass

    def group_mesh_pin(self, group_id: str, message_id: str, sender_id: str, active: bool) -> None:
        """A member pinned/unpinned a message over a mesh link."""
        pass

    def group_mesh_read_receipt(self, group_id: str, reader_id: str, up_to_id: str) -> None:
        """A linked member reported reading the group up to [up_to_id] over a
        mesh link (host-offline path)."""
        pass

    def group_mesh_group_file_add(self, group_id: str, entry: GroupFileInfo) -> None:
        """A member shared a file into the share area over a mesh link (the
        mesh layer validated the sender against the link)."""
        pass

    def group_mesh_group_file_remove(
        self, group_id: str, file_id: str, sender_id: str
    ) -> bool:
        """A group_file_remove arrived over a mesh link. The implementation
        authorizes (uploader or owner) and returns whether it was applied."""
        return False

    def group_mesh_group_files(self, group_id: str, entries, removed_ids) -> None:
        """history_reply carried share-area convergence data over a mesh
        link: [entries] is the peer's index, [removed_ids] its removal
        tombstones."""
        pass


class GroupMeshManager:
    """Group mesh: direct member-to-member links inside a group, so members
    can keep chatting when the host is offline, and each member
    auto-backfills the messages they missed while away.

    A member links to every OTHER member over the shared listener
    (mesh_hello / mesh_ack, auto-accepted). Every link is secured with the
    group's password-bound handshake (mode "mesh"), so only members who know
    the group password may link and read the group's history. Group messages
    (chat + file offers) are broadcast over all links (plus the host relay
    when it is up; receivers dedup by message id); deletes ride the mesh too
    so they converge even when the host is unreachable. When a link is
    established, both sides push their stored history for the group (capped),
    so a member coming online learns what happened while they were away. The
    host itself is NOT meshed — it relays to everyone.

    Qt-free: unit-testable with real sockets.
    """

    HEARTBEAT_INTERVAL = 15.0
    HEARTBEAT_TIMEOUT = 45.0
    RETRY_INTERVAL = 10.0
    HISTORY_CAP = 500
    # Hard cap on tracked peers per group: mesh_announce carries
    # attacker-controlled peer records, and without a cap a flood of them
    # would grow the peer map (and the dial backlog behind it) without
    # bound. Real LAN groups stay far below this.
    MAX_PEERS_PER_GROUP = 64
    # A mesh dial gives up after this many consecutive failed attempts
    # (with linear backoff): an unreachable host:port from a forged
    # announce must not keep a dial thread retrying forever. Re-entering
    # the group or a fresh legitimate announce starts a new bounded cycle.
    MAX_DIAL_ATTEMPTS = 30
    # History is pushed in batches whose encrypted line stays safely under
    # MAX_LINE_LENGTH (the AES-GCM + Base64 expansion is ~1.4x) — one
    # 500-message packet would exceed the read cap and drop the link
    # (Android parity: HISTORY_CHUNK_BYTES = 36KB there).
    HISTORY_CHUNK_BYTES = 36 * 1024

    def __init__(self):
        self._lock = threading.RLock()
        self._listener: Optional[GroupMeshListener] = None
        self._groups: Dict[str, dict] = {}
        self._has_links: Dict[str, bool] = {}
        # Tombstone source for history pushes: callable(group_id) ->
        # [msgId, ...] (backed by the ViewModel's deleted_messages table).
        self.deleted_ids_provider = None
        # Group file share area (群文件) sources for history pushes:
        # callable(group_id) -> [GroupFileInfo, ...] (share index) and
        # callable(group_id) -> [fileId, ...] (removal tombstones). None
        # omits the fields.
        self.group_files_provider = None
        self.removed_file_ids_provider = None
        # Creator (group owner) id per group: callable(group_id) -> str. Owner
        # management packets (group_update / kick_member) are only accepted on
        # a link when their senderId is the creator; an unknown creator refuses
        # them (fail-closed).
        self.creator_id_provider = None

    def attach(self, listener: GroupMeshListener) -> None:
        self._listener = listener

    # ------------------------------------------------------------ lifecycle

    def enter_group(self, group_id: str, my_peer: Peer, peers, history, password: str = "") -> None:
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                state = {
                    "group_id": group_id,
                    "connected": True,
                    "password": password,
                    "my_peer": my_peer,
                    "peers": {},
                    "links": {},
                    "messages": sorted(history, key=lambda m: m.timestamp),
                    "dialing": set(),
                }
                self._groups[group_id] = state
            else:
                state["connected"] = True
                state["password"] = password
                state["my_peer"] = my_peer
                state["messages"] = sorted(history, key=lambda m: m.timestamp)
        for peer in peers:
            self.add_peer(group_id, peer)

    def update_local_port(self, group_id: str, port: int) -> None:
        """Update the advertised local port after a settings change. Existing
        links keep working; new mesh links and reconnects use the new port."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is not None and state.get("my_peer") is not None:
                state["my_peer"].port = port

    def leave_group(self, group_id: str) -> None:
        with self._lock:
            state = self._groups.pop(group_id, None)
        if state is None:
            return
        state["connected"] = False
        for link in list(state["links"].values()):
            link["alive"] = False
            self._safe_close(link["sock"])
        state["links"].clear()
        self._set_has_links(group_id, False)

    def shutdown(self) -> None:
        for gid in list(self._groups):
            self.leave_group(gid)

    def is_in_group(self, group_id: str) -> bool:
        with self._lock:
            return group_id in self._groups

    def password_for(self, group_id: str) -> Optional[str]:
        """The group's password, or None when this device is not in the group —
        consumed by the shared listener's mesh-handshake password lookup."""
        with self._lock:
            state = self._groups.get(group_id)
            return state["password"] if state is not None else None

    def sync_peers(self, group_id: str, peers) -> None:
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            mine = state["my_peer"].id if state["my_peer"] else None
            keep = {p.id for p in peers if p.id != mine}
            for pid in list(state["peers"]):
                if pid not in keep:
                    link = state["links"].pop(pid, None)
                    if link is not None:
                        link["alive"] = False
                        self._safe_close(link["sock"])
                    state["peers"].pop(pid, None)
        for peer in peers:
            self.add_peer(group_id, peer)

    def has_links(self, group_id: str) -> bool:
        with self._lock:
            state = self._groups.get(group_id)
            return bool(state and state["links"])

    def _set_has_links(self, group_id: str, value: bool) -> None:
        changed = False
        with self._lock:
            if self._has_links.get(group_id) != value:
                self._has_links[group_id] = value
                changed = True
        if changed:
            listener = self._listener
            if listener is not None:
                try:
                    listener.group_mesh_links_changed(group_id)
                except Exception:
                    pass

    # --------------------------------------------------------------- sending

    def broadcast(self, group_id: str, msg) -> None:
        """Broadcast a message to every mesh link (the host path is separate).
        The writes run on a worker thread — this is called from the UI thread
        when the user sends a message."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            links = list(state["links"].values())
            # dedupe by id: the same message often arrives twice (host relay
            # + mesh overlap) and must not double-fill the history (Android
            # noteMessage parity)
            if not any(m.id == msg.id for m in state["messages"]):
                # store a COPY: the caller keeps the same object in the relay
                # view (p2p.messages), and update_mesh_message mutates its
                # target in place — sharing one object would let a mesh-side
                # edit signature overwrite the relay copy's fresh message
                # signature (LESSONS 2026-09-21 #4)
                state["messages"].append(copy.deepcopy(msg))
            state["messages"].sort(key=lambda m: m.timestamp)
            if len(state["messages"]) > self.HISTORY_CAP:
                del state["messages"][: len(state["messages"]) - self.HISTORY_CAP]
        if not links:
            return
        packet = NetworkPacket(type="mesh_chat", group_id=group_id, message=msg)
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_admin(self, group_id: str, packet: NetworkPacket) -> None:
        """Relay an owner management packet over every mesh link. Called by a
        member that received the packet on the host relay (set as the owning
        P2PManager's admin_rebroadcast hook), so members whose own relay is down
        still see the owner's command. Receivers validate senderId against the
        creator and never forward again (the mesh is a complete graph)."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            links = list(state["links"].values())
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_delete(self, group_id: str, message_id: str) -> None:
        """Tell every linked member that a message was deleted (host-offline
        path) so deletes converge even when the host relay is unreachable.
        The sender also drops the message from its own mesh history right
        away, so a later backfill cannot resurrect it."""
        had = False
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            my_id = state["my_peer"].id if state["my_peer"] else ""
            links = list(state["links"].values())
            had = any(m.id == message_id for m in state["messages"])
            state["messages"] = [m for m in state["messages"] if m.id != message_id]
        if had:
            listener = self._listener
            if listener is not None:
                try:
                    listener.group_mesh_delete(group_id, message_id, my_id)
                except Exception:
                    pass
        if not links:
            return
        packet = NetworkPacket(
            type="delete_message", message_id=message_id, sender_id=my_id
        )
        groupauth.sign_packet(packet, groupauth.delete_parts(group_id, my_id, message_id))
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_typing(self, group_id: str, sender_id: str, active: bool) -> None:
        """Tell every linked member that [sender_id] started/stopped typing
        (host-offline path). Advisory: no history/state is touched, and a
        member with no link simply misses the indicator."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            links = list(state["links"].values())
        if not links:
            return
        packet = NetworkPacket(
            type="typing",
            group_id=group_id,
            sender_id=sender_id,
            active=bool(active),
        )
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_edit(self, group_id: str, message_id: str, new_content: str) -> None:
        """Tell every linked member that [messageId]'s author replaced its
        content (host-offline path). The packet signature covers the edited
        body's message transcript: receivers verify it against their copies
        and store it, so this member's later history pushes carry the new
        text AND a signature that passes verify_message (LESSONS
        2026-09-21 #1). Unsigned (identity missing) edits are not broadcast."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            my_id = state["my_peer"].id if state["my_peer"] else ""
            links = list(state["links"].values())
            target = next(
                (m for m in state["messages"] if m.id == message_id), None
            )
            if target is None or target.sender_id != my_id:
                return
            pub, sig = groupauth.sign_parts(
                groupauth.message_fields_parts(
                    group_id, my_id, message_id, target.timestamp, new_content,
                )
            )
            if not pub:
                return
            target.content = new_content
            target.edited = True
            target.sender_pub_id = pub
            target.sender_sig = sig
        packet = NetworkPacket(
            type="edit_message",
            group_id=group_id,
            message_id=message_id,
            sender_id=my_id,
            new_content=new_content,
            sender_pub_id=pub,
            sender_sig=sig,
        )
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_reaction(
        self, group_id: str, message_id: str, emoji: str, active: bool
    ) -> None:
        """Toggle an emoji reaction over every mesh link. Advisory: members
        with no link at send time miss it (no offline queue)."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            my_id = state["my_peer"].id if state["my_peer"] else ""
            links = list(state["links"].values())
        packet = NetworkPacket(
            type="reaction",
            group_id=group_id,
            message_id=message_id,
            sender_id=my_id,
            emoji=emoji,
            active=bool(active),
        )
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_pin(self, group_id: str, message_id: str, active: bool) -> None:
        """Pin/unpin a message over every mesh link (host-offline path)."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            my_id = state["my_peer"].id if state["my_peer"] else ""
            links = list(state["links"].values())
        packet = NetworkPacket(
            type="pin_message",
            group_id=group_id,
            message_id=message_id,
            sender_id=my_id,
            active=bool(active),
        )
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_read_receipt(self, group_id: str, up_to_id: str) -> None:
        """Tell every linked member we have read the group up to [up_to_id]
        (host-offline path)."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            my_id = state["my_peer"].id if state["my_peer"] else ""
            links = list(state["links"].values())
        packet = NetworkPacket(
            type="read_receipt",
            group_id=group_id,
            up_to_id=up_to_id,
            reader_id=my_id,
        )
        for link in links:
            self._spawn(self._link_write, link, packet)

    def update_mesh_message(
        self,
        group_id: str,
        message_id: str,
        new_content: str,
        sender_id: str,
        sender_pub_id: Optional[str] = None,
        sender_sig: Optional[str] = None,
    ) -> bool:
        """Apply a verified edit to this member's mesh history copy so a later
        history push carries the new text. [sender_pub_id]/[sender_sig] are
        the edit packet's author fields: the signature must verify as the
        EDITED body's message transcript against this copy's identity fields
        (the packet key, or the key already stored on the copy when the
        caller did not carry it) — on success it is stored as the copy's
        signature, so pushed history passes verify_message and the edit
        converges to members that were offline (LESSONS 2026-09-21 #1).
        Strict: a missing or invalid signature rejects the edit (an unsigned
        rewrite must never enter history)."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return False
            target = next(
                (m for m in state["messages"] if m.id == message_id), None
            )
            if target is None or target.sender_id != sender_id:
                return False
            pub = sender_pub_id or target.sender_pub_id
            if not pub or not groupauth.verify_message_fields(
                group_id,
                sender_id,
                message_id,
                target.timestamp,
                new_content,
                pub,
                sender_sig,
            ):
                return False
            target.content = new_content
            target.edited = True
            target.sender_pub_id = pub
            target.sender_sig = sender_sig
        return True

    def apply_deleted_ids(self, group_id: str, deleted_ids) -> None:
        """Convergence data received with a history push: drop still-present
        copies from this member's mesh history and let the listener record
        the tombstones. Never rebroadcasts: convergence is not a new delete
        event (Android parity). Sanitized/capped first (one packet must not
        force unbounded work)."""
        ids = sanitize_deleted_ids(deleted_ids)
        if not ids:
            return
        id_set = set(ids)
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            state["messages"] = [m for m in state["messages"] if m.id not in id_set]
        listener = self._listener
        if listener is not None:
            try:
                listener.group_mesh_deleted_ids(group_id, ids)
            except Exception:
                pass

    def note_message(self, group_id: str, msg) -> None:
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            if any(m.id == msg.id for m in state["messages"]):
                return
            state["messages"].append(msg)
            state["messages"].sort(key=lambda m: m.timestamp)
            if len(state["messages"]) > self.HISTORY_CAP:
                del state["messages"][: len(state["messages"]) - self.HISTORY_CAP]

    def announce_peer(self, group_id: str, peer: Peer) -> None:
        """Tell every linked member that [peer] joined the group, so each one
        links up with it (used when a member sponsors a join)."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            links = list(state["links"].values())
        self.add_peer(group_id, peer)
        if not links:
            return
        packet = NetworkPacket(type="mesh_announce", group_id=group_id, peer=peer)
        for link in links:
            self._spawn(self._link_write, link, packet)

    @staticmethod
    def _link_write(link: dict, packet: NetworkPacket) -> None:
        try:
            link["wire"].send_packet(packet)
        except Exception:
            pass

    def _send_history(self, wire: Wire, group_id: str, history) -> None:
        """Push history in size-capped batches (see HISTORY_CHUNK_BYTES): the
        receiver reads with a bounded line reader, so each packet must stay
        under the cap or the link would be dropped. Receivers merge each batch
        independently and dedup by message id (Android parity). After the
        batches a dedicated tombstone history_reply carries the group's delete
        tombstones (deletedIds) — sent even when the history is empty, since
        everything the peer missed may have been deleted while it was away —
        so an offline member converges instead of resurrecting the message.
        Tombstones travel outside the batch size budget, which does not
        account for them (Android parity: sendHistory/sendDeletedIds).

        The group file share area (群文件) rides the same push: index
        batches (byte-budgeted like history), then the delete tombstones +
        share-area removal tombstones packet LAST — the receiver applies
        removals before/after entries idempotently, but sending removals
        last mirrors "history then tombstones"."""
        batch = []
        estimated = 0

        def flush() -> None:
            nonlocal batch, estimated
            if not batch:
                return
            packet = NetworkPacket(
                type="history_reply", group_id=group_id, messages=batch
            )
            try:
                wire.send_packet(packet)
            except Exception:
                pass
            batch = []
            estimated = 0

        for msg in history:
            # Upper bound per message: UTF-8 worst case (4 bytes/char), JSON
            # escaping, the packet envelope, and the AES-GCM + Base64
            # expansion of the encrypted line — stays under the line cap for
            # any realistic content.
            size = len(msg.content) * 4 + 1024
            if estimated > 0 and estimated + size > self.HISTORY_CHUNK_BYTES:
                flush()
            batch.append(msg)
            estimated += size
        flush()
        self._send_group_files(wire, group_id)
        deleted_ids = []
        if self.deleted_ids_provider is not None:
            try:
                deleted_ids = sanitize_deleted_ids(self.deleted_ids_provider(group_id))
            except Exception:
                deleted_ids = []
        removed_ids = self._removed_file_ids(group_id)
        if deleted_ids or removed_ids:
            try:
                wire.send_packet(
                    NetworkPacket(
                        type="history_reply",
                        group_id=group_id,
                        deleted_ids=deleted_ids or None,
                        removed_ids=removed_ids or None,
                    )
                )
            except Exception:
                pass

    def _removed_file_ids(self, group_id: str) -> list:
        """The group's removed-share-file tombstone ids (capped), or [] when
        no provider is wired."""
        provider = self.removed_file_ids_provider
        if provider is None:
            return []
        try:
            return sanitize_id_list(
                provider(group_id), MAX_GROUP_FILE_REMOVED
            )
        except Exception:
            return []

    def _send_group_files(self, wire: Wire, group_id: str) -> None:
        """Push the share-area index in byte-budgeted batches (one entry is
        far smaller than one message, but 500 entries still exceed a single
        line, so the same chunking rule as history applies). Receivers merge
        each batch independently and dedup by fileId."""
        provider = self.group_files_provider
        if provider is None:
            return
        try:
            entries = sanitize_group_files(provider(group_id))
        except Exception:
            return
        batch = []
        estimated = 0

        def flush() -> None:
            nonlocal batch, estimated
            if not batch:
                return
            try:
                wire.send_packet(
                    NetworkPacket(
                        type="history_reply", group_id=group_id, group_files=batch
                    )
                )
            except Exception:
                pass
            batch = []
            estimated = 0

        for entry in entries:
            size = len(entry.name) * 4 + len(entry.sender_name) * 4 + 512
            if estimated > 0 and estimated + size > GROUP_FILE_PUSH_BUDGET_BYTES:
                flush()
            batch.append(entry)
            estimated += size
        flush()

    # ------------------------------------------------------------- listeners

    def handle_mesh_hello(self, sock, wire: Wire, packet: NetworkPacket) -> None:
        group_id = packet.group_id
        peer = packet.peer
        with self._lock:
            state = self._groups.get(group_id) if group_id else None
        if state is None or peer is None:
            self._safe_close(sock)
            return
        try:
            wire.send_packet(
                NetworkPacket(type="mesh_ack", group_id=group_id, peer=state["my_peer"])
            )
        except Exception:
            self._safe_close(sock)
            return
        self._register_link(state, peer, sock, wire)

    # ------------------------------------------------------------- internals

    def add_peer(self, group_id: str, peer: Peer) -> None:
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            # endpoint sanity: never dial an empty host or an out-of-range
            # port — both come straight off the wire inside mesh_announce
            if not peer.ip_address or not 1 <= peer.port <= 65535:
                return
            mine = state["my_peer"].id if state["my_peer"] else ""
            if peer.id == mine or not peer.id:
                return
            # per-group peer cap (see MAX_PEERS_PER_GROUP): once full, only
            # already-known peers are refreshed, never new ones
            if (
                peer.id not in state["peers"]
                and len(state["peers"]) >= self.MAX_PEERS_PER_GROUP
            ):
                return
            state["peers"][peer.id] = peer
            # deterministic linking: only the smaller id dials
            if mine >= peer.id:
                return
            if peer.id in state["links"]:
                return
            if peer.id in state["dialing"]:
                return
            state["dialing"].add(peer.id)
        self._spawn(self._dial_loop, group_id, peer)

    def _dial_loop(self, group_id: str, peer: Peer) -> None:
        try:
            self._dial_with_retry(group_id, peer)
        finally:
            with self._lock:
                state = self._groups.get(group_id)
                if state is not None:
                    state["dialing"].discard(peer.id)

    def _dial_sleep(self, group_id: str, seconds: float) -> bool:
        """Sleep in small slices while the group stays live; returns False as
        soon as the group is left/stopped, so a dial thread exits promptly
        (leave_group flips the state alive flag) instead of blocking inside
        one long time.sleep."""
        deadline = time.monotonic() + seconds
        while True:
            with self._lock:
                state = self._groups.get(group_id)
                if state is None or not state["connected"]:
                    return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.5, remaining))

    def _dial_with_retry(self, group_id: str, peer: Peer) -> None:
        # Bounded retry (see MAX_DIAL_ATTEMPTS): a peer address learned from
        # a mesh_announce may be garbage or unreachable forever, and the
        # loop must not dial it for the lifetime of the process. A link that
        # DID come up resets the counter: a flaky-but-real member is still
        # retried patiently across drops.
        failures = 0
        while True:
            with self._lock:
                state = self._groups.get(group_id)
                if state is None or not state["connected"]:
                    return
                existing = state["links"].get(peer.id)
                if existing is not None and existing["alive"]:
                    return
            link = self._try_connect(group_id, peer)
            if link is None:
                failures += 1
                if failures >= self.MAX_DIAL_ATTEMPTS:
                    logger.warning(
                        "mesh dial to %s (%s:%d) gave up after %d failed attempts",
                        peer.id, peer.ip_address, peer.port, failures,
                    )
                    return
                # linear backoff up to a minute between attempts
                if not self._dial_sleep(
                    group_id, min(self.RETRY_INTERVAL * failures, 60.0)
                ):
                    return
                continue
            with self._lock:
                state = self._groups.get(group_id)
                if state is None or not state["connected"]:
                    link["alive"] = False
                    self._safe_close(link["sock"])
                    return
                # Atomically install the new link: only a LIVE existing link
                # makes us drop ours — a dead/stale entry must be replaced,
                # not block the fresh connection (otherwise a member could
                # never re-link after its old link died).
                existing = state["links"].get(peer.id)
                if existing is not None and existing["alive"]:
                    link["alive"] = False
                    self._safe_close(link["sock"])
                    return
                state["links"][peer.id] = link
                self._set_has_links(group_id, True)
                history = list(state["messages"])[-self.HISTORY_CAP:]
            failures = 0
            # push our history so the peer backfills what it missed (both
            # sides push; receivers dedup by id)
            if history:
                self._send_history(link["wire"], group_id, history)
            self._read_loop(group_id, link)
            with self._lock:
                state = self._groups.get(group_id)
                if state is not None:
                    if state["links"].get(peer.id) is link:
                        state["links"].pop(peer.id, None)
                    self._set_has_links(group_id, bool(state["links"]))
            if not self._dial_sleep(group_id, self.RETRY_INTERVAL):
                return

    def _try_connect(self, group_id: str, peer: Peer) -> Optional[dict]:
        sock = None
        try:
            sock = socket.create_connection((peer.ip_address, peer.port), timeout=8)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(15)
            with self._lock:
                state = self._groups.get(group_id)
                my_peer = state["my_peer"] if state is not None else None
                password = state["password"] if state is not None else ""
            if my_peer is None:
                self._safe_close(sock)
                sock = None
                return None
            # password-bound secured handshake: mesh traffic (chat + history +
            # deletes) is encrypted, and both sides prove group membership.
            # The link stays line-based, so a buffered reader is fine.
            reader = sock.makefile("r", encoding="utf-8", newline="\n")
            wire = make_wire(sock, reader)
            Handshake.initiate(wire, Protocol.MODE_MESH, group_id, password)
            wire.send_packet(
                NetworkPacket(type="mesh_hello", group_id=group_id, peer=my_peer)
            )
            ack = wire.recv_packet()
            if ack is None or ack.type != "mesh_ack" or ack.peer is None:
                self._safe_close(sock)
                sock = None
                return None
            return {
                "peer_id": peer.id,
                "peer_name": ack.peer.name,
                "sock": sock,
                "wire": wire,
                "alive": True,
            }
        except Exception:
            if sock is not None:
                self._safe_close(sock)
            return None

    def _register_link(self, state: dict, peer: Peer, sock, wire: Wire) -> None:
        link = {
            "peer_id": peer.id,
            "peer_name": peer.name,
            "sock": sock,
            "wire": wire,
            "alive": True,
        }
        with self._lock:
            state["peers"][peer.id] = peer
            existing = state["links"].get(peer.id)
            if existing is not None and existing["alive"]:
                link["alive"] = False
                self._safe_close(sock)
                return
            if existing is not None:
                existing["alive"] = False
                self._safe_close(existing["sock"])
            state["links"][peer.id] = link
            self._set_has_links(state["group_id"], True)
            history = list(state["messages"])[-self.HISTORY_CAP:]
        if history:
            self._send_history(wire, state["group_id"], history)
        self._spawn(self._read_loop, state["group_id"], link)
        self._spawn(self._ping_loop, link)

    def _read_loop(self, group_id: str, link: dict) -> None:
        try:
            link["sock"].settimeout(self.HEARTBEAT_TIMEOUT)
            while link["alive"]:
                try:
                    packet = link["wire"].recv_packet()
                except WireException:
                    break
                if packet is None:
                    break
                if packet.type in ("mesh_chat", "file_message") and packet.message is not None:
                    msg = packet.message
                    # only the linked member's own messages travel this path;
                    # a file offer is a message carrying FileInfo and gets the
                    # same sender validation as plain chat
                    if msg.sender_id != link["peer_id"] or not is_valid_content(msg.content):
                        logger.warning(
                            "drop %s on link %s: senderId=%r content_len=%d",
                            packet.type, link["peer_id"], msg.sender_id, len(msg.content),
                        )
                        continue
                    # author identity gate (TOFU) BEFORE the message is merged
                    # or any listener fires
                    if not groupauth.verify_message(group_id, msg):
                        continue
                    self._handle_incoming(group_id, [msg])
                elif packet.type == "delete_message":
                    if packet.message_id and packet.sender_id:
                        self._handle_delete_incoming(
                            group_id,
                            link,
                            packet.message_id,
                            packet.sender_id,
                            packet.sender_pub_id,
                            packet.sender_sig,
                        )
                elif packet.type == "edit_message" and packet.message_id and packet.new_content is not None:
                    self._handle_edit_incoming(group_id, link, packet)
                elif packet.type == "reaction" and packet.message_id and packet.emoji:
                    # only the linked member may react as itself
                    if packet.sender_id == link["peer_id"]:
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.group_mesh_reaction(
                                    group_id,
                                    packet.message_id,
                                    packet.emoji,
                                    packet.sender_id,
                                    bool(packet.active),
                                )
                            except Exception:
                                pass
                elif packet.type == "pin_message" and packet.message_id:
                    if packet.sender_id == link["peer_id"]:
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.group_mesh_pin(
                                    group_id,
                                    packet.message_id,
                                    packet.sender_id,
                                    bool(packet.active),
                                )
                            except Exception:
                                pass
                elif (
                    packet.type == "read_receipt"
                    and packet.up_to_id
                    and packet.reader_id == link["peer_id"]
                ):
                    listener = self._listener
                    if listener is not None:
                        try:
                            listener.group_mesh_read_receipt(
                                group_id, packet.reader_id, packet.up_to_id
                            )
                        except Exception:
                            pass
                elif (
                    packet.type == "typing"
                    and packet.active is not None
                    and packet.sender_id == link["peer_id"]
                ):
                    # only the linked member may claim ITS OWN typing state
                    listener = self._listener
                    if listener is not None:
                        try:
                            listener.group_mesh_typing(
                                group_id, packet.sender_id, bool(packet.active)
                            )
                        except Exception:
                            pass
                elif packet.type == "history_reply":
                    # History legitimately contains messages from many senders,
                    # but each message must still be well-formed and bounded —
                    # and each author's device signature must verify (TOFU)
                    # before it may enter history or fire a listener. Strict:
                    # an entry whose signature does not verify as a message
                    # transcript is dropped, even from the author's own link
                    # (the project is unreleased — no legacy tolerance).
                    valid_history = [
                        m for m in (packet.messages or [])
                        if m.sender_id
                        and is_valid_content(m.content)
                        and groupauth.verify_message(group_id, m)
                    ]
                    edits = []
                    with self._lock:
                        state = self._groups.get(group_id)
                        # A batch entry reusing a locally-known id with
                        # DIFFERENT content is a forged overwrite UNLESS the
                        # author itself pushes its own message over its OWN
                        # link: that is exactly how an edit converges to a
                        # member that was offline. Everyone else (including
                        # the author id claimed over another member's link)
                        # must never rewrite stored history.
                        # TRUST NOTE: the mesh handshake is password-only and
                        # the link's peer id is self-claimed (handle_mesh_hello
                        # registers it without an identity proof), so a group
                        # member CAN claim another author's id on its own link
                        # and pass this check. The rewrite trust boundary is
                        # therefore "holders of the group password", exactly
                        # like the delete tombstones (see README security
                        # notes); binding link identity (DeviceIdentity-signed
                        # mesh hello) would be needed to make it author-true.
                        local_rows = (
                            {m.id: m for m in state["messages"]}
                            if state is not None
                            else None
                        )
                        if local_rows is not None:
                            still_new = []
                            for m in valid_history:
                                local = local_rows.get(m.id)
                                if local is None:
                                    still_new.append(m)
                                elif (
                                    m.sender_id == link["peer_id"]
                                    and local.sender_id == m.sender_id
                                    and local.content != m.content
                                ):
                                    local.content = m.content
                                    local.edited = True
                                    # the entry just passed verify_message:
                                    # adopt its signature so this member's
                                    # own history pushes stay verify-valid
                                    if m.sender_pub_id and m.sender_sig:
                                        local.sender_pub_id = m.sender_pub_id
                                        local.sender_sig = m.sender_sig
                                    edits.append(m)
                            valid_history = still_new
                    if valid_history:
                        self._handle_incoming(group_id, valid_history)
                    for m in edits:
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.group_mesh_edit(
                                    group_id, m.id, m.content, m.sender_id
                                )
                            except Exception:
                                pass
                    if packet.deleted_ids:
                        # tombstone convergence riding the history push (no
                        # rebroadcast — see apply_deleted_ids)
                        self.apply_deleted_ids(group_id, packet.deleted_ids)
                    if packet.removed_ids or packet.group_files:
                        # share-area convergence riding the history push:
                        # the store applies removal tombstones first and
                        # skips entries they cover (no rebroadcast)
                        removed = sanitize_id_list(
                            packet.removed_ids, MAX_GROUP_FILE_REMOVED
                        )
                        entries = sanitize_group_files(packet.group_files)
                        if removed or entries:
                            listener = self._listener
                            if listener is not None:
                                try:
                                    listener.group_mesh_group_files(
                                        group_id, entries, removed
                                    )
                                except Exception:
                                    pass
                elif packet.type == "mesh_announce" and packet.peer is not None:
                    self.add_peer(group_id, packet.peer)
                elif packet.type == "group_file_add":
                    # share area over a mesh link: only the linked member may
                    # share as itself, and the entry must name that sender
                    entry = None
                    if (
                        packet.sender_id == link["peer_id"]
                        and packet.group_id == group_id
                    ):
                        entry = P2PManager._group_file_entry_from_packet(packet)
                    if entry is None or entry.sender_id != packet.sender_id:
                        logger.warning(
                            "drop group_file_add on link %s: senderId=%r",
                            link["peer_id"], packet.sender_id,
                        )
                    else:
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.group_mesh_group_file_add(group_id, entry)
                            except Exception:
                                pass
                elif packet.type == "group_file_remove":
                    if (
                        packet.sender_id == link["peer_id"]
                        and packet.file_id
                        and packet.group_id == group_id
                    ):
                        listener = self._listener
                        if listener is not None:
                            try:
                                listener.group_mesh_group_file_remove(
                                    group_id, packet.file_id, packet.sender_id
                                )
                            except Exception:
                                pass
                elif packet.type in ("group_update", "kick_member"):
                    self._handle_admin_incoming(group_id, link, packet)
                elif packet.type == "ping":
                    try:
                        link["wire"].send_packet(NetworkPacket(type="pong"))
                    except Exception:
                        pass
                # "pong": traffic only
        except Exception:
            pass
        finally:
            link["alive"] = False
            with self._lock:
                state = self._groups.get(group_id)
                if state is not None:
                    if state["links"].get(link["peer_id"]) is link:
                        state["links"].pop(link["peer_id"], None)
                    self._set_has_links(group_id, bool(state["links"]))
            self._safe_close(link["sock"])

    def _handle_admin_incoming(self, group_id: str, link: dict, packet: NetworkPacket) -> None:
        """Owner management packet over a mesh link: only the group creator may
        originate it (senderId must match the persisted creator id), otherwise
        the link is dropped. Applied packets are forwarded to the ViewModel;
        they are never re-forwarded (the mesh is a complete graph)."""
        creator = ""
        provider = self.creator_id_provider
        if provider is not None:
            try:
                creator = str(provider(group_id) or "")
            except Exception:
                creator = ""
        if not creator or not packet.sender_id or packet.sender_id != creator:
            logger.warning(
                "reject %s on mesh link %s: senderId=%r is not the creator %r",
                packet.type,
                link.get("peer_id"),
                packet.sender_id,
                creator,
            )
            link["alive"] = False
            self._safe_close(link["sock"])
            return
        # owner identity binding (TOFU): a signed owner packet must verify
        # against the creatorId's remembered key; unsigned stays legacy
        if packet.type == "group_update":
            verified = groupauth.verify_group_update(
                group_id,
                creator,
                packet.group_name or "",
                packet.announcement or "",
                packet.sender_pub_id,
                packet.sender_sig,
            )
        else:
            verified = groupauth.verify_kick(
                group_id,
                creator,
                packet.target_id or "",
                packet.sender_pub_id,
                packet.sender_sig,
            )
        if not verified:
            link["alive"] = False
            self._safe_close(link["sock"])
            return
        if packet.type == "kick_member":
            target = packet.target_id
            if not target:
                return
            mine = None
            with self._lock:
                state = self._groups.get(group_id)
                if state is not None and state.get("my_peer") is not None:
                    mine = state["my_peer"].id
            if target != mine:
                with self._lock:
                    state = self._groups.get(group_id)
                    if state is not None:
                        state["peers"].pop(target, None)
                        dead = state["links"].pop(target, None)
                        if dead is not None:
                            dead["alive"] = False
                            self._safe_close(dead["sock"])
                self._set_has_links(group_id, self.has_links(group_id))
        listener = self._listener
        if listener is not None:
            try:
                listener.group_mesh_admin(group_id, packet)
            except Exception:
                pass

    def _handle_edit_incoming(self, group_id: str, link: dict, packet: NetworkPacket) -> None:
        """Apply a mesh-received edit locally: update this member's mesh
        history copy and relay to the ViewModel. Only the linked member may
        edit as itself, only its own message (stricter than the mesh
        delete rule: content rewrites demand the author on the authoring
        link), and the author's device signature must verify (TOFU).
        No forwarding: the sender's broadcast already reached every
        link of the complete graph.
        TRUST NOTE: for UNSIGNED packets the link peer id is self-claimed
        (password-only mesh handshake), so like deletes this rule trusts
        holders of the group password; signed packets are author-true."""
        if packet.sender_id != link["peer_id"]:
            logger.warning(
                "reject mesh edit %s: claimed senderId=%r on link %s",
                packet.message_id, packet.sender_id, link["peer_id"],
            )
            return
        if not self.update_mesh_message(
            group_id,
            packet.message_id,
            packet.new_content,
            packet.sender_id,
            sender_pub_id=packet.sender_pub_id,
            sender_sig=packet.sender_sig,
        ):
            logger.warning(
                "reject mesh edit %s: unsigned, not verifiable, message not "
                "found or not authored by %r",
                packet.message_id, packet.sender_id,
            )
            return
        listener = self._listener
        if listener is not None:
            try:
                listener.group_mesh_edit(
                    group_id, packet.message_id, packet.new_content, packet.sender_id,
                    message_sig=packet.sender_sig,
                )
            except Exception:
                pass

    def _handle_delete_incoming(
        self,
        group_id: str,
        link: dict,
        message_id: str,
        sender_id: str,
        sender_pub_id=None,
        sender_sig=None,
    ) -> None:
        """Apply a mesh-received delete locally: remove the message from this
        group's mesh state and relay it to the ViewModel. Only the original
        sender may delete (same authorization as the host relay), the author's
        device signature must verify (TOFU), and a duplicate delete for an
        already removed message is ignored. No forwarding: the mesh links
        every member pair directly (the sender's broadcast already reaches
        everyone), and relaying would only create a delete storm through the
        complete graph."""
        target = None
        with self._lock:
            state = self._groups.get(group_id)
            if state is not None:
                target = next(
                    (m for m in state["messages"] if m.id == message_id), None
                )
        if target is None:
            return
        if target.sender_id != sender_id:
            logger.warning(
                "reject mesh delete %s: message senderId=%r != claimed %r",
                message_id, target.sender_id, sender_id,
            )
            return
        if not groupauth.verify_delete(
            group_id, sender_id, message_id, sender_pub_id, sender_sig
        ):
            return
        with self._lock:
            if state is not None:
                state["messages"] = [m for m in state["messages"] if m.id != message_id]
        listener = self._listener
        if listener is not None:
            try:
                listener.group_mesh_delete(group_id, message_id, sender_id)
            except Exception:
                pass

    def _handle_incoming(self, group_id: str, incoming) -> None:
        """Merge incoming messages (a whole history batch at once) into the
        group state and forward the new ones to the ViewModel in ONE call, so
        the UI and persistence update once instead of once per message."""
        new_ones = []
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            mine = state["my_peer"].id if state["my_peer"] else ""
            ids = {m.id for m in state["messages"]}
            new_ones = [
                m.marked_from_me(mine) for m in incoming if m.id not in ids
            ]
            if new_ones:
                state["messages"].extend(new_ones)
                state["messages"].sort(key=lambda m: m.timestamp)
                if len(state["messages"]) > self.HISTORY_CAP:
                    del state["messages"][: len(state["messages"]) - self.HISTORY_CAP]
        if not new_ones:
            return
        listener = self._listener
        if listener is not None:
            try:
                listener.group_mesh_message(group_id, new_ones)
            except Exception:
                pass

    def _ping_loop(self, link: dict) -> None:
        while link["alive"]:
            time.sleep(self.HEARTBEAT_INTERVAL)
            if not link["alive"]:
                break
            try:
                link["wire"].send_packet(NetworkPacket(type="ping"))
            except Exception:
                break

    def _safe_close(self, sock) -> None:
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _spawn(self, target, *args) -> None:
        threading.Thread(target=target, args=args, daemon=True).start()
