"""Member-sponsored join tests: a newcomer can join a group by connecting to
ANY member's address (not just the creator's). The sponsor answers with the
member list + the host's address, and the newcomer then completes the join by
connecting to the host for the relay path.

The member-side sponsor answer lives in the ViewModel; here a raw sponsor stub
stands in for it (performing the real secured handshake) so the protocol flow
(sponsor handshake -> ack -> connect to host) is verified headlessly with real
sockets. Chinese literals are unicode escapes (\\uXXXX) so the file is
pure-ASCII on disk.
"""

import os
import socket
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import localchat.network as network_module
from localchat.models import GroupInfo, NetworkPacket, Peer
from localchat.network import (
    HostGroupServer,
    P2PListener,
    P2PManager,
    _read_line_bounded,
    make_wire,
)
from localchat.securewire import Handshake
from tests.fake_peer import FakePeerClient


class Rec(P2PListener):
    def __init__(self):
        self.join_results = []

    def join_state_changed(self, p2p):
        if p2p.connection_result is not None:
            self.join_results.append(p2p.connection_result)


def wait_until(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class SponsorSponsor:
    """A member that sponsors a join/query for a group it belongs to: accepts
    the secured handshake (it knows the group password) then answers inside
    the encrypted channel. Mirrors ViewModel._handle_member_group_request."""

    def __init__(self, port, group_id, group_name, group_password, host_peer):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(4)
        self._srv.settimeout(10)
        self._group_id = group_id
        self._group_name = group_name
        self._password = group_password
        self._host_peer = host_peer
        self._port = port

    def serve(self, mode):
        def loop():
            try:
                conn, _ = self._srv.accept()
                conn.settimeout(6)
                reader = conn.makefile("r", encoding="utf-8", newline="\n")
                first = _read_line_bounded(reader)
                if not first:
                    self._safe_close(conn)
                    return
                start = NetworkPacket.from_json(first)
                wire = make_wire(conn, reader)
                secured = Handshake.accept(wire, start, lambda m, g: self._password)
                if secured is None:
                    self._safe_close(conn)
                    return
                packet = wire.recv_packet()
                if packet is None or packet.type != mode:
                    self._safe_close(conn)
                    return
                if mode == "query":
                    info = GroupInfo(self._group_name, "\u67d0\u6210\u5458", "member-dev", 3)  # 某成员
                    wire.send_packet(NetworkPacket(type="group_info", group_info=info))
                elif mode == "join":
                    ack = NetworkPacket(
                        type="join_ack",
                        group_id=self._group_id,
                        members=[self._host_peer],
                        host=self._host_peer,
                    )
                    wire.send_packet(ack)
                self._safe_close(conn)
            except Exception:
                pass

        threading.Thread(target=loop, daemon=True).start()

    @staticmethod
    def _safe_close(conn):
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    @property
    def port(self):
        return self._port

    def close(self):
        try:
            self._srv.close()
        except OSError:
            pass


class SponsorJoinTest(unittest.TestCase):
    PORT_HOST = 19721
    PORT_SPONSOR = 19722
    PORT_JOINER = 19723

    def setUp(self):
        self.server_h = HostGroupServer(self.PORT_HOST)
        self.server_h.ensure_running()
        self.host = P2PManager(Rec(), port=self.PORT_HOST, host_server=self.server_h, device_id="host-dev")
        self.host.initialize_as_host("\u4e3b\u673a", "\u4f1a\u8bae\u5ba4", password="1234")  # 主机 / 会议室
        self.host.start_as_host()
        self.host.set_join_id(self.host.numeric_group_id)
        # the shared listener must authenticate joins like the ViewModel wires
        # it: resolve the group by numeric id / name and return its password
        self.server_h.password_lookup = lambda mode, gid: (
            "1234" if gid in (self.host.numeric_group_id, self.host.group_name) else None
        )

    def tearDown(self):
        self.host.stop()
        self.server_h.shutdown()

    def test_join_via_member_sponsor_reaches_host(self):
        """J connects to the SPONSOR (a member's address, not the host's); the
        sponsor ack reveals the host; J completes the join against the host."""
        host_peer = Peer(self.host.my_id, self.host.my_name, "127.0.0.1", self.PORT_HOST)
        sponsor = SponsorSponsor(
            self.PORT_SPONSOR, self.host.current_group_id,
            self.host.group_name, "1234", host_peer,
        )
        sponsor.serve("join")

        joiner = P2PManager(Rec(), port=self.PORT_JOINER, device_id="joiner-dev")
        joiner.initialize_as_client("\u65b0\u6210\u5458", "", password="1234")  # 新成员
        joiner.set_join_id(self.host.numeric_group_id)
        joiner.confirm_join("127.0.0.1", sponsor.port)
        self.assertTrue(
            wait_until(lambda: joiner.connection_result is not None), "join via sponsor should succeed"
        )
        ok, message = joiner.connection_result
        self.assertTrue(ok, message)
        self.assertTrue(
            wait_until(lambda: joiner.my_id in self.host.peers),
            "host must register the newcomer after the sponsor hop",
        )
        self.assertTrue(
            wait_until(lambda: self.host.my_id in joiner.peers),
            "newcomer must learn the host peer",
        )
        self.assertEqual(
            (joiner.connected_host.ip_address, joiner.connected_host.port),
            ("127.0.0.1", self.PORT_HOST),
            "connected_host must be the host revealed by the join_ack",
        )
        joiner.stop()
        sponsor.close()

    def test_query_via_member_address(self):
        """A query sent to a member's address (not the host's) gets answered."""
        host_peer = Peer(self.host.my_id, self.host.my_name, "127.0.0.1", self.PORT_HOST)
        sponsor = SponsorSponsor(
            self.PORT_SPONSOR, self.host.current_group_id,
            self.host.group_name, "1234", host_peer,
        )
        sponsor.serve("query")

        q = P2PManager(Rec(), port=self.PORT_JOINER, device_id="q-dev")
        q.initialize_as_client("\u8def\u4eba", "", password="1234")  # 路人
        q.set_join_id(self.host.numeric_group_id)
        q.query_group("127.0.0.1", sponsor.port)
        self.assertTrue(wait_until(lambda: q.queried_group_info is not None or q.query_error))
        self.assertIsNone(q.query_error)
        self.assertEqual(q.queried_group_info.group_name, "\u4f1a\u8bae\u5ba4")
        q.stop()
        sponsor.close()


class CrossGroupConfusionTest(unittest.TestCase):
    """The handshake verifies the password for the group named in hs_start;
    the inner query/join packet must name the SAME group. Without that check
    a member of groups A and B (a sponsor of both) would answer a join that
    handshook as A but named B — leaking B's member list and host address to
    someone who only ever proved A's password."""

    PORT = 19731
    GROUP_A = "11111111"
    GROUP_B = "22222222"

    def setUp(self):
        self.server = HostGroupServer(self.PORT)
        self.server.ensure_running()
        self.handled = []
        passwords = {self.GROUP_A: "pwA", self.GROUP_B: "pwB"}
        self.server.password_lookup = (
            lambda mode, gid: passwords.get(gid)  # a device that is a member of both
        )

        def handler(packet, sock, wire):
            self.handled.append(packet.group_id)
            wire.send_packet(
                NetworkPacket(
                    type="join_ack",
                    group_id=packet.group_id,
                    members=[Peer("victim-host", "victim", "10.0.0.1", 1)],
                    host=Peer("victim-host", "victim", "10.0.0.1", 1),
                )
            )
            return True

        self.server.member_group_handler = handler

    def tearDown(self):
        self.server.shutdown()

    def test_join_naming_a_different_group_is_dropped(self):
        c = FakePeerClient.connect("127.0.0.1", self.PORT)
        c.hs_join(self.GROUP_A, password="pwA")
        # knows A's password, but the join names group B
        c.send(
            NetworkPacket(
                type="join", group_id=self.GROUP_B,
                peer=Peer("me", "me", "127.0.0.1", 1),
            )
        )
        ack = c.recv(timeout=2)
        c.close()
        self.assertIsNone(
            ack, "a join naming a different group than the handshake must be dropped"
        )
        self.assertEqual(
            self.handled, [], "the sponsor handler must not run for a mismatched group"
        )

    def test_join_naming_the_same_group_is_answered(self):
        c = FakePeerClient.connect("127.0.0.1", self.PORT)
        c.hs_join(self.GROUP_A, password="pwA")
        c.send(
            NetworkPacket(
                type="join", group_id=self.GROUP_A,
                peer=Peer("me", "me", "127.0.0.1", 1),
            )
        )
        ack = c.recv(timeout=3)
        c.close()
        self.assertIsNotNone(ack)
        self.assertEqual(ack.type, "join_ack")
        self.assertEqual(self.handled, [self.GROUP_A])


if __name__ == "__main__":
    unittest.main()
