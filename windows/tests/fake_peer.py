"""Test double: a peer on the other end that performs the REAL secured
handshake (securewire.Handshake) over a raw socket, then exchanges
NetworkPackets over the secured Wire.

Both the host side and the client side of LocalChat are exercised here, so
these helpers make a raw socket behave like a remote LocalChat device without
standing up a full P2PManager/DirectChatManager. They mirror what the Android
app does on the wire: password-bound ECDH handshake for query/join/mesh and
identity-signed ECDH for direct chats / call media.
"""

import json
import socket
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.network import Handshake, Protocol, Wire, make_wire
from localchat.models import Peer, NetworkPacket
from localchat.crypto import generate_ec_key_pair
from localchat.securewire import DeviceIdentity, WireException


class FakePeerClient:
    """Dial side of a secured connection: connects, runs the client handshake,
    then sends/receives NetworkPackets over the encrypted wire."""

    def __init__(self, sock):
        self.sock = sock
        self.wire = make_wire(sock)

    @classmethod
    def connect(cls, host, port, timeout=6):
        s = socket.create_connection((host, port), timeout=timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(s)

    # ---- client handshakes ----

    def hs_query(self, group, password=""):
        return Handshake.initiate(self.wire, Protocol.MODE_QUERY, group, password)

    def hs_join(self, group, password="", expected_peer_id=None):
        # optional identity binding (join never binds; reserved for symmetry)
        return Handshake.initiate(self.wire, Protocol.MODE_JOIN, group, password)

    def hs_mesh(self, group_id, password):
        return Handshake.initiate(self.wire, Protocol.MODE_MESH, group_id, password)

    def hs_direct(self, expected_peer_id=None):
        return Handshake.initiate_direct(
            self.wire,
            expected_peer_id=expected_peer_id,
            on_identity_mismatch=lambda: self._identity_warned(),
        )

    def _identity_warned(self):
        raise AssertionError("identity mismatch flagged by the handshake")

    # ---- IO ----

    def send(self, packet):
        self.wire.send_packet(packet)

    def send_json(self, json_str):
        """Send an arbitrary JSON line through the ALREADY-secured wire
        (encrypted), for tests that need to inject raw bytes a normal
        NetworkPacket cannot represent (e.g. unknown keys) — mirroring what an
        Android peer might emit that we must tolerate."""
        self.wire.send_raw_encrypted(json_str)

    def recv_raw_line(self, timeout=6):
        self.sock.settimeout(timeout)
        buf = bytearray()
        try:
            while True:
                chunk = self.sock.recv(1)
                if not chunk:
                    return None
                buf.extend(chunk)
                if chunk == b"\n":
                    break
        except socket.timeout:
            return None
        except (ConnectionResetError, OSError):
            return None
        return bytes(buf).decode("utf-8", "replace").rstrip("\r\n")

    def recv(self, timeout=6, skip_heartbeat=False):
        """Read ONE decrypted packet (skipping ping/pong when skip_heartbeat,
        so routing assertions aren't confused by heartbeats). A dead or
        tampered wire also returns None (tests wait for a response that never
        comes), but records the reason in [last_error] so assertions can tell
        a protocol rejection apart from a broken wire."""
        self.last_error = None
        deadline = time.time() + timeout
        while True:
            try:
                self.sock.settimeout(max(deadline - time.time(), 0.05))
                pkt = self.wire.recv_packet()
            except WireException as e:
                self.last_error = e
                return None
            except Exception:
                return None
            if pkt is None:
                return None
            if skip_heartbeat and pkt.type in ("ping", "pong"):
                continue
            return pkt

    def recv_or_none(self, timeout=0.4, skip_heartbeat=False):
        pkt = self.recv(timeout=timeout, skip_heartbeat=skip_heartbeat)
        return pkt

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class FakeHostServer:
    """Acceptor side: binds a listening socket and, for each connection, runs
    the server-side handshake then hands the secured connection to a handler
    (like HostGroupServer._handle). Used to stand in for an Android host."""

    def __init__(self, password_lookup):
        self._password_lookup = password_lookup
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]

    def accept_loop(self, on_secured):
        """Accept connections and call on_secured(mode, group_id, conn, wire)
        after the handshake (all modes supported)."""
        import threading

        def loop():
            while True:
                try:
                    conn, _ = self._srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                threading.Thread(target=self._handle, args=(conn, on_secured), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()

    def _handle(self, conn, on_secured):
        try:
            conn.settimeout(15)
            reader = conn.makefile("r", encoding="utf-8", newline="\n")
            from localchat.network import _read_line_bounded

            first = _read_line_bounded(reader)
            if not first:
                conn.close()
                return
            start = NetworkPacket.from_json(first)
            wire = make_wire(conn, reader)
            if start.hs_mode == Protocol.MODE_DIRECT:
                secured = Handshake.accept_direct(wire, start, None)
                if secured is None:
                    conn.close()
                    return
                on_secured(secured.mode, secured.group_id, conn, wire)
                return
            password = self._password_lookup(start.hs_mode, start.group_id)
            secured = Handshake.accept(wire, start, lambda m, g: password)
            if secured is None:
                conn.close()
                return
            on_secured(secured.mode, secured.group_id, conn, wire)
        except Exception:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass


def install_identity():
    """Give the test process a fresh long-term identity key (direct chats and
    call media require DeviceIdentity.current). Each call replaces it."""
    DeviceIdentity.install(generate_ec_key_pair())


def wait_until(cond, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False
