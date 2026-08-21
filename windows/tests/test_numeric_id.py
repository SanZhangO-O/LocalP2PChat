"""Numeric group ID tests: the 8-digit join identifier is derived from the
machine fingerprint + group name; members query and join by it (the group
name is only a display label learned from the host). Runs headless with real
loopback sockets.

Every connection uses the secured ECDH handshake (securewire.Handshake) and
the wire is AES-256-GCM encrypted afterwards; the plaintext password field is
gone. The join/query packet "type" is now the handshake mode ("query" /
"join") and the host authenticates via the password-bound handshake while
resolving the group by its numeric join id. A wrong password or an unknown
numeric id fails the handshake itself.

All Chinese literals are written as \\uXXXX escapes so the file stays
pure-ASCII on disk.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.models import NetworkPacket, Peer
from localchat.network import (
    HostGroupServer,
    P2PListener,
    P2PManager,
    format_numeric_group_id,
    numeric_group_id_of,
)
from tests.fake_peer import FakePeerClient, wait_until


GROUP_NAME = "\u4f1a\u8bae\u5ba4"
GROUP_PASSWORD = "1234"
HOST_NAME = "\u4e3b\u673a"
NOT_IN_GROUP = "\u4e0d\u5b58\u5728\u6b64\u7fa4\u7ec4"


class Rec(P2PListener):
    def __init__(self):
        self.join_results = []
        self.query_results = []

    def join_state_changed(self, p2p):
        if p2p.connection_result is not None:
            self.join_results.append(p2p.connection_result)

    def query_result_changed(self, p2p):
        self.query_results.append((p2p.queried_group_info, p2p.query_error))


class NumericIdTest(unittest.TestCase):
    PORT = 19611

    def _password_for(self, mode, group_id):
        """Mirror the ViewModel's password_lookup: resolve the group by its
        numeric id (or legacy name) and return its password, or None when this
        device knows no such group (which makes the handshake reject)."""
        host = self.host
        if group_id == host.numeric_group_id or group_id == host.group_name:
            return host.group_password
        return None

    def setUp(self):
        self.server = HostGroupServer(self.PORT)
        self.server.ensure_running()
        self.server.password_lookup = self._password_for
        self.host = P2PManager(Rec(), port=self.PORT, host_server=self.server, device_id="host-dev")
        self.host.initialize_as_host(HOST_NAME, GROUP_NAME, password=GROUP_PASSWORD)
        self.host.set_join_id(self.host.numeric_group_id)
        self.host.start_as_host()

    def tearDown(self):
        self.host.stop()
        self.server.shutdown()

    def test_numeric_id_stable_and_distinct(self):
        a = numeric_group_id_of(GROUP_NAME, "fp-1")
        b = numeric_group_id_of(GROUP_NAME, "fp-1")
        c = numeric_group_id_of(GROUP_NAME, "fp-2")
        d = numeric_group_id_of("\u4f11\u606f\u5ba4", "fp-1")
        self.assertEqual(a, b, "same fingerprint + name must yield the same id")
        self.assertNotEqual(a, c, "different fingerprint must yield a different id")
        self.assertNotEqual(a, d, "different name must yield a different id")
        self.assertEqual(len(a), 8)
        self.assertTrue(a.isdigit())
        self.assertEqual(len(format_numeric_group_id(a).replace(" ", "")), 8)

    def test_p2pmanager_uses_passed_hardware_id(self):
        """The persisted fingerprint feeds the numeric id: two managers on the
        same device (same passed fingerprint) agree on the group id, which is
        what keeps members able to rejoin across restarts."""
        a = P2PManager(Rec(), port=self.PORT, device_id="d1", hardware_id="fp-stable")
        b = P2PManager(Rec(), port=self.PORT, device_id="d2", hardware_id="fp-stable")
        try:
            a.initialize_as_host(HOST_NAME, GROUP_NAME)
            b.initialize_as_host(HOST_NAME, GROUP_NAME)
            self.assertEqual(a.numeric_group_id, b.numeric_group_id)
        finally:
            a.stop()
            b.stop()

    def test_query_and_join_by_numeric_id(self):
        gid = self.host.numeric_group_id
        member = P2PManager(Rec(), port=self.PORT, device_id="member-dev")
        member.initialize_as_client("\u6210\u5458", "", password=GROUP_PASSWORD)
        member.set_join_id(gid)
        member.query_group("127.0.0.1", self.PORT)
        self.assertTrue(
            wait_until(lambda: member.queried_group_info is not None or member.query_error),
            "query by numeric id should resolve",
        )
        self.assertIsNone(member.query_error)
        self.assertEqual(member.queried_group_info.group_name, GROUP_NAME)
        self.assertEqual(member.group_name, GROUP_NAME, "display name comes from the host")

        member.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: member.connection_result is not None))
        ok, message = member.connection_result
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(lambda: member.my_id in self.host.peers))
        member.stop()

    def test_wrong_id_rejected(self):
        """An unknown numeric id makes the host's password_lookup return None,
        so the handshake itself rejects; the real P2PManager surfaces the
        rejection message as query_error."""
        member = P2PManager(Rec(), port=self.PORT, device_id="member-dev")
        member.initialize_as_client("\u8def\u4eba", "", password=GROUP_PASSWORD)
        member.set_join_id("00000000")
        member.query_group("127.0.0.1", self.PORT)
        self.assertTrue(
            wait_until(lambda: member.query_error is not None),
            "wrong numeric id must be rejected",
        )
        self.assertIn(NOT_IN_GROUP, member.query_error)
        member.stop()

    def test_raw_query_by_numeric_id(self):
        """A raw (FakePeerClient) peer performs the secured handshake and then
        queries by numeric id over the encrypted wire."""
        gid = self.host.numeric_group_id
        s = FakePeerClient.connect("127.0.0.1", self.PORT)
        try:
            s.hs_query(gid, GROUP_PASSWORD)
            s.send(NetworkPacket(type="query", group_id=gid))
            resp = s.recv()
            self.assertIsNotNone(resp)
            self.assertEqual(resp.type, "group_info")
            self.assertEqual(resp.group_info.group_name, GROUP_NAME)
            self.assertEqual(resp.group_info.creator_name, HOST_NAME)
            self.assertEqual(resp.group_info.member_count, 1)
        finally:
            s.close()

    def test_raw_join_by_numeric_id(self):
        gid = self.host.numeric_group_id
        s = FakePeerClient.connect("127.0.0.1", self.PORT)
        try:
            peer = Peer("raw-member-1", "\u6210\u5458", "127.0.0.1", 19998)
            s.hs_join(gid, GROUP_PASSWORD)
            s.send(NetworkPacket(type="join", group_id=gid, peer=peer))
            ack = s.recv()
            self.assertIsNotNone(ack)
            self.assertEqual(ack.type, "join_ack")
            self.assertEqual(ack.group_id, self.host.group_id)
            self.assertEqual(len(ack.members), 1)
            self.assertEqual(ack.members[0].id, self.host.my_id)
        finally:
            s.close()

    def test_raw_wrong_id_rejected_at_handshake(self):
        """No group matches the numeric id -> password_lookup returns None ->
        the host sends hs_reject and the client handshake raises."""
        s = FakePeerClient.connect("127.0.0.1", self.PORT)
        try:
            with self.assertRaises(Exception):
                s.hs_query("00000000", GROUP_PASSWORD)
        finally:
            s.close()

    def test_raw_wrong_password_rejected_at_handshake(self):
        """A wrong password fails the password-bound handshake (raises) before
        any query/join packet can be sent."""
        gid = self.host.numeric_group_id
        s = FakePeerClient.connect("127.0.0.1", self.PORT)
        try:
            with self.assertRaises(Exception):
                s.hs_query(gid, "wrong")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
