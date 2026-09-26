#!/usr/bin/env python3
"""LocalChat 信标/中继服务器（signaling + relay）。

部署在公网（VPS）上，负责给不同 NAT 网段的两台设备"牵线搭桥"：

  1. 主机与成员各连上来注册同一个群组数字 ID；
  2. 服务器把双方的公网映射地址 (ip, port) 互换（matched）；
  3. 双方各自断开与服务器的连接，从原端口对对方的映射地址发起
     TCP 同时打开（simultaneous open）打洞 —— 之后流量全程直连，
     服务器不再参与；
  4. 打洞失败（如对称 NAT）时，双方回到服务器，服务器把两条控制
     连接变成原始字节管道（中继模式），转发端到端加密的 LocalChat
     协议流 —— 服务器只看到密文。

访问认证（协议 v2）：每条控制连接必须先通过质询-应答认证才允许注册：

  C -> {"type":"hello","proto":2}
  S -> {"type":"challenge","nonce":"<32字节hex>"}
  C -> {"type":"auth","proof":"hex(HMAC-SHA256(secret, "lc-sig-auth-v1|" + nonce))"}
  S -> {"type":"welcome","ip":..,"port":..}     认证失败则回 error 并断开

访问密钥来源（按优先级）：--secret 参数 > 环境变量
LOCALCHAT_SIGNALING_SECRET > 启动时随机生成并打印一次。没有密钥的客户端
无法注册、配对或占用中继资源；证明一次性（fresh nonce），密钥永不上线。

认证之后的协议（客户端 -> 服务器）：

  {"type":"host","groupId":"12345678",...}        主机 armed 等待成员
  {"type":"member","groupId":"12345678",...}      成员等待匹配
  （同一条连接可重复发 host/member 注册多个 groupId（上限
    MAX_GROUPS_PER_CONN）：role 只取第一次出现的 host 或 member，重复的
    (role, groupId) 幂等。）
  {"type":"punch_result","session":..,"ok":bool,"role":"host"|"member"}
  {"type":"relay_request","session":..,"role":..} 单侧打洞失败强制中继
  {"type":"pong"}                                  心跳应答

服务器 -> 客户端：

  {"type":"ping"}                         控制阶段心跳
  {"type":"matched","session":..,"groupId":..,"peer":{"ip","port","role","id","nick"}}
  {"type":"use_punch"}                    双方（或单方重试后）直连成功
  {"type":"relay_start"}                  切换中继：此后这条连接变成原始字节管道
  {"type":"peer_gone"}                    对方在会话中掉线

匹配成功后双方按角色运行 LocalChat 既有的加密握手（密码绑定的
ECDH），服务器无法解密任何业务流量；它只互换地址。

运行：  python3 signaling_server.py [端口] [--secret <访问密钥>]
        （默认端口 25000）
"""

import hashlib
import hmac
import json
import logging
import os
import secrets as pysecrets
import socket
import sys
import threading
import time
import uuid

PROTOCOL_VERSION = 2
DEFAULT_PORT = 25000
AUTH_MSG_PREFIX = "lc-sig-auth-v1|"

MAX_LINE = 128 * 1024  # must cover the app's 64 KiB line cap (relayed app traffic)
HELLO_TIMEOUT = 10.0
CONTROL_IDLE_TIMEOUT = 45.0
DECISION_TIMEOUT = 20.0
RELAY_IDLE_TIMEOUT = 120.0
# "punch" 只是配对到中继之间的过渡态：双方 punch_result 都成功、服务器发出
# use_punch 后，若一侧控制连接一直存活（另一侧已断开），会话必须有界回收，
# 否则 _sessions 条目常驻。窗口取客户端的决策等待（30s）+ 补打/中继回退余量。
PUNCH_IDLE_TIMEOUT = 60.0
PING_INTERVAL = 20.0
MAX_CONNECTIONS = 512
MAX_PER_IP = 32
MAX_GROUP_LEN = 64
MAX_ID_LEN = 64
MAX_NICK_LEN = 64
# One control connection may register at most this many groupIds: a hostile
# client must not be able to grow the _hosts/_waiting maps without bound on
# a single connection.
MAX_GROUPS_PER_CONN = 32

log = logging.getLogger("signaling")


def auth_proof(secret: str, nonce: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        (AUTH_MSG_PREFIX + nonce).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _valid_group_id(value) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_GROUP_LEN
        and all(ch.isalnum() or ch in "@_.-" for ch in value)
    )


def _clip(value, limit: int) -> str:
    return str(value)[:limit] if value is not None else ""


class ClientConn:
    """One control connection (host or member side)."""

    def __init__(self, sock: socket.socket, addr):
        self.sock = sock
        self.ip = addr[0]
        self.port = addr[1]
        self.role = None  # "host" | "member"
        self.group_id = ""  # most recently registered group (debug)
        self.groups = set()  # every groupId registered on this connection
        self.group_meta = {}  # group_id -> {"id": client_id, "nick": nick}
        self.client_id = ""
        self.nick = ""
        # The endpoint the client must punch to (its public mapped address).
        self.endpoint = (self.ip, self.port)
        self.alive = True
        self.send_lock = threading.Lock()
        # Set when this connection becomes one half of a relay pipe: the
        # read loop then stops parsing lines and pumps raw bytes to
        # pump_to.sock instead.
        self.pump_to: "ClientConn" = None
        self.relay_mode = False

    def send_json(self, obj) -> bool:
        try:
            data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
            with self.send_lock:
                self.sock.sendall(data)
            return True
        except OSError:
            self.alive = False
            return False

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except OSError:
            pass


class Session:
    """One host<->member pairing between the matched and the decision."""

    def __init__(self, group_id: str, host: ClientConn, member: ClientConn):
        self.token = uuid.uuid4().hex[:16]
        self.group_id = group_id
        self.roles = {
            "host": {"conn": host, "result": None},
            "member": {"conn": member, "result": None},
        }
        self.mode = "wait"  # wait | punch | relay | done
        self.lock = threading.Lock()
        self.deadline = time.monotonic() + DECISION_TIMEOUT
        self.created = time.time()

    def peer_of(self, role: str) -> ClientConn:
        other = "member" if role == "host" else "host"
        return self.roles[other]["conn"]


class SignalingServer:
    def __init__(self, port: int = DEFAULT_PORT, bind: str = "0.0.0.0", secret: str = None):
        self.port = port
        self.bind = bind
        # Access secret: --secret arg > env > autogenerate (logged once).
        if secret is None:
            secret = os.environ.get("LOCALCHAT_SIGNALING_SECRET", "") or ""
        if not secret:
            secret = pysecrets.token_hex(32)
            log.warning(
                "no --secret given: GENERATED an access secret (clients must "
                "use it to register):\n  %s",
                secret,
            )
        self.secret = secret
        self._sock: socket.socket = None
        self._stop = False
        self._lock = threading.RLock()
        self._hosts: dict = {}  # group_id -> [ClientConn, ...] (newest last)
        self._waiting: dict = {}  # group_id -> [ClientConn, ...] (FIFO)
        self._sessions: dict = {}  # token -> Session
        self._conns: set = set()
        self._thread: threading.Thread = None

    # ------------------------------------------------------------ lifecycle

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.port))
        srv.listen(64)
        srv.settimeout(0.5)
        self.port = srv.getsockname()[1]
        self._sock = srv
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        log.info("signaling server listening on %s:%d", self.bind, self.port)

    def stop(self):
        self._stop = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        with self._lock:
            conns = list(self._conns)
        for c in conns:
            c.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _accept_loop(self):
        while not self._stop:
            try:
                sock, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if len(self._conns) >= MAX_CONNECTIONS:
                    sock.close()
                    continue
                per_ip = sum(1 for c in self._conns if c.ip == addr[0])
                if per_ip >= MAX_PER_IP:
                    sock.close()
                    continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = ClientConn(sock, addr)
            with self._lock:
                self._conns.add(conn)
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    # ------------------------------------------------------------ per-conn

    def _client_loop(self, conn: ClientConn):
        try:
            conn.sock.settimeout(HELLO_TIMEOUT)
            hello = self._read_line(conn.sock)
            if hello is None or not self._dispatch_hello(conn, hello):
                return
            self._ping_loop_start(conn)
            conn.sock.settimeout(CONTROL_IDLE_TIMEOUT)
            while conn.alive and not self._stop:
                if conn.relay_mode:
                    self._pump(conn)
                    return
                line = self._read_line(conn.sock)
                if line is None:
                    if conn.relay_mode:
                        # switched to relay while we waited: keep pumping
                        self._pump(conn)
                    return
                if conn.relay_mode:
                    # This read raced the relay switch: the client already
                    # received relay_start and this line is its first relayed
                    # app line. Forward it, then pump the raw remainder.
                    self._pump(conn, first_line=line)
                    return
                if not self._dispatch(conn, line):
                    return
        except (OSError, ValueError):
            pass
        finally:
            self._cleanup(conn)

    @staticmethod
    def _read_line(sock: socket.socket):
        """Byte-at-a-time line reader (no read-ahead buffering, so a mid-line
        switch to relay mode never swallows app bytes). The cap covers the
        app's MAX_LINE_LENGTH (64 KiB) relayed through the pipe, not just the
        small control messages."""
        buf = bytearray()
        while len(buf) <= MAX_LINE:
            data = sock.recv(1)
            if not data:
                return None
            if data == b"\n":
                return bytes(buf).decode("utf-8", "replace")
            buf.extend(data)
        return None

    def _dispatch_hello(self, conn: ClientConn, line: str) -> bool:
        """Authentication gate: hello(proto) -> challenge -> auth proof ->
        welcome. Only a client that proves the access secret gets a welcome;
        everyone else is dropped before they can register, match or relay."""
        try:
            msg = json.loads(line)
        except ValueError:
            return False
        if msg.get("type") != "hello" or msg.get("proto") != PROTOCOL_VERSION:
            conn.send_json({"type": "error", "errorMessage": "protocol mismatch"})
            return False
        nonce = pysecrets.token_hex(32)
        if not conn.send_json({"type": "challenge", "nonce": nonce}):
            return False
        conn.sock.settimeout(HELLO_TIMEOUT)
        proof_line = self._read_line(conn.sock)
        if proof_line is None:
            return False
        try:
            auth = json.loads(proof_line)
        except ValueError:
            return False
        provided = auth.get("proof") if isinstance(auth, dict) else None
        if (
            auth.get("type") != "auth"
            or not isinstance(provided, str)
            or not hmac.compare_digest(
                provided.encode("utf-8"),
                auth_proof(self.secret, nonce).encode("utf-8"),
            )
        ):
            log.warning("auth failed from %s", conn.ip)
            conn.send_json({"type": "error", "errorMessage": "authentication failed"})
            return False
        if not conn.send_json(
            {"type": "welcome", "ip": conn.endpoint[0], "port": conn.endpoint[1]}
        ):
            return False
        return True

    def _dispatch(self, conn: ClientConn, line: str) -> bool:
        try:
            msg = json.loads(line)
        except ValueError:
            return False
        mtype = msg.get("type")
        if mtype == "pong":
            return True
        if mtype == "host" or mtype == "member":
            return self._register(conn, mtype, msg)
        if mtype == "punch_result":
            return self._record_result(conn, msg)
        if mtype == "relay_request":
            return self._relay_request(conn, msg)
        log.debug("unknown message type %r from %s", mtype, conn.ip)
        return True

    def _register(self, conn: ClientConn, role: str, msg: dict) -> bool:
        group_id = msg.get("groupId")
        if not _valid_group_id(group_id):
            conn.send_json({"type": "error", "errorMessage": "invalid groupId"})
            return False
        if conn.role is not None and conn.role != role:
            return True  # one role per connection: host and member never mix
        if group_id not in conn.groups and len(conn.groups) >= MAX_GROUPS_PER_CONN:
            conn.send_json({"type": "error", "errorMessage": "too many groups"})
            return False
        client_id = _clip(msg.get("clientId"), MAX_ID_LEN)
        nick = _clip(msg.get("nick"), MAX_NICK_LEN)
        with self._lock:
            conn.role = role
            conn.group_id = group_id
            conn.client_id = client_id
            conn.nick = nick
            conn.group_meta[group_id] = {"id": client_id, "nick": nick}
            if group_id in conn.groups:
                return True  # idempotent re-registration of (role, group)
            conn.groups.add(group_id)
            if role == "host":
                self._hosts.setdefault(group_id, []).append(conn)
            else:
                self._waiting.setdefault(group_id, []).append(conn)
            self._try_match(group_id)
        return True

    # ------------------------------------------------------------ matching

    def _try_match(self, group_id: str):
        # caller holds self._lock
        hosts = [c for c in self._hosts.get(group_id, []) if c.alive]
        waiting = [c for c in self._waiting.get(group_id, []) if c.alive]
        while waiting and hosts:
            member = waiting.pop(0)
            # One pairing per armed host registration: on `matched` the host
            # closes its control link to punch from that local port, then the
            # bridge reconnects and re-registers for the next member. Keeping
            # the host listed here would hand a second member a `matched`
            # against a link that is already closing.
            host = hosts.pop()
            session = Session(group_id, host, member)
            self._sessions[session.token] = session
            self._send_matched(session, "host", member)
            self._send_matched(session, "member", host)
            threading.Thread(target=self._decision_watch, args=(session,), daemon=True).start()
            log.info(
                "matched group=%s host=%s member=%s session=%s",
                group_id, host.ip, member.ip, session.token,
            )
        if hosts:
            self._hosts[group_id] = hosts
        else:
            self._hosts.pop(group_id, None)
        if waiting:
            self._waiting[group_id] = waiting
        else:
            self._waiting.pop(group_id, None)

    def _peer_desc(self, conn: ClientConn, group_id: str) -> dict:
        meta = conn.group_meta.get(group_id, {"id": conn.client_id, "nick": conn.nick})
        return {
            "ip": conn.endpoint[0],
            "port": conn.endpoint[1],
            "role": conn.role,
            "id": meta["id"],
            "nick": meta["nick"],
        }

    def _send_matched(self, session: Session, to_role: str, peer: ClientConn):
        conn = session.roles[to_role]["conn"]
        conn.send_json(
            {
                "type": "matched",
                "session": session.token,
                "groupId": session.group_id,
                "peer": self._peer_desc(peer, session.group_id),
            }
        )

    # ------------------------------------------------------------ decisions

    def _record_result(self, conn: ClientConn, msg: dict) -> bool:
        token = msg.get("session")
        role = msg.get("role")
        ok = bool(msg.get("ok"))
        with self._lock:
            session = self._sessions.get(token)
            if session is None or role not in session.roles:
                return True
            if session.mode != "wait":
                return True  # decision already made
            session.roles[role]["result"] = ok
            # Rebind: the punch connected from the SAME local port as the
            # original control connection, so the result may arrive on a
            # fresh connection (the original was closed for punching).
            session.roles[role]["conn"] = conn
            self._decide(session)
        return True

    def _relay_request(self, conn: ClientConn, msg: dict) -> bool:
        token = msg.get("session")
        role = msg.get("role")
        with self._lock:
            session = self._sessions.get(token)
            if session is None or role not in session.roles:
                return True
            if session.mode == "wait":
                session.roles[role]["result"] = False
                session.roles[role]["conn"] = conn
                self._decide(session)
            elif session.mode == "punch":
                # use_punch was sent but this side never completed its
                # connection: everyone falls back to the relay.
                self._start_relay(session)
        return True

    def _decision_watch(self, session: Session):
        while True:
            delay = session.deadline - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, 0.5))
                continue
            with self._lock:
                if self._sessions.get(session.token) is not session:
                    return  # already reaped (relay cleanup / both conns gone)
                if session.mode == "wait":
                    self._decide(session)
                    continue  # _decide set a fresh deadline (punch) or ended us
                if session.mode == "punch":
                    # A punch session is transient: normally _cleanup reaps it
                    # once both control connections are gone, but one surviving
                    # control link must not pin it forever.
                    log.info("session %s expired in punch mode", session.token)
                    self._end_session(session)
                return

    def _decide(self, session: Session):
        # caller holds self._lock; session.mode == "wait"
        #
        # A side that closed its control connection for the punch (planned:
        # the punch rebinds that local port) still owes us a punch_result on
        # a FRESH connection, which rebinds its entry. So a missing result
        # only forces the decision at the deadline — an early decision here
        # would kill every session the moment a side closes its link.
        results = [session.roles[r]["result"] for r in ("host", "member")]
        if not all(r is not None for r in results):
            if time.monotonic() < session.deadline:
                return  # keep waiting for the remaining report
            results = [r if r is not None else False for r in results]
        if all(results):
            session.mode = "punch"
            # Bound the transient punch state from the moment it is entered;
            # _decision_watch wakes on this deadline if no relay_request comes.
            session.deadline = time.monotonic() + PUNCH_IDLE_TIMEOUT
            for role in ("host", "member"):
                conn = session.roles[role]["conn"]
                if conn is not None and conn.alive:
                    conn.send_json({"type": "use_punch"})
            log.info("session %s -> direct punch", session.token)
        else:
            self._start_relay(session)

    def _start_relay(self, session: Session):
        # caller holds self._lock
        session.mode = "relay"
        host_conn = session.roles["host"]["conn"]
        member_conn = session.roles["member"]["conn"]
        if host_conn is None or member_conn is None:
            self._end_session(session, notify_peer_gone=True)
            return
        if not host_conn.alive or not member_conn.alive:
            # a side vanished before it could rebind: nothing to relay over
            self._end_session(session, notify_peer_gone=True)
            return
        # Order matters: set relay_mode on BOTH before either client is
        # told, so each connection's read loop switches to raw pumping
        # before any relayed app bytes can arrive.
        host_conn.pump_to = member_conn
        member_conn.pump_to = host_conn
        host_conn.relay_mode = True
        member_conn.relay_mode = True
        host_conn.send_json({"type": "relay_start"})
        member_conn.send_json({"type": "relay_start"})
        log.info("session %s -> relay", session.token)

    def _end_session(self, session: Session, notify_peer_gone: bool = False):
        # caller holds self._lock
        session.mode = "done"
        self._sessions.pop(session.token, None)
        if notify_peer_gone:
            for role in ("host", "member"):
                entry = session.roles[role]
                if entry["conn"] is not None and entry["conn"].alive:
                    entry["conn"].send_json({"type": "peer_gone"})

    # ------------------------------------------------------------ relay

    def _pump(self, conn: ClientConn, first_line: str = None):
        """Raw byte pipe between the two relayed connections (both ends see
        only end-to-end ciphertext). [first_line] is an app line consumed by
        the read loop before it noticed the relay switch — forwarded first so
        no byte is lost; a racing client control line (e.g. a late pong) is
        dispatched instead of injected into the app stream."""
        peer = conn.pump_to
        if peer is None:
            conn.alive = False
            return
        if first_line is not None:
            if not self._racing_control_line(first_line):
                try:
                    with peer.send_lock:
                        peer.sock.sendall((first_line + "\n").encode("utf-8"))
                except OSError:
                    pass
        conn.sock.settimeout(RELAY_IDLE_TIMEOUT)
        try:
            while conn.alive and peer.alive:
                data = conn.sock.recv(65536)
                if not data:
                    break
                with peer.send_lock:
                    peer.sock.sendall(data)
        except OSError:
            pass
        finally:
            conn.alive = False
            peer.alive = False
            try:
                peer.sock.close()
            except OSError:
                pass

    @staticmethod
    def _racing_control_line(line: str) -> bool:
        """True when [line] is one of the client->server control messages
        (never app data: app lines are encrypted base64 or app JSON with
        different type values)."""
        try:
            obj = json.loads(line)
        except ValueError:
            return False
        return (
            isinstance(obj, dict)
            and obj.get("type")
            in ("hello", "host", "member", "punch_result", "relay_request", "pong")
        )

    # ------------------------------------------------------------ helpers

    def _ping_loop_start(self, conn: ClientConn):
        def ping():
            while conn.alive and not conn.relay_mode and not self._stop:
                time.sleep(PING_INTERVAL)
                if conn.alive and not conn.relay_mode:
                    conn.send_json({"type": "ping"})

        threading.Thread(target=ping, daemon=True).start()

    def _cleanup(self, conn: ClientConn):
        conn.alive = False
        conn.close()
        with self._lock:
            self._conns.discard(conn)
            if conn.role == "host":
                for group_id in conn.groups:
                    lst = self._hosts.get(group_id, [])
                    if conn in lst:
                        lst.remove(conn)
                    if not lst:
                        self._hosts.pop(group_id, None)
            elif conn.role == "member":
                for group_id in conn.groups:
                    lst = self._waiting.get(group_id, [])
                    if conn in lst:
                        lst.remove(conn)
                    if not lst:
                        self._waiting.pop(group_id, None)
            for session in list(self._sessions.values()):
                for role, entry in session.roles.items():
                    if entry["conn"] is conn:
                        if session.mode == "wait":
                            # Do NOT null the entry or decide: a wait-mode
                            # close is usually the PLANNED close before the
                            # punch, and the side owes us a punch_result on
                            # a fresh connection that rebinds this entry.
                            # The deadline watch bounds a side that is gone
                            # for good.
                            pass
                        elif session.mode == "relay":
                            entry["conn"] = None
                            other = session.peer_of(role)
                            session.mode = "done"
                            self._sessions.pop(session.token, None)
                            if other is not None:
                                other.alive = False
                                try:
                                    other.sock.close()
                                except OSError:
                                    pass
                        else:
                            entry["conn"] = None
                # a decided punch session with no control connections left
                # (both clients closed after use_punch) must not leak
                if session.mode == "punch" and all(
                    entry["conn"] is None for entry in session.roles.values()
                ):
                    session.mode = "done"
                    self._sessions.pop(session.token, None)


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = DEFAULT_PORT
    secret = None
    args = list(argv[1:])
    i = 0
    positional = []
    while i < len(args):
        if args[i] == "--secret" and i + 1 < len(args):
            secret = args[i + 1]
            i += 2
            continue
        positional.append(args[i])
        i += 1
    if positional:
        try:
            port = int(positional[0])
        except ValueError:
            print(f"invalid port: {positional[0]}", file=sys.stderr)
            return 2
    server = SignalingServer(port, secret=secret)
    try:
        server.start()
    except OSError as e:
        log.error("cannot bind port %d: %s", port, e)
        return 1
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
