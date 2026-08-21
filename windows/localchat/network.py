import logging
import os
import queue
import socket
import struct
import threading
import time
import uuid
from typing import Dict, Optional

from .crypto import (
    KEY_LEN,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    from_b64,
    random_bytes,
    to_b64,
)
from .hardware import get_hardware_id, get_local_ip_address
from .models import (
    MAX_LINE_LENGTH,
    TCP_PORT,
    ChatMessage,
    FileInfo,
    GroupInfo,
    NetworkPacket,
    Peer,
    is_valid_content,
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


def numeric_group_id_of(group_name: str, fingerprint: str) -> str:
    """Stable 8-digit numeric id for a group: FNV-1a hash of the machine
    fingerprint + group name (same algorithm as the Android side), so it is
    machine-bound yet distinct per group. Used as the join identifier — members
    type this instead of the group name."""
    hash_ = 0x811C9DC5
    for ch in f"{group_name}\u0000{fingerprint}":
        hash_ ^= ord(ch)
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


def _serve_file_download(
    sock: socket.socket, file_id: str, path: str, file_size: int, file_key: bytes
) -> None:
    """Serve one file-download connection. Handshake (Android parity, see
    FileTransfer.kt):

        receiver -> "file_download" {fileId}                          (plaintext)
        sender   -> ENCRYPTED LINE: AES-GCM(fileKey, file_meta JSON)
        sender   -> [4B ctLen][12B nonce][AES-GCM chunk]... [4B zero EOF]

    The per-file key travels only inside the (itself encrypted) chat message
    that offered the file, so a passive sniffer sees ciphertext for the meta
    line AND the byte stream, and tampering anywhere trips the GCM tag and
    aborts the download.
    """
    try:
        sock.settimeout(30)
        reader = sock.makefile("r", encoding="utf-8", newline="\n")
        handshake = _read_line_bounded(reader)
        if handshake is None:
            return
        try:
            req = NetworkPacket.from_json(handshake)
        except Exception:
            return
        if req.type != "file_download" or req.file_id != file_id:
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


def _download_file_offer(file_info: FileInfo, target_path: str) -> tuple:
    """Download a file offered via [file_info] to [target_path]. Blocks the
    calling thread. Returns (ok: bool, message: str). The meta line and every
    chunk are decrypted with the per-file key from the (encrypted) offer; any
    tampering or key mismatch aborts."""
    if file_info.file_size > MAX_DOWNLOAD_BYTES:
        return False, f"文件过大（超过 {MAX_DOWNLOAD_BYTES // 1024 // 1024} MB 限制）"
    try:
        file_key = from_b64(file_info.file_key) if file_info.file_key else None
    except Exception:
        file_key = None
    if file_key is None or len(file_key) != KEY_LEN:
        return False, "文件密钥缺失或无效"
    try:
        sock = socket.create_connection(
            (file_info.download_host, file_info.download_port), timeout=10
        )
    except OSError as e:
        return False, f"连接失败: {e}"
    tmp_path = target_path + ".part"
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(10)
        _send_line_simple(
            sock, NetworkPacket(type="file_download", file_id=file_info.file_id).to_json()
        )
        # read the meta line from the raw socket, byte by byte: a buffered
        # wrapper would read-ahead and swallow the file frames that the
        # sender streams immediately after the meta line
        line = _read_raw_line(sock)
        if line is None:
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
        sock.settimeout(120)
        received = 0
        eof_marker = False
        with open(tmp_path, "wb") as f:
            while not eof_marker:
                header = _recv_exact(sock, 4)
                if header is None:
                    return False, "文件传输中断"
                frame_len = int.from_bytes(header, "big")
                if frame_len == 0:
                    eof_marker = True
                    break
                if frame_len < GCM_MIN_FRAME or frame_len > MAX_CHUNK_WIRE:
                    return False, "文件数据损坏"
                frame = _recv_exact(sock, frame_len)
                if frame is None:
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
        if expected > 0 and received != expected:
            return False, f"文件不完整（{received}/{expected} 字节）"
        os.replace(tmp_path, target_path)
        return True, ""
    except Exception as e:
        return False, f"下载失败: {e}"
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
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

    def __init__(self, port: int):
        self.port = port
        self._groups: Dict[str, "P2PManager"] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._server_socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self.error: Optional[str] = None
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

    # ------------------------------------------------------------- lifecycle

    def register(self, p2p: "P2PManager") -> None:
        with self._lock:
            old = self._groups.get(p2p.group_name)
            self._groups[p2p.group_name] = p2p
        # A same-name re-host replaces the previous registration: stop the old
        # instance so its heartbeats and sockets do not leak. stop() unregisters
        # conditionally, so it cannot remove the fresh registration.
        if old is not None and old is not p2p:
            old.stop()
        self._ensure_running()

    def unregister(self, p2p: "P2PManager") -> None:
        with self._lock:
            # conditional remove: an old same-name instance being stopped must
            # not unregister the replacement that is already registered
            if self._groups.get(p2p.group_name) is p2p:
                self._groups.pop(p2p.group_name, None)
        # keep listening: the shared port also serves direct member chats

    def restart(self, port: Optional[int] = None) -> None:
        """Rebind the listener (e.g. after a bind failure or a port change).
        Existing member connections stay alive — they are owned by the groups."""
        if port is not None:
            self.port = port
        self.stop()
        self._ensure_running()

    def shutdown(self) -> None:
        """Stop the listener for good (app teardown)."""
        self.stop()

    def ensure_running(self) -> None:
        """Make sure the shared port is being listened on (direct chats need
        it even on devices with no host group)."""
        self._ensure_running()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
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

    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(16)
            srv.settimeout(1.0)
            self._server_socket = srv
            self._set_error(None)
        except OSError:
            self._set_error(
                f"无法监听端口 {self.port}，请检查端口是否被占用或防火墙设置"
                f"（Windows 防火墙需允许入站 TCP {self.port}）"
            )
            return
        while not self._stop_event.is_set():
            try:
                client, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            _spawn(self._handle, client)
        try:
            srv.close()
        except OSError:
            pass
        self._server_socket = None

    def _handle(self, sock: socket.socket) -> None:
        """Read the hs_start handshake line, secure the connection, then
        dispatch by mode. Only the secured handshake is accepted — a legacy
        plaintext query_group/join/direct_hello/mesh_hello never works."""
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
            if start.type != Protocol.HS_START:
                self._safe_close(sock)
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
            elif mode == Protocol.MODE_MESH:
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
            elif mode in (Protocol.MODE_QUERY, Protocol.MODE_JOIN):
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
                elif mode == Protocol.MODE_QUERY:
                    p2p._handle_query_group(sock, wire, packet)
                else:
                    # the group's P2PManager takes over the socket (join_ack,
                    # member registration, then its read loop)
                    p2p._handle_join(sock, wire, packet)
            else:
                self._safe_close(sock)
        except Exception:
            self._safe_close(sock)

    def resolve_group(self, id_or_name: Optional[str]) -> Optional["P2PManager"]:
        """Lock-safe group resolution for callers on ANY thread (the
        handshake password lookup runs on socket threads, concurrently with
        register/unregister)."""
        with self._lock:
            return self._resolve_group(id_or_name)

    def _resolve_group(self, id_or_name: Optional[str]) -> Optional["P2PManager"]:
        """Resolve a group by its numeric join id (primary) or its name
        (legacy fallback for groups saved before the numeric id existed).
        Callers must hold self._lock or use resolve_group()."""
        if not id_or_name:
            return None
        p2p = self._groups.get(id_or_name)
        if p2p is not None:
            return p2p
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


class P2PManager:
    # Peer-presence heartbeat: both sides send a ping every interval; a read
    # loop that sees no traffic for HEARTBEAT_TIMEOUT seconds declares the peer
    # offline (detects half-open TCP connections instead of failing only when a
    # message is sent). Keep HEARTBEAT_TIMEOUT > HEARTBEAT_INTERVAL.
    HEARTBEAT_INTERVAL = 15.0
    HEARTBEAT_TIMEOUT = 45.0

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

    @property
    def current_group_id(self) -> str:
        return self.group_id

    @property
    def current_group_name(self) -> str:
        return self.group_name

    @property
    def numeric_group_id(self) -> str:
        """Stable 8-digit group id derived from the machine fingerprint and the
        group name — the join identifier, separate from the display name."""
        return numeric_group_id_of(self.group_name, self.hardware_id)

    def initialize_as_host(
        self, user_name: str, group: str, password: Optional[str] = None
    ) -> None:
        self.my_name = user_name.strip()
        self.group_name = group.strip()
        self.group_id = f"{self.group_name}@{self.hardware_id}"
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
        if self.is_joining:
            return
        self.is_joining = True
        self.connection_lost = False
        self.connection_result = None
        self.listener.join_state_changed(self)

        def run() -> None:
            sock: Optional[socket.socket] = None
            handed_off = False
            try:
                sock = socket.create_connection((target_ip, target_port), timeout=5)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(15)
                reader = sock.makefile("r", encoding="utf-8", newline="\n")
                # password-bound secured handshake: the join (and everything
                # after it) is encrypted and the host is authenticated by its
                # knowledge of the group password
                wire = make_wire(sock, reader)
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
                elif response.type == "join_ack" and response.members is not None:
                    with self._lock:
                        self.group_id = response.group_id or self.group_id
                        for peer in response.members:
                            if peer.id != self.my_id:
                                self.peers[peer.id] = peer
                    self.listener.peers_changed(self)
                    host = response.host
                    if host is not None and (
                        host.ip_address != target_ip or host.port != target_port
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
                        sock = None
                        self._connect_to_host(host)
                        return
                    self._host_socket = sock
                    self._host_wire = wire
                    self.is_joining = False
                    self._set_join_result(True, "")
                    self.listener.peers_changed(self)
                    handed_off = True
                    self._start_heartbeat()
                    self._read_loop_from_host(sock, wire)
                    return
                elif response.type == "join_rejected":
                    self._set_join_result(False, "群组不匹配，连接被拒绝")
                elif response.type == "error":
                    self._set_join_result(False, response.error_message or "加入被拒绝")
                else:
                    self._set_join_result(False, "未知的响应")
            except WireException as e:
                self._set_join_result(False, str(e))
            except Exception as e:
                self._set_join_result(False, f"连接失败: {e}")
            finally:
                if sock is not None and not handed_off:
                    self._safe_close(sock)
                self.is_joining = False
                self.listener.join_state_changed(self)

        self._spawn(run)

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
                sock = None
                self._start_heartbeat()
                self._read_loop_from_host(self._host_socket, wire)
            except Exception:
                if sock is not None:
                    self._safe_close(sock)
                with self._lock:
                    self._host_socket = None
                    self._host_wire = None

        self._spawn(run)

    # -------------------------------------------------------- host handlers

    def _matches_group(self, id_or_name: str) -> bool:
        """True when [id_or_name] identifies this group: the numeric join id
        (primary) or the legacy group name."""
        return id_or_name == self.numeric_group_id or id_or_name == self.group_name

    def _handle_query_group(self, sock: socket.socket, wire: Wire, packet: NetworkPacket) -> None:
        try:
            if not packet.group_id or not self._matches_group(packet.group_id):
                wire.send_packet(NetworkPacket(type="join_rejected"))
            else:
                with self._lock:
                    count = len(self.peers) + 1
                info = GroupInfo(self.group_name, self.my_name, self.my_id, count)
                wire.send_packet(NetworkPacket(type="group_info", group_info=info))
        except Exception:
            pass
        finally:
            self._safe_close(sock)

    def _handle_join(self, sock: socket.socket, wire: Wire, packet: NetworkPacket) -> None:
        # The handshake already verified the group password (password-bound
        # ECDH); only the packet shape is validated here.
        peer = packet.peer
        if not packet.group_id or not self._matches_group(packet.group_id) or peer is None:
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
            wire.send_packet(
                NetworkPacket(type="join_ack", group_id=self.group_id, members=members)
            )
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
        elif packet.type == "delete_message" and packet.message_id is not None:
            with self._lock:
                self.messages = [m for m in self.messages if m.id != packet.message_id]
            self.listener.messages_changed(self)
        elif packet.type == "ping":
            wire = self._host_wire
            if wire is not None:
                try:
                    wire.send_packet(NetworkPacket(type="pong"))
                except Exception:
                    pass
        elif packet.type in CALL_PACKET_TYPES:
            # The host only delivers call packets to the addressed member, so
            # anything arriving here is for this node; double-check the id.
            if packet.call is None:
                return
            if packet.target_id is not None and packet.target_id != self.my_id:
                return
            self._dispatch_call(packet)
        # "pong": traffic only; keeps the read loop alive

    def _process_packet_from_client(self, packet: NetworkPacket, sender_id: str) -> None:
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
        # "pong": traffic only; keeps the read loop alive

    def _route_call_packet(self, packet: NetworkPacket, sender_id: str) -> None:
        """Host-side routing for call signaling: validate the sender identity
        and deliver the packet either locally (the host is the peer) or to the
        addressed member's socket (never broadcast)."""
        call = packet.call
        if call is None:
            return
        if packet.type in ("call_offer", "call_failed"):
            if call.caller_id != sender_id:
                logger.warning("drop call %s from %s: callerId mismatch", packet.type, sender_id)
                return
        elif packet.type in ("call_answer", "call_reject"):
            if call.callee_id != sender_id:
                logger.warning("drop call %s from %s: calleeId mismatch", packet.type, sender_id)
                return
        elif packet.type == "call_hangup":
            if call.caller_id != sender_id and call.callee_id != sender_id:
                logger.warning("drop call_hangup from %s: not a call participant", sender_id)
                return
        target_id = packet.target_id
        if target_id is None or target_id == self.my_id:
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

    def _set_join_result(self, success: bool, message: str) -> None:
        self.connection_result = (success, message)
        self.listener.join_state_changed(self)

    # -------------------------------------------------------------- sending

    def send_message(self, content: str) -> Optional[ChatMessage]:
        """Send a chat message through the host relay; returns the created
        message (or None for invalid content) so the caller can also broadcast
        it over the group mesh."""
        if not is_valid_content(content):
            return None
        message = ChatMessage(
            id=str(uuid.uuid4()),
            content=content,
            timestamp=int(time.time() * 1000),
            sender_id=self.my_id,
            sender_name=self.my_name,
            is_from_me=True,
        )
        with self._lock:
            self.messages.append(message)
        self.listener.messages_changed(self)
        packet = NetworkPacket(type="chat", message=message)
        self._enqueue_send(packet)
        return message

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
        self._enqueue_send(packet)
        return True

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

    def _enqueue_send(self, packet: NetworkPacket) -> None:
        """Queue an outbound packet for the single sender worker thread."""
        with self._lock:
            if self._send_worker is None:
                self._send_worker = threading.Thread(
                    target=self._send_worker_loop, name="LocalChat-sender", daemon=True
                )
                self._send_worker.start()
            self._send_queue.put(packet)

    def send_targeted(self, peer_id: str, packet: NetworkPacket) -> None:
        """Send a packet addressed to a specific member (call signaling).

        As the host the packet goes straight to that member's wire; as a
        client it is relayed through the host with targetId set. Never
        broadcast — the receiving side validates the target id.
        """
        packet.target_id = peer_id
        self._enqueue_send(packet)

    def _send_worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                packet = self._send_queue.get(timeout=0.5)
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
                    if wire is not None:
                        wire.send_packet(packet)
            except Exception:
                pass

    def replay_saved_messages(self, messages: list) -> None:
        with self._lock:
            existing_ids = {m.id for m in self.messages}
            for m in messages:
                if m.id not in existing_ids:
                    self.messages.append(m)

    # -------------------------------------------------------- file transfer

    def send_file(self, path: str) -> Optional[ChatMessage]:
        """Offer a local file to the group. Returns the created file message
        (or None if the file cannot be served) so the caller can also
        broadcast it over the group mesh. The file bytes travel over a
        separate download server, not over the message stream; a per-file
        random key travels INSIDE the encrypted message channel and protects
        the raw download stream."""
        if not path:
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
        file_info = FileInfo(file_id, file_name, file_size, advertised, port, to_b64(file_key))
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
        )
        with self._lock:
            self.messages.append(message)
        self.listener.messages_changed(self)
        self._enqueue_send(NetworkPacket(type="file_message", message=message))
        self._spawn(self._file_server_loop, file_id, path, file_size, file_key)
        return message

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

    def download_file(self, file_info: FileInfo, target_path: str) -> tuple:
        """Download a file offered via [file_info] to [target_path]. Blocks the
        calling thread. Returns (ok: bool, message: str)."""
        return _download_file_offer(file_info, target_path)

    # -------------------------------------------------------------- server

    def _server_loop(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
                client, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self._spawn(lambda c=client: self._handle_incoming(c))

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


class DirectChatManager:
    """Direct member-to-member chat: the management unit is the member, not
    the group. Picking a member immediately pulls up a 1:1 chat over a direct
    TCP connection; the other side auto-accepts (no confirmation) as long as
    the app is running and listening on the program-wide port.

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
        # Keys with a redial loop currently running.
        self._redial_loops: set = set()
        self._redial_guard = threading.Lock()
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
        changed = False
        with self._lock:
            # dedupe by endpoint: a manually added placeholder (id from ip) is
            # replaced by the real contact once a handshake reveals the id
            for old_id in list(self._contacts):
                old = self._contacts[old_id]
                if (
                    old.ip_address == contact.ip_address
                    and old.port == contact.port
                    and old.id != contact.id
                ):
                    del self._contacts[old_id]
                    changed = True
            if self._contacts.get(contact.id) != contact:
                self._contacts[contact.id] = contact
                changed = True
        if changed:
            self._notify_contacts()

    def remove_contact(self, contact_id: str) -> None:
        with self._lock:
            if self._contacts.pop(contact_id, None) is not None:
                self._notify_contacts()

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

    # ---------------------------------------------------------------- actions

    def open_chat(self, contact: Peer) -> None:
        """Record the endpoint a chat key refers to (called when the chat is
        opened or a message is queued): a later handshake uses it to migrate
        alias keys to the revealed real device id."""
        with self._lock:
            self._chat_endpoints[contact.id] = f"{contact.ip_address}:{contact.port}"

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
        self.add_contact(peer)
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
            wire.send_packet(
                NetworkPacket(type=Protocol.DIRECT_HELLO, peer=self.my_peer())
            )
            ack = wire.recv_packet()
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
            sock = None  # ownership transferred to the session
            return remote.id
        except Exception as e:
            if not quiet:
                reason = "未知错误"
                if isinstance(e, socket.timeout):
                    reason = "无响应（请确认对方应用在运行且在同一网络）"
                elif isinstance(e, ConnectionRefusedError):
                    reason = "连接被拒绝（对方应用未运行或端口不对）"
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
        """Listener side: a secured direct_hello arrived on the shared port —
        auto-accept, no confirmation needed (the handshake already
        authenticated the dialer's identity key)."""
        peer = packet.peer
        if peer is None or peer.id == self._my_id:
            self._safe_close(sock)
            return
        # TOFU: a changed identity key for a KNOWN peer id means someone is
        # impersonating or intercepting it — refuse the session.
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

    def send_message(self, peer_id: str, content: str) -> bool:
        """Send a text message. The peer does NOT have to be online: with no
        live session the message is appended locally (marked pending), parked
        in the outbox, and delivered automatically once a session comes up —
        our redial or the peer dialing us. Returns False only when there is
        no known contact AND no session to deliver to."""
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

    def send_file(self, peer_id: str, path: str) -> Optional[ChatMessage]:
        """Offer a local file to a direct-chat member. The bytes are NOT sent
        over the message stream: this opens a short-lived download server on a
        random port and sends a file_message carrying [FileInfo] (incl. the
        download address and per-file key). The receiver connects back to
        download the file (shared encrypted file_download protocol, Android
        parity). Requires a live session (files cannot queue offline)."""
        if not path:
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
        file_info = FileInfo(file_id, file_name, file_size, my_ip, port, to_b64(file_key))
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
        self._put_send(session, NetworkPacket(type="file_message", message=msg))
        self._spawn(self._direct_file_server_loop, file_id, srv, path, file_size, file_key)
        return msg

    def download_file(self, file_info: FileInfo, target_path: str) -> tuple:
        """Download a file offered via [file_info] to [target_path]. Blocks the
        calling thread. Returns (ok: bool, message: str)."""
        return _download_file_offer(file_info, target_path)

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

    def is_chat_alive(self, peer_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(peer_id)
            return bool(session and session["alive"])

    def shutdown(self) -> None:
        self._stop_event.set()
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
        # deliver everything that piled up while the peer was offline
        self._flush_outbox(peer.id, session)
        _spawn(self._send_loop, session)
        _spawn(self._read_loop, session)
        _spawn(self._ping_loop, session)

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
                elif packet.type in CALL_PACKET_TYPES:
                    # 1:1 session: call signaling must involve this member
                    # (self-initiated calls come back as answer/reject with our
                    # own id in callerId)
                    call = packet.call
                    if call is None:
                        continue
                    if call.caller_id != self._my_id and call.callee_id != self._my_id:
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
            state["messages"].append(msg)
            state["messages"].sort(key=lambda m: m.timestamp)
            if len(state["messages"]) > self.HISTORY_CAP:
                del state["messages"][: len(state["messages"]) - self.HISTORY_CAP]
        if not links:
            return
        packet = NetworkPacket(type="mesh_chat", group_id=group_id, message=msg)
        for link in links:
            self._spawn(self._link_write, link, packet)

    def broadcast_delete(self, group_id: str, message_id: str) -> None:
        """Tell every linked member that a message was deleted (host-offline
        path) so deletes converge even when the host relay is unreachable."""
        with self._lock:
            state = self._groups.get(group_id)
            if state is None:
                return
            my_id = state["my_peer"].id if state["my_peer"] else ""
            links = list(state["links"].values())
        if not links:
            return
        packet = NetworkPacket(
            type="delete_message", message_id=message_id, sender_id=my_id
        )
        for link in links:
            self._spawn(self._link_write, link, packet)

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
        independently and dedup by message id (Android parity)."""
        batch = []
        estimated = 0

        def flush() -> None:
            nonlocal batch, estimated
            if not batch:
                return
            try:
                wire.send_packet(
                    NetworkPacket(
                        type="history_reply", group_id=group_id, messages=batch
                    )
                )
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
            mine = state["my_peer"].id if state["my_peer"] else ""
            if peer.id == mine or not peer.id:
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

    def _dial_with_retry(self, group_id: str, peer: Peer) -> None:
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
                time.sleep(self.RETRY_INTERVAL)
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
            time.sleep(self.RETRY_INTERVAL)

    def _try_connect(self, group_id: str, peer: Peer) -> Optional[dict]:
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
                return None
            return {
                "peer_id": peer.id,
                "peer_name": ack.peer.name,
                "sock": sock,
                "wire": wire,
                "alive": True,
            }
        except Exception:
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
                    self._handle_incoming(group_id, [msg])
                elif packet.type == "delete_message":
                    if packet.message_id and packet.sender_id:
                        self._handle_delete_incoming(group_id, link, packet.message_id, packet.sender_id)
                elif packet.type == "history_reply":
                    self._handle_incoming(group_id, packet.messages or [])
                elif packet.type == "mesh_announce" and packet.peer is not None:
                    self.add_peer(group_id, packet.peer)
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

    def _handle_delete_incoming(self, group_id: str, link: dict, message_id: str, sender_id: str) -> None:
        """Apply a mesh-received delete locally: remove the message from this
        group's mesh state and relay it to the ViewModel. Only the original
        sender may delete (same authorization as the host relay); a duplicate
        delete for an already removed message is ignored. No forwarding: the
        mesh links every member pair directly (the sender's broadcast already
        reaches everyone), and relaying would only create a delete storm
        through the complete graph."""
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
