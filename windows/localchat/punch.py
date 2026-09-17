"""跨网段连接客户端：信令配对 + TCP 同时打开打洞 + 服务器中继兜底。

与 server/signaling_server.py 配套。流程（见该文件顶部说明）：

  1. 连上信令服务器，记住自己控制连接的 LOCAL 端口和服务器回报的
     公网映射地址（NAT 通常按内部端口建立映射，打洞必须复用同一
     本地端口才能走同一映射）；
  2. 注册角色（host/member）+ 群组数字 ID，等服务器配对（matched），
     拿到对方的公网映射地址；
  3. 断开控制连接，用一个绑定同一本地端口（SO_REUSEADDR 允许复用
     刚关闭的 TIME_WAIT 端口）的 socket 循环向对方映射地址 connect
     —— 双方同时进行，SYN 交叉后即完成 TCP simultaneous open；
  4. 把结果回报给服务器换决策：双方成功 -> 直连；否则退回中继
     （控制连接变成原始字节管道，跑既有的加密 LocalChat 协议）。

返回的 socket 上运行的是完全不变的 LocalChat 协议（securewire 的
密码绑定握手 + AES-GCM），服务器/中间人无法注入：握手会认证群组
密码，假地址配对建立不了会话。
"""

import json
import socket
import threading
import time
from typing import Optional, Tuple

from .models import MAX_LINE_LENGTH

SIGNALING_PROTO = 1
DEFAULT_SIGNALING_PORT = 25000

# 打洞总时长：双方重试循环必须重叠，任何一方过早放弃都会让另一方的
# SYN 永远等不到回应。
PUNCH_TIMEOUT = 10.0
PUNCH_RETRY_INTERVAL = 0.4
PUNCH_CONNECT_TIMEOUT = 1.5
# use_punch 后单侧未完成时的补打窗口。
REPUNCH_TIMEOUT = 6.0
# 等服务器决策（punch_result -> use_punch/relay_start）的上限；服务器
# 自己的 DECISION_TIMEOUT 是 20s，这里要更长。
DECISION_WAIT = 30.0
# 配对等待上限（主机桥接重连周期是秒级，正常远小于此）。
MATCH_WAIT = 45.0

ROLE_HOST = "host"
ROLE_MEMBER = "member"


class PunchError(Exception):
    """Signaling/punch failure surfaced as the join error message."""


def parse_server_endpoint(text: str) -> Tuple[Optional[str], int]:
    """Parse 'host' or 'host:port' (hostname allowed, unlike the LAN join
    field). Returns (host, port); port defaults to DEFAULT_SIGNALING_PORT.
    (None, 0) when the input has no usable host part."""
    text = (text or "").strip()
    if not text:
        return None, 0
    host, port_str = text, ""
    if text.count(":") == 1:
        host, port_str = text.rsplit(":", 1)
    host = host.strip()
    if not host:
        return None, 0
    port = DEFAULT_SIGNALING_PORT
    if port_str:
        if not (port_str.isascii() and port_str.isdigit()):
            return None, 0
        port = int(port_str)
        if not 1 <= port <= 65535:
            return None, 0
    return host, port


class _Link:
    """One control connection to the signaling server (JSON lines)."""

    def __init__(self, server_host: str, server_port: int, timeout: float = 10.0):
        self.sock = socket.create_connection((server_host, server_port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(timeout)
        self.reader = self.sock.makefile("r", encoding="utf-8", newline="\n")
        # The punch socket later rebinds THIS local port so its outbound
        # traffic rides the same NAT mapping the server just reported.
        self.local_port = self.sock.getsockname()[1]
        self.my_endpoint: Optional[Tuple[str, int]] = None
        self._send({"type": "hello", "proto": SIGNALING_PROTO})
        welcome = self._recv(timeout)
        if welcome is None or welcome.get("type") != "welcome":
            raise PunchError("信令服务器响应无效")
        try:
            self.my_endpoint = (str(welcome["ip"]), int(welcome["port"]))
        except (KeyError, TypeError, ValueError):
            raise PunchError("信令服务器响应无效")

    def _send(self, obj) -> None:
        self.sock.sendall((json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    def _recv(self, timeout: float) -> Optional[dict]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                line = self.reader.readline(MAX_LINE_LENGTH + 1)
            except socket.timeout:
                return None  # idle tick (bridge poll / decision wait)
            if not line:
                raise PunchError("信令服务器连接中断")
            if len(line) > MAX_LINE_LENGTH and not line.endswith("\n"):
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # not JSON: ignore (never expected from our server)
            if msg.get("type") == "ping":
                try:
                    self._send({"type": "pong"})
                except OSError:
                    return None
                continue
            return msg

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _punch_once(peer_endpoint: Tuple[str, int], local_port: int) -> Optional[socket.socket]:
    """One simultaneous-open attempt from a socket bound to [local_port]."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # SO_REUSEADDR: the control connection just closed this port (client
    # side went TIME_WAIT) — required on Windows to rebind it.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("", local_port))
    except OSError:
        s.close()
        return None
    try:
        s.settimeout(PUNCH_CONNECT_TIMEOUT)
        s.connect(peer_endpoint)
        s.settimeout(None)
        return s
    except OSError:
        try:
            s.close()
        except OSError:
            pass
        return None


def _punch_loop(peer_endpoint: Tuple[str, int], local_port: int, deadline: float) -> Optional[socket.socket]:
    """Retry loop: NAT mappings/whitelists only line up after both sides
    have sent SYNs, so early attempts legitimately fail with RST/drops."""
    while True:
        s = _punch_once(peer_endpoint, local_port)
        if s is not None:
            return s
        if time.monotonic() >= deadline:
            return None
        time.sleep(PUNCH_RETRY_INTERVAL)


def punch_connect(
    server_host: str,
    server_port: int,
    group_id: str,
    role: str,
    client_id: str,
    nick: str,
    punch_timeout: float = PUNCH_TIMEOUT,
) -> socket.socket:
    """Establish the cross-NAT data socket for [group_id] (numeric join id).

    Returns a connected socket carrying the standard LocalChat line protocol
    (caller runs the secured handshake on it). Raises PunchError when the
    signaling server cannot pair the peers or every path (punch and relay)
    fails.
    """
    link = _Link(server_host, server_port)
    try:
        link._send(
            {
                "type": role,
                "groupId": group_id,
                "clientId": client_id,
                "nick": nick,
            }
        )
        matched = link._recv(MATCH_WAIT)
        if matched is not None and matched.get("type") == "error":
            raise PunchError(str(matched.get("errorMessage") or "配对失败"))
        if matched is None:
            raise PunchError("对方不在线或未开始打洞")
        if matched.get("type") != "matched":
            raise PunchError("信令服务器响应无效")
        session = str(matched.get("session") or "")
        peer = matched.get("peer") or {}
        try:
            peer_endpoint = (str(peer["ip"]), int(peer["port"]))
        except (KeyError, TypeError, ValueError):
            raise PunchError("信令服务器响应无效")
    finally:
        local_port = link.local_port
        link.close()

    punched = _punch_loop(peer_endpoint, local_port, time.monotonic() + punch_timeout)

    # Report the outcome and wait for the server's decision.
    rlink = _Link(server_host, server_port)
    try:
        rlink._send(
            {"type": "punch_result", "session": session, "role": role, "ok": punched is not None}
        )
        decision = rlink._recv(DECISION_WAIT)
        if decision is None:
            raise PunchError("打洞超时，且中继不可用")
        dtype = decision.get("type")
        if dtype == "use_punch":
            if punched is not None:
                return _own(punched, rlink)
            # The peer completed its side: our mapping exists now, so a
            # short retry usually lands the same connection.
            repunched = _punch_loop(
                peer_endpoint, local_port, time.monotonic() + REPUNCH_TIMEOUT
            )
            if repunched is not None:
                return _own(repunched, rlink)
            rlink._send(
                {"type": "relay_request", "session": session, "role": role}
            )
            decision = rlink._recv(DECISION_WAIT)
            dtype = decision.get("type") if decision else None
        if dtype == "relay_start":
            if punched is not None:
                punched.close()  # direct path lost the race; use the relay
            return _own(rlink.sock, rlink)
        raise PunchError("打洞失败且中继不可用")
    except PunchError:
        if punched is not None:
            punched.close()
        raise
    except OSError:
        if punched is not None:
            punched.close()
        raise PunchError("中继连接中断")


def _own(sock: socket.socket, link: _Link) -> socket.socket:
    """Detach the data socket from its control link. In the use_punch paths
    the control connection is no longer needed and is closed explicitly (in
    the relay path it IS the data socket and must survive)."""
    ctl = link.sock
    link.sock = None
    if ctl is not None and ctl is not sock:
        try:
            ctl.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            ctl.close()
        except OSError:
            pass
    return sock


class SignalingHostBridge:
    """Keeps every hosted group registered on the signaling server and turns
    matched members into LocalChat connections served by HostGroupServer.

    The bridge's persistent control connection is ALSO the punched
    connection's local port: on `matched` the session thread closes it
    (its NAT mapping stays alive for the punch), punches, reports the
    result on a fresh link, then the main loop reconnects and re-registers
    so the next member can be served while this one joins.
    """

    RECONNECT_DELAY = 3.0
    IDLE_POLL = 0.5

    def __init__(self, server_host: str, server_port: int, host_server):
        self._server_host = server_host
        self._server_port = server_port
        self._host_server = host_server
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        self._groups_dirty = threading.Event()
        self._link: Optional[_Link] = None
        self._link_lock = threading.Lock()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="LocalChat-signaling-bridge", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._groups_dirty.set()
        with self._link_lock:
            link = self._link
        if link is not None:
            link.close()

    def notify_groups_changed(self) -> None:
        self._groups_dirty.set()

    # ------------------------------------------------------------ loop

    def _run(self) -> None:
        while not self._stop:
            try:
                link = _Link(self._server_host, self._server_port)
            except (OSError, PunchError):
                if self._stop:
                    return
                time.sleep(self.RECONNECT_DELAY)
                continue
            with self._link_lock:
                self._link = link
            try:
                self._register_all(link)
                while not self._stop:
                    if self._groups_dirty.is_set():
                        self._groups_dirty.clear()
                        self._register_all(link)
                    msg = link._recv(self.IDLE_POLL)
                    if msg is None:
                        continue  # idle poll tick
                    if msg.get("type") == "matched":
                        threading.Thread(
                            target=self._handle_matched,
                            args=(msg, link.local_port),
                            daemon=True,
                        ).start()
                    elif msg.get("type") == "error":
                        break
            except (OSError, PunchError):
                pass
            finally:
                with self._link_lock:
                    if self._link is link:
                        self._link = None
                link.close()
            if self._stop:
                return
            time.sleep(self.RECONNECT_DELAY)

    def _register_all(self, link: _Link) -> None:
        for group_id, group_name in self._host_server.signaling_groups():
            link._send(
                {
                    "type": ROLE_HOST,
                    "groupId": group_id,
                    "clientId": "",
                    "nick": group_name,
                }
            )

    # ------------------------------------------------------------ sessions

    def _handle_matched(self, matched: dict, local_port: int) -> None:
        """One pairing: punch to the member, then hand the socket to the
        host server's dispatch (secured handshake + query/join on it)."""
        session = str(matched.get("session") or "")
        group_id = str(matched.get("groupId") or "")
        peer = matched.get("peer") or {}
        try:
            peer_endpoint = (str(peer["ip"]), int(peer["port"]))
        except (KeyError, TypeError, ValueError):
            return
        if not session or not group_id:
            return
        # Close the persistent link NOW: the punch must rebind its local
        # port (the mapping the server reported), and a still-open socket
        # would make that bind fail. The main loop sees the closed socket
        # and reconnects/re-registers for the next member.
        with self._link_lock:
            if self._link is not None and self._link.local_port == local_port:
                self._link.close()
                self._link = None

        punched = _punch_loop(
            peer_endpoint, local_port, time.monotonic() + PUNCH_TIMEOUT
        )
        rlink = None
        try:
            rlink = _Link(self._server_host, self._server_port)
            rlink._send(
                {
                    "type": "punch_result",
                    "session": session,
                    "role": ROLE_HOST,
                    "ok": punched is not None,
                }
            )
            decision = rlink._recv(DECISION_WAIT)
            dtype = decision.get("type") if decision else None
            if dtype == "use_punch" and punched is not None:
                sock = _own(punched, rlink)
            elif dtype == "use_punch":
                repunched = _punch_loop(
                    peer_endpoint, local_port, time.monotonic() + REPUNCH_TIMEOUT
                )
                if repunched is None:
                    rlink._send(
                        {"type": "relay_request", "session": session, "role": ROLE_HOST}
                    )
                    decision = rlink._recv(DECISION_WAIT)
                    dtype = decision.get("type") if decision else None
                    sock = (
                        _own(rlink.sock, rlink)
                        if dtype == "relay_start"
                        else None
                    )
                else:
                    sock = _own(repunched, rlink)
            elif dtype == "relay_start":
                if punched is not None:
                    punched.close()
                sock = _own(rlink.sock, rlink)
            else:
                sock = None
        except (OSError, PunchError):
            if punched is not None:
                punched.close()
            return

        if sock is None:
            return
        # The member runs a keep-open query first (group display name), then
        # the join handshake on the SAME socket — dispatch both.
        self._host_server._handle(sock, allow_query_join=True)
