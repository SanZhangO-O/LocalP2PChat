"""Functional integration tests: P2P networking, ViewModel flows, persistence.

Every connection now uses the ECDH secured handshake (securewire.Handshake)
+ AES-256-GCM packets; the plaintext password field is GONE from join packets
(the password-bound handshake authenticates). HostGroupServer resolves group
passwords via `password_lookup`; `_handle_member_group_request(packet, sock,
wire)` receives an already-secured wire.

Chinese literals are written as unicode escapes so this file stays pure-ASCII
on disk. Run with:  python -m pytest tests/test_functional.py -q
"""

import os
import shutil
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

import localchat.network as network_module
from localchat.models import Peer
from localchat.network import P2PListener, P2PManager
from localchat.storage import ChatStore, SavedGroup
from localchat.view_model import ChatViewModel

HOST_NAME = "\u6d4b\u8bd5\u4e3b\u673a"  # \\u6d4b\\u8bd5\\u4e3b\\u673a
GROUP_NAME = "\u6d4b\u8bd5\u7fa4"  # \\u6d4b\\u8bd5\\u7fa4
PASSWORD = "pass123"

# Per-VM data dir (identity key + persisted password settings land here, not
# in the repo root).
DATA_DIR = os.path.join(tempfile.gettempdir(), "kilo", "lc_vmdata")


def _fresh_db(name):
    path = os.path.join(tempfile.gettempdir(), "kilo", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    return path


def make_vm(db_path):
    """Build a ChatViewModel with the constructor's required data_dir."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return ChatViewModel(ChatStore(db_path), data_dir=DATA_DIR)


class Recorder(P2PListener):
    def __init__(self):
        self.join_results = []
        self.query_results = []
        self.typing_events = []

    def join_state_changed(self, p2p):
        if p2p.connection_result is not None:
            self.join_results.append(p2p.connection_result)

    def query_result_changed(self, p2p):
        self.query_results.append((p2p.queried_group_info, p2p.query_error))

    def typing_changed(self, p2p, sender_id, active):
        self.typing_events.append((sender_id, active))


def wait_until(cond, timeout=12.0, pump=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pump is not None:
            pump()
        if cond():
            return True
        time.sleep(0.02)
    return False


class P2PNetworkTest(unittest.TestCase):
    PORT = 19201

    def setUp(self):
        self.host = P2PManager(Recorder(), port=self.PORT, password=PASSWORD)
        self.host.initialize_as_host(HOST_NAME, GROUP_NAME, password=PASSWORD)
        self.host.set_join_id(self.host.numeric_group_id)
        self.host.start_as_host()

    def tearDown(self):
        self.host.stop()

    def _join(self, name="\u6d4b\u8bd5\u6210\u5458", password=None):
        client = P2PManager(Recorder(), port=self.PORT)
        client.initialize_as_client(
            name, GROUP_NAME, password=password if password is not None else PASSWORD
        )
        client.set_join_id(self.host.numeric_group_id)
        client.query_group("127.0.0.1", self.PORT)
        self.assertTrue(
            wait_until(lambda: client.queried_group_info is not None or client.query_error)
        )
        self.assertIsNone(client.query_error)
        client.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: client.connection_result is not None))
        ok, message = client.connection_result
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(lambda: self.host.my_id in client.peers))
        self.assertTrue(wait_until(lambda: client.my_id in self.host.peers))
        return client

    def test_query_mismatch(self):
        # a wrong GROUP with the right password passes the secured handshake
        # and is rejected by the query packet (the per-instance host server
        # accepts the handshake for any group name)
        c = P2PManager(Recorder(), port=self.PORT)
        c.initialize_as_client(
            "\u8def\u4eba", "\u4e0d\u5b58\u5728\u7684\u7fa4", password=PASSWORD
        )
        c.query_group("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: c.query_error is not None))
        self.assertIn("\u4e0d\u5b58\u5728\u6b64\u7fa4\u7ec4", c.query_error)
        c.stop()

    def test_join_wrong_password_rejected(self):
        # the wrong password fails the ECDH handshake itself: query_error and
        # the join result both carry "\\u7fa4\\u7ec4\\u5bc6\\u7801\\u9519\\u8bef"
        c = P2PManager(Recorder(), port=self.PORT)
        c.initialize_as_client("\u8def\u4eba", GROUP_NAME, password="wrongpassword")
        c.set_join_id(self.host.numeric_group_id)
        c.query_group("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: c.query_error is not None))
        self.assertIn("\u7fa4\u7ec4\u5bc6\u7801\u9519\u8bef", c.query_error)
        c.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: c.connection_result is not None))
        ok, message = c.connection_result
        self.assertFalse(ok)
        self.assertIn("\u7fa4\u7ec4\u5bc6\u7801\u9519\u8bef", message)
        self.assertTrue(wait_until(lambda: c.my_id not in self.host.peers))
        c.stop()

    def test_reconnect_keeps_stable_identity(self):
        """A reconnect with the same persisted device id must look like the
        same member: the host keeps exactly one peer entry under that id, and
        the old socket's cleanup must not remove the just-rejoined member."""
        client1 = P2PManager(Recorder(), port=self.PORT, device_id="stable-device-1")
        client1.initialize_as_client("\u6210\u5458", GROUP_NAME, password=PASSWORD)
        client1.set_join_id(self.host.numeric_group_id)
        client1.query_group("127.0.0.1", self.PORT)
        self.assertTrue(
            wait_until(lambda: client1.queried_group_info is not None or client1.query_error)
        )
        self.assertIsNone(client1.query_error)
        client1.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: client1.connection_result is not None))
        ok, message = client1.connection_result
        self.assertTrue(ok, message)
        self.assertEqual(client1.my_id, "stable-device-1")

        # simulate a reconnect: the old p2p stops (old socket dies) and a new
        # one joins with the same persisted device id
        client1.stop()
        client2 = P2PManager(Recorder(), port=self.PORT, device_id="stable-device-1")
        client2.initialize_as_client("\u6210\u5458", GROUP_NAME, password=PASSWORD)
        client2.set_join_id(self.host.numeric_group_id)
        client2.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: client2.connection_result is not None))
        ok, message = client2.connection_result
        self.assertTrue(ok, message)
        self.assertEqual(client2.my_id, "stable-device-1")

        try:
            # the host must see exactly one member under the stable id
            self.assertTrue(
                wait_until(
                    lambda: len(self.host.peers) == 1
                    and self.host.peers.get("stable-device-1") is not None,
                    pump=None,
                ),
                f"host peers wrong after reconnect: {self.host.peers}",
            )
            # and the member must still see the host
            self.assertTrue(
                wait_until(lambda: self.host.my_id in client2.peers),
                f"member peers wrong after reconnect: {client2.peers}",
            )
        finally:
            client2.stop()

    def test_chat_and_delete_sync(self):
        client = self._join()
        try:
            hello = "\u4f60\u597d\uff0c\u6765\u81ea\u6210\u5458"  # \\u4f60\\u597d\\uff0c\\u6765\\u81ea\\u6210\\u5458
            client.send_message(hello)
            self.assertTrue(
                wait_until(lambda: any(m.content == hello for m in self.host.messages))
            )
            reply = "\u6536\u5230\uff0c\u6765\u81ea\u4e3b\u673a"  # \\u6536\\u5230\\uff0c\\u6765\\u81ea\\u4e3b\\u673a
            self.host.send_message(reply)
            self.assertTrue(
                wait_until(lambda: any(m.content == reply for m in client.messages))
            )
            client.remove_message(client.messages[0].id)
            self.assertTrue(
                wait_until(
                    lambda: all(m.content != hello for m in self.host.messages)
                )
            )
        finally:
            client.stop()

    def test_group_typing_relayed_to_other_members(self):
        """typing packets ride the host relay: the host sees a member's
        indicator and relays it to the other members, but never back to the
        sender (the sender's UI already knows it is typing)."""
        member_a = self._join("\u6210\u5458A")  # 成员A
        member_b = self._join("\u6210\u5458B")  # 成员B
        try:
            member_a.send_typing(True)
            self.assertTrue(
                wait_until(
                    lambda: (member_a.my_id, True) in self.host.listener.typing_events
                ),
                "host must see A typing",
            )
            self.assertTrue(
                wait_until(
                    lambda: (member_a.my_id, True) in member_b.listener.typing_events
                ),
                "B must see A typing through the relay",
            )
            self.assertNotIn(
                (member_a.my_id, True),
                member_a.listener.typing_events,
                "A must not receive its own relayed indicator",
            )
            member_a.send_typing(False)
            self.assertTrue(
                wait_until(
                    lambda: (member_a.my_id, False) in member_b.listener.typing_events
                ),
                "the stop must be relayed too",
            )
        finally:
            member_a.stop()
            member_b.stop()

    def test_delete_authorized_broadcast(self):
        a = self._join("\u6210\u5458A")
        b = self._join("\u6210\u5458B")
        try:
            content_a = "A \u7684\u6d88\u606f"
            a.send_message(content_a)
            self.assertTrue(
                wait_until(lambda: any(m.content == content_a for m in self.host.messages))
            )
            self.assertTrue(
                wait_until(lambda: any(m.content == content_a for m in b.messages))
            )
            msg = next(m for m in a.messages if m.content == content_a)
            a.remove_message(msg.id)
            self.assertTrue(
                wait_until(lambda: all(m.id != msg.id for m in self.host.messages))
            )
            self.assertTrue(
                wait_until(lambda: all(m.id != msg.id for m in b.messages)),
                "authorized delete must be broadcast to all members",
            )
        finally:
            a.stop()
            b.stop()

    def test_delete_unauthorized_not_broadcast(self):
        a = self._join("\u6210\u5458A")
        b = self._join("\u6210\u5458B")
        try:
            content_a = "A \u7684\u6d88\u606f"
            a.send_message(content_a)
            self.assertTrue(
                wait_until(lambda: any(m.content == content_a for m in self.host.messages))
            )
            self.assertTrue(
                wait_until(lambda: any(m.content == content_a for m in b.messages))
            )
            msg = next(m for m in a.messages if m.content == content_a)
            b.remove_message(msg.id)
            time.sleep(0.5)
            self.assertTrue(
                any(m.id == msg.id for m in self.host.messages),
                "host must keep the message after unauthorized delete",
            )
            self.assertTrue(
                any(m.id == msg.id for m in a.messages),
                "unauthorized delete must not be broadcast",
            )
        finally:
            a.stop()
            b.stop()

    def test_disconnect_sync(self):
        client = self._join()
        client.stop()
        self.assertTrue(wait_until(lambda: client.my_id not in self.host.peers))


class SharedPortServerTest(unittest.TestCase):
    """The program-wide single-port host server serves every host group.

    The shared server resolves each group's password via password_lookup
    (wired like the ViewModel); without it every handshake would be rejected.
    """

    PORT = 10030

    def _make_server(self):
        server = network_module.HostGroupServer(self.PORT)
        server.password_lookup = (
            lambda mode, gid: (
                p2p.group_password
                if (p2p := server.resolve_group(gid)) is not None
                else None
            )
        )
        return server

    @staticmethod
    def _port_free(port: int) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", port))
            s.listen(1)
            s.close()
            return True
        except OSError:
            return False

    def test_two_groups_share_one_listener_and_are_isolated(self):
        server = self._make_server()
        g_a = P2PManager(Recorder(), port=self.PORT, password="pa", host_server=server)
        g_a.initialize_as_host("\u7532\u4e3b", "\u7fa4\u7532", password="pa")
        g_a.set_join_id(g_a.numeric_group_id)
        g_a.start_as_host()
        g_b = P2PManager(Recorder(), port=self.PORT, password="pb", host_server=server)
        g_b.initialize_as_host("\u4e59\u4e3b", "\u7fa4\u4e59", password="pb")
        g_b.set_join_id(g_b.numeric_group_id)
        g_b.start_as_host()
        try:
            # query each group through the same port (numeric join id + the
            # group's password in the secured handshake)
            c1 = P2PManager(Recorder(), port=21041)
            c1.initialize_as_client("\u7532\u6210\u5458", "", password="pa")
            c1.set_join_id(g_a.numeric_group_id)
            c1.query_group("127.0.0.1", self.PORT)
            self.assertTrue(wait_until(lambda: c1.queried_group_info is not None or c1.query_error))
            self.assertIsNone(c1.query_error)
            self.assertEqual(c1.queried_group_info.group_name, "\u7fa4\u7532")

            # unknown group on the same port is rejected during the handshake
            c2 = P2PManager(Recorder(), port=21042)
            c2.initialize_as_client("\u8def\u4eba", "\u4e0d\u5b58\u5728\u7684\u7fa4")
            c2.query_group("127.0.0.1", self.PORT)
            self.assertTrue(wait_until(lambda: c2.query_error is not None))
            self.assertIn("\u4e0d\u5b58\u5728\u6b64\u7fa4\u7ec4", c2.query_error)

            # join both groups via the one port
            m1 = P2PManager(Recorder(), port=21043)
            m1.initialize_as_client("\u7532\u6210\u5458", "", password="pa")
            m1.set_join_id(g_a.numeric_group_id)
            m1.confirm_join("127.0.0.1", self.PORT)
            self.assertTrue(wait_until(lambda: m1.connection_result is not None))
            self.assertTrue(m1.connection_result[0])
            self.assertTrue(wait_until(lambda: m1.my_id in g_a.peers))
            m2 = P2PManager(Recorder(), port=21044)
            m2.initialize_as_client("\u4e59\u6210\u5458", "", password="pb")
            m2.set_join_id(g_b.numeric_group_id)
            m2.confirm_join("127.0.0.1", self.PORT)
            self.assertTrue(wait_until(lambda: m2.connection_result is not None))
            self.assertTrue(m2.connection_result[0])
            self.assertTrue(wait_until(lambda: m2.my_id in g_b.peers))

            # groups are isolated: a chat in \\u7fa4\\u7532 never reaches \\u7fa4\\u4e59
            content_a = "\u7532\u7fa4\u7684\u6d88\u606f"
            m1.send_message(content_a)
            self.assertTrue(
                wait_until(lambda: any(m.content == content_a for m in g_a.messages))
            )
            time.sleep(0.3)
            self.assertFalse(any(m.content == content_a for m in g_b.messages))
            self.assertFalse(any(m.content == content_a for m in m2.messages))
            m1.stop()
            m2.stop()
            c1.stop()
            c2.stop()
        finally:
            g_a.stop()
            g_b.stop()
            server.stop()

    def test_server_frees_port_on_shutdown(self):
        server = network_module.HostGroupServer(self.PORT)
        server.password_lookup = lambda mode, gid: None
        g = P2PManager(Recorder(), port=self.PORT, host_server=server)
        g.initialize_as_host("\u4e3b\u673a", "\u5355\u7fa4")
        g.start_as_host()
        self.assertTrue(wait_until(lambda: server.has_groups()))
        # unregistering the last group removes it from the registry; the shared
        # listener itself stays up to keep serving direct member chats
        g.stop()
        self.assertFalse(
            server.has_groups(), "last unregister removes the group registration"
        )
        # an explicit shutdown releases the port
        server.stop()
        self.assertTrue(
            wait_until(lambda: self._port_free(self.PORT), timeout=6.0),
            "port must be released after the server shuts down",
        )


class TombstoneStoreTest(unittest.TestCase):
    """deleted_messages table: record/get/delete tombstones with a per-group
    cap (newest 200 by deleted_at), id dedupe and group isolation."""

    def test_record_get_prune_and_isolation(self):
        store = ChatStore(_fresh_db("lc_test_tombstones.db"))
        try:
            self.assertEqual(store.get_deleted_ids("g1"), [])
            store.record_deleted_messages("g1", ["m1"])
            store.record_deleted_messages("g1", ["m2", "m3"])
            self.assertEqual(sorted(store.get_deleted_ids("g1")), ["m1", "m2", "m3"])
            # re-recording an id keeps a single row
            store.record_deleted_messages("g1", ["m1"])
            self.assertEqual(len(store.get_deleted_ids("g1")), 3)
            # per-group cap: only the newest 200 stay (by deleted_at); the
            # bulk rows get deliberately old timestamps
            for i in range(250):
                store.record_deleted_messages("g1", [f"bulk-{i}"], deleted_at=1000 + i)
            ids = store.get_deleted_ids("g1")
            self.assertEqual(len(ids), ChatStore.TOMBSTONE_CAP)
            self.assertIn("m1", ids)
            self.assertIn("m2", ids)
            self.assertIn("bulk-249", ids)
            self.assertNotIn("bulk-0", ids, "oldest rows must be pruned")
            # groups are isolated
            store.record_deleted_messages("g2", ["x"])
            self.assertEqual(store.get_deleted_ids("g2"), ["x"])
            self.assertEqual(len(store.get_deleted_ids("g1")), ChatStore.TOMBSTONE_CAP)
            # deleting the group clears its tombstones too
            store.delete_group("g2")
            self.assertEqual(store.get_deleted_ids("g2"), [])
        finally:
            store.close()


class ViewModelFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.dbs = [
            _fresh_db("lc_test_a.db"),
            _fresh_db("lc_test_b.db"),
            _fresh_db("lc_test_c.db"),
        ]

    def tearDown(self):
        for vm in getattr(self, "_vms", []):
            vm.shutdown()

    def pump(self):
        self.app.processEvents()

    def test_join_chat_unread_restart(self):
        dba, dbb, dbc = self.dbs
        # dedicated ports: the tests must not collide with a running app
        # instance that may hold the default 9999
        network_module.TCP_PORT = 10031
        host_a = make_vm(dba)
        host_a.create_group("\u4e3b\u673aA", "\u529e\u516c\u5ba4")
        gid_a = host_a.active_group_id
        self.assertIsNotNone(gid_a)
        password_a = host_a.active_group_password
        self.assertTrue(password_a, "host must generate a group password")
        join_id_a = host_a.active_group_numeric_id()
        self.assertTrue(join_id_a, "host must derive a numeric join id")

        member = make_vm(dbc)
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group("\u6210\u5458", join_id_a, "127.0.0.1", password=password_a)
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))
        self.assertTrue(
            wait_until(lambda: len(host_a.active_peers()) == 1, pump=self.pump)
        )

        network_module.TCP_PORT = 10032
        host_b = make_vm(dbb)
        host_b.create_group("\u4e3b\u673aB", "\u4f1a\u8bae\u5ba4")
        gid_b = host_b.active_group_id
        password_b = host_b.active_group_password
        join_id_b = host_b.active_group_numeric_id()
        joined.clear()
        member.query_group(
            "\u6210\u5458", join_id_b, "127.0.0.1", port=10032, password=password_b
        )
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join(port=10032)
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))
        self.assertEqual(len(member.groups_list()), 2)

        msg_a = "\u5728\u529e\u516c\u5ba4\u7684\u6d88\u606f"
        host_a.send_message(msg_a)
        p2p_a = member.group_p2p_map[gid_a]
        self.assertTrue(
            wait_until(
                lambda: any(m.content == msg_a for m in p2p_a.messages),
                pump=self.pump,
            )
        )
        meta_a = next(g for g in member.groups_list() if g.group_id == gid_a)
        self.assertEqual(meta_a.unread_count, 1, "inactive group should get unread")

        member.switch_to_group(gid_a)
        meta_a = next(g for g in member.groups_list() if g.group_id == gid_a)
        self.assertEqual(meta_a.unread_count, 0, "switch should clear unread")
        greeting = "\u56de\u5230\u529e\u516c\u5ba4\u7684\u95ee\u5019"
        member.send_message(greeting)
        self.assertTrue(
            wait_until(
                lambda: any(m.content == greeting for m in host_a.active_messages()),
                pump=self.pump,
            )
        )

        msg_b = "B \u7fa4\u65b0\u6d88\u606f"
        host_b.send_message(msg_b)
        p2p_b = member.group_p2p_map[gid_b]
        self.assertTrue(
            wait_until(lambda: any(m.content == msg_b for m in p2p_b.messages), pump=self.pump)
        )
        meta_b = next(g for g in member.groups_list() if g.group_id == gid_b)
        self.assertEqual(meta_b.unread_count, 1)

        host_a.shutdown()
        host_b.shutdown()
        member.shutdown()

        # the program port stays 10031 across the restart, so the member's
        # persisted host address (10031) still matches when it rejoins
        network_module.TCP_PORT = 10031
        host_a2 = make_vm(dba)
        self.assertTrue(host_a2.can_create_group(), "multiple groups are always allowed")
        host_a2.switch_to_group(gid_a)
        self.assertTrue(host_a2.active_is_host, "host group rebuilds its server")

        member2 = make_vm(dbc)
        member2.switch_to_group(gid_a)
        self.assertTrue(
            wait_until(
                lambda: not member2.rejoin_in_progress and member2.active_peers(),
                pump=self.pump,
            ),
            "client should rejoin after restart",
        )
        after = "\u91cd\u542f\u540e\u8fd8\u80fd\u804a"
        member2.send_message(after)
        self.assertTrue(
            wait_until(
                lambda: any(m.content == after for m in host_a2.active_messages()),
                pump=self.pump,
            )
        )
        self._vms = [host_a2, member2]

    def test_remote_delete_persists_after_restart(self):
        port = 10023  # avoid ports held by other running instances
        network_module.TCP_PORT = port
        dba = _fresh_db("lc_test_delete_host.db")
        dbc = _fresh_db("lc_test_delete_member.db")
        host = make_vm(dba)
        host.create_group("\u4e3b\u673a", "\u5220\u9664\u6d4b\u8bd5")
        gid = host.active_group_id
        password = host.active_group_password
        join_id = host.active_group_numeric_id()

        member = make_vm(dbc)
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group("\u6210\u5458", join_id, "127.0.0.1", port=port, password=password)
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))

        content = "\u6210\u5458\u7684\u6d88\u606f"
        member.send_message(content)
        self.assertTrue(
            wait_until(
                lambda: any(m.content == content for m in host.active_messages()),
                pump=self.pump,
            )
        )
        msg_id = next(m.id for m in member.active_messages() if m.content == content)
        member.delete_message(msg_id)
        self.assertTrue(
            wait_until(
                lambda: all(m.content != content for m in host.active_messages()),
                pump=self.pump,
            ),
            "host must drop the message after authorized remote delete",
        )

        host.shutdown()
        host2 = make_vm(dba)
        host2.switch_to_group(gid)
        saved = host2.store.get_messages_for_group(gid)
        self.assertTrue(
            all(m.content != content for m in saved),
            "deleted message must not resurrect from sqlite after host restart",
        )
        self._vms = [member, host2]

    def test_replay_window_blocks_mirror_delete(self):
        """While the saved-history replay window is open, a stale in-flight
        callback must not mirror-delete persisted rows (Android parity:
        replayDone[groupId] = p2p)."""
        network_module.TCP_PORT = 10046
        vm = make_vm(_fresh_db("lc_test_replay_guard.db"))
        self._vms = [vm]
        vm.create_group("\u4e3b\u673a", "\u91cd\u653e\u5b88\u536b")
        gid = vm.active_group_id
        p2p = vm.group_p2p_map[gid]
        msg = p2p.send_message("\u5b58\u6d3b\u6d88\u606f")
        self.assertIsNotNone(msg)
        self.assertTrue(any(m.id == msg.id for m in vm.store.get_messages_for_group(gid)))
        # open the replay window, then a stale callback reports the message gone
        vm.replay_done.pop(gid, None)
        p2p.messages.clear()
        vm.messages_changed(p2p)
        self.assertTrue(
            any(m.id == msg.id for m in vm.store.get_messages_for_group(gid)),
            "mirror delete must be blocked while the replay window is open",
        )
        self.assertIn(msg.id, vm.persisted_message_ids[gid])
        # once the replay completes, the same state change mirrors normally
        vm.replay_done[gid] = p2p
        vm.messages_changed(p2p)
        self.assertTrue(all(m.id != msg.id for m in vm.store.get_messages_for_group(gid)))
        self.assertIn(msg.id, vm.store.get_deleted_ids(gid))

    def test_stale_connection_cannot_mirror_delete(self):
        """A replaced P2PManager (old connection of the same group) must never
        mirror-delete from its own message list — not mid-replay of the fresh
        instance and not after it completed one. Otherwise a late callback from
        the dying connection wipes rows the fresh instance still holds and
        mints delete tombstones that make every member drop the message."""
        network_module.TCP_PORT = 10047
        vm = make_vm(_fresh_db("lc_test_stale_mirror.db"))
        self._vms = [vm]
        vm.create_group("\u4e3b\u673a", "\u65e7\u8fde\u63a5")
        gid = vm.active_group_id
        stale = vm.group_p2p_map[gid]
        msg = stale.send_message("\u5b58\u6d3b\u6d88\u606f")
        self.assertIsNotNone(msg)

        # a reconnect replaces the manager for the same group id
        fresh = P2PManager(
            vm,
            port=vm.port,
            device_id=vm._device_id(),
            hardware_id=vm._hardware_fingerprint(),
        )
        fresh.initialize_as_host(vm.nickname, "\u65e7\u8fde\u63a5", "")
        vm.group_p2p_map[gid] = fresh
        vm.persisted_message_ids[gid] = {msg.id}

        # the fresh instance has NOT finished its replay yet: a stale callback
        # must not mirror (and must not mint tombstones)
        stale.messages.clear()
        vm.messages_changed(stale)
        self.assertTrue(
            any(m.id == msg.id for m in vm.store.get_messages_for_group(gid)),
            "a stale callback must not mirror-delete mid-replay",
        )
        self.assertEqual(vm.store.get_deleted_ids(gid), [])

        # even after the fresh instance completed its replay, the stale one is
        # still not the live manager: still no mirror delete
        vm.replay_done[gid] = fresh
        vm.messages_changed(stale)
        self.assertTrue(
            any(m.id == msg.id for m in vm.store.get_messages_for_group(gid)),
            "a replaced manager must never mirror-delete",
        )
        self.assertEqual(vm.store.get_deleted_ids(gid), [])
        fresh.stop()

    def test_rejoin_without_numeric_id_fails_clearly(self):
        """A member group saved without its numeric join id cannot be rejoined
        (a host resolves handshakes by numeric id only, never by name): the
        attempt must fail fast with an actionable message instead of dialing
        with the group name that the host would reject."""
        network_module.TCP_PORT = 10048
        os.makedirs(DATA_DIR, exist_ok=True)
        store = ChatStore(_fresh_db("lc_test_rejoin_no_id.db"))
        # the row must exist BEFORE the VM loads the group list, and it must
        # carry no remembered join id
        store.upsert_group(SavedGroup(
            group_id="legacy-group-id",
            group_name="\u65e0ID\u7fa4",
            is_host=False,
            host_ip="127.0.0.1",
            host_port=1,
            my_name="\u6211",
        ))
        vm = ChatViewModel(store, data_dir=DATA_DIR)
        self._vms = [vm]
        statuses = []
        vm.status_message.connect(lambda s: statuses.append(s))

        vm.switch_to_group("legacy-group-id")

        self.assertTrue(vm.rejoin_failed, "the rejoin must fail fast")
        self.assertFalse(vm.rejoin_in_progress)
        self.assertIsNone(vm.pending_p2p, "no dial may be attempted")
        self.assertTrue(
            any("\u6570\u5b57 ID" in s for s in statuses),
            "the failure must tell the user to re-query the numeric id",
        )

    def test_tombstone_converges_on_rejoin(self):
        """A member that was offline during a delete must not resurrect the
        message after rejoining: join_ack carries deletedIds, the member
        drops its copy and records the tombstone locally."""
        port = 10041
        network_module.TCP_PORT = port
        dba = _fresh_db("lc_test_ts_host.db")
        dbc = _fresh_db("lc_test_ts_member.db")
        host = make_vm(dba)
        host.create_group("\u4e3b\u673a", "\u5893\u7891")
        gid = host.active_group_id
        password = host.active_group_password
        join_id = host.active_group_numeric_id()

        member = make_vm(dbc)
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group("\u6210\u5458", join_id, "127.0.0.1", port=port, password=password)
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))
        content = "\u5c06\u88ab\u5220\u9664\u7684\u6d88\u606f"
        host.send_message(content)
        member_p2p = member.group_p2p_map[gid]
        self.assertTrue(
            wait_until(
                lambda: any(m.content == content for m in member_p2p.messages),
                pump=self.pump,
            )
        )
        msg_id = next(m.id for m in member_p2p.messages if m.content == content)
        # the member goes offline, then the delete happens
        member.shutdown()
        host.delete_message(msg_id)
        self.assertTrue(
            wait_until(
                lambda: all(m.content != content for m in host.active_messages()),
                pump=self.pump,
            )
        )
        self.assertIn(msg_id, host.store.get_deleted_ids(gid), "host must record the tombstone")

        # the member comes back: the join_ack deletedIds must converge it
        member2 = make_vm(dbc)
        self._vms = [member2, host]
        member2.switch_to_group(gid)
        self.assertTrue(
            wait_until(
                lambda: member2.group_p2p_map.get(gid) is not None
                and not member2.rejoin_in_progress
                and member2.active_peers(),
                pump=self.pump,
            ),
            "member should rejoin",
        )
        time.sleep(0.3)  # let any late tombstone application land
        p2p2 = member2.group_p2p_map[gid]
        self.assertTrue(
            all(m.id != msg_id for m in p2p2.messages),
            "the deleted message must not resurrect in memory after rejoin",
        )
        saved = member2.store.get_messages_for_group(gid)
        self.assertTrue(
            all(m.id != msg_id for m in saved),
            "the deleted message must not resurrect from sqlite after rejoin",
        )
        self.assertIn(
            msg_id, member2.store.get_deleted_ids(gid),
            "the member must record the tombstone locally",
        )

    def test_create_group_persists_is_host(self):
        network_module.TCP_PORT = 10006
        dba = _fresh_db("lc_test_create_host_flag.db")
        vm = make_vm(dba)
        vm.create_group("\u4e3b\u673a", "\u65b0\u7fa4")
        gid = vm.active_group_id
        sg = vm.store.get_group(gid)
        self.assertTrue(sg.is_host, "newly created host group must persist is_host=True")
        self.assertEqual(
            sg.host_port, network_module.TCP_PORT, "new host group must persist its port"
        )
        self.assertTrue(vm.can_create_group(), "creating more groups stays allowed")
        # after a restart the group must still be re-hostable on the same port
        vm.shutdown()
        vm2 = make_vm(dba)
        meta = next(g for g in vm2.groups_list() if g.group_id == gid)
        self.assertTrue(meta.is_host)
        self.assertEqual(meta.host_port, network_module.TCP_PORT, "restart must keep the host port")
        vm2.switch_to_group(gid)
        self.assertTrue(vm2.active_is_host, "re-entering a persisted host group re-hosts")
        self.assertEqual(
            vm2.group_p2p_map[gid].port,
            network_module.TCP_PORT,
            "re-host must reuse the persisted port",
        )
        self._vms = [vm2]

    def test_multiple_host_groups_share_one_port(self):
        """The whole program uses ONE port: every host group is served by the
        shared listener and reachable through the same address."""
        network_module.TCP_PORT = 10009
        dba = _fresh_db("lc_test_multi_host.db")
        vm = make_vm(dba)
        vm.create_group("\u4e3b\u673a", "\u7fa4\u7532")
        gid_a = vm.active_group_id
        vm.create_group("\u4e3b\u673a", "\u7fa4\u4e59")
        gid_b = vm.active_group_id
        self.assertNotEqual(gid_a, gid_b)
        self.assertTrue(vm.can_create_group(), "creating two host groups must be allowed")
        base = network_module.TCP_PORT
        self.assertEqual(vm.group_p2p_map[gid_a].port, base)
        self.assertEqual(vm.group_p2p_map[gid_b].port, base, "both groups share the one port")
        self.assertTrue(vm.group_p2p_map[gid_a].is_host)
        self.assertTrue(vm.group_p2p_map[gid_b].is_host)
        sg_b = vm.store.get_group(gid_b)
        self.assertEqual(sg_b.host_port, base, "host groups persist the shared port")
        # both groups are reachable through the single listener (the shared
        # server resolves each group's password for the handshake)
        p2p_b = vm.group_p2p_map[gid_b]
        c = P2PManager(Recorder(), port=base)
        c.initialize_as_client("\u6210\u5458", "", password=vm.active_group_password)
        c.set_join_id(p2p_b.numeric_group_id)
        c.query_group("127.0.0.1", base)
        self.assertTrue(wait_until(lambda: c.queried_group_info is not None or c.query_error))
        self.assertIsNone(c.query_error)
        self.assertEqual(c.queried_group_info.group_name, "\u7fa4\u4e59")
        c.stop()
        self._vms = [vm]

    def test_join_group_on_shared_port(self):
        port = 10008
        network_module.TCP_PORT = port
        dba = _fresh_db("lc_test_multi_join_host.db")
        dbc = _fresh_db("lc_test_multi_join_member.db")
        host = make_vm(dba)
        host.create_group("\u4e3b\u673a", "\u7fa4\u7532")
        host.create_group("\u4e3b\u673a", "\u7fa4\u4e59")
        gid_b = host.active_group_id
        self.assertEqual(
            host.group_p2p_map[gid_b].port, port, "both groups share the one port"
        )
        join_id_b = host.active_group_numeric_id()

        member = make_vm(dbc)
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group(
            "\u6210\u5458", join_id_b, "127.0.0.1", port=port,
            password=host.active_group_password,
        )
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))
        self.assertTrue(
            wait_until(lambda: len(host.active_peers()) == 1, pump=self.pump),
            "host of \\u7fa4\\u4e59 must see the member",
        )
        # the member's persisted group keeps the host port for later rejoins
        sg = member.store.get_group(gid_b)
        self.assertEqual(sg.host_port, port, "client must persist the host port")
        # and re-enter/rejoin uses it
        member.leave_active_group()
        member.switch_to_group(gid_b)
        self.assertTrue(
            wait_until(
                lambda: member.active_peers() and not member.rejoin_in_progress,
                pump=self.pump,
            ),
            "client should rejoin \\u7fa4\\u4e59 on the shared port",
        )
        self._vms = [host, member]

    def test_leave_host_group_keeps_and_rehosts_same_port(self):
        network_module.TCP_PORT = 10010
        dba = _fresh_db("lc_test_leave_host.db")
        vm = make_vm(dba)
        vm.create_group("\u4e3b\u673a", "\u9000\u7fa4\u6d4b\u8bd5")
        gid = vm.active_group_id
        port = vm.group_p2p_map[gid].port
        vm.leave_active_group()
        self.assertTrue(
            any(g.group_id == gid for g in vm.groups_list()),
            "leaving must keep the group in the list",
        )
        self.assertTrue(
            vm.can_create_group(), "leaving must not block creating new groups"
        )
        # re-entering re-hosts the old group on the same port
        vm.switch_to_group(gid)
        self.assertTrue(vm.active_is_host, "re-entering a host group re-hosts")
        self.assertEqual(
            vm.group_p2p_map[gid].port, port, "re-host must reuse the same port"
        )
        self._vms = [vm]

    def test_leave_client_group_keeps_group_and_rejoins(self):
        port = 10007
        network_module.TCP_PORT = port
        dba = _fresh_db("lc_test_leave_client_host.db")
        dbc = _fresh_db("lc_test_leave_client_member.db")
        host = make_vm(dba)
        host.create_group("\u4e3b\u673a", "\u9000\u7fa4\u6d4b\u8bd5")
        gid = host.active_group_id
        password = host.active_group_password
        join_id = host.active_group_numeric_id()

        member = make_vm(dbc)
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group("\u6210\u5458", join_id, "127.0.0.1", port=port, password=password)
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))
        self.assertTrue(
            wait_until(lambda: len(host.active_peers()) == 1, pump=self.pump)
        )

        member.leave_active_group()
        self.assertTrue(
            any(g.group_id == gid for g in member.groups_list()),
            "leaving must keep the group in the list",
        )
        meta = next(g for g in member.groups_list() if g.group_id == gid)
        self.assertFalse(meta.connected, "group must show as disconnected after leave")
        self.assertTrue(
            wait_until(lambda: len(host.active_peers()) == 0, pump=self.pump),
            "host must drop the leaver from its peer list",
        )

        # re-entering rejoins
        member.switch_to_group(gid)
        self.assertTrue(
            wait_until(
                lambda: member.active_peers() and not member.rejoin_in_progress,
                pump=self.pump,
            ),
            "client should rejoin after leaving",
        )
        self._vms = [host, member]

    def test_host_bind_failure_keeps_group_and_retry(self):
        port = 10020
        network_module.TCP_PORT = port
        dba = _fresh_db("lc_test_host_retry.db")
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("0.0.0.0", port))
        blocker.listen(1)
        vm = make_vm(dba)
        self._vms = [vm]  # ensure shutdown even if an assertion below fails
        try:
            vm.create_group("\u4e3b\u673a", "\u91cd\u8bd5\u6d4b\u8bd5")
            gid = vm.active_group_id
            self.assertTrue(
                any(g.group_id == gid for g in vm.groups_list()),
                "bind failure must keep the group in the list",
            )
            self.assertIsNotNone(
                vm.store.get_group(gid),
                "bind failure must not delete the group from storage",
            )
            # server_error is set asynchronously by the server thread; poll for it
            self.assertTrue(
                wait_until(lambda: bool(vm.active_server_error()), pump=self.pump),
                "server error must be visible after bind failure",
            )
        finally:
            blocker.close()

        vm.retry_host_listening()
        self.assertTrue(
            wait_until(
                lambda: vm.active_server_error() is None and self._port_open(port),
                pump=self.pump,
            ),
            "retry must restart the server once the port is free",
        )
        member = make_vm(_fresh_db("lc_test_host_retry_member.db"))
        self._vms.append(member)
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group(
            "\u6210\u5458", vm.active_group_numeric_id(), "127.0.0.1", port=port,
            password=vm.active_group_password,
        )
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(
            wait_until(lambda: joined, pump=self.pump),
            "a member must be able to join the group after retry",
        )

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            s.close()
            return True
        except OSError:
            return False

    def test_file_offer_download_and_persistence(self):
        port = 10021
        network_module.TCP_PORT = port
        dba = _fresh_db("lc_test_file_host.db")
        dbc = _fresh_db("lc_test_file_member.db")
        host = make_vm(dba)
        host.create_group("\u4e3b\u673a", "\u6587\u4ef6\u6d4b\u8bd5")
        gid = host.active_group_id
        password = host.active_group_password
        join_id = host.active_group_numeric_id()

        member = make_vm(dbc)
        self._vms = [host, member]
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group("\u6210\u5458", join_id, "127.0.0.1", port=port, password=password)
        self.assertTrue(wait_until(lambda: member.queried_group_info() is not None, pump=self.pump))
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))

        payload = b"view-model file payload " * 500
        fpath = os.path.join(tempfile.gettempdir(), "kilo", "lc_test_send.dat")
        os.makedirs(os.path.dirname(fpath), exist_ok=True)
        with open(fpath, "wb") as f:
            f.write(payload)

        self.assertTrue(member.send_file(fpath), "send_file must succeed")
        self.assertTrue(
            wait_until(
                lambda: any(m.file_info is not None for m in host.active_messages()),
                pump=self.pump,
            ),
            "host must receive the file offer",
        )
        offer = next(m for m in host.active_messages() if m.file_info is not None)
        self.assertEqual(offer.file_info.file_name, "lc_test_send.dat")
        self.assertEqual(offer.file_info.file_size, len(payload))

        # download from the host side via the offer (sender is the member)
        target = os.path.join(tempfile.gettempdir(), "kilo", "lc_test_received.dat")
        if os.path.exists(target):
            os.remove(target)
        done = []
        host.file_download_finished.connect(
            lambda fid, ok, message: done.append((ok, message))
        )
        host.download_file(offer.id, target)
        self.assertTrue(wait_until(lambda: done, pump=self.pump), "download must finish")
        ok, message = done[0]
        self.assertTrue(ok, message)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), payload, "downloaded bytes must match")

        # the offer must survive a restart with its file metadata intact
        member.shutdown()
        member2 = make_vm(dbc)
        self._vms.append(member2)
        member2.switch_to_group(gid)
        self.assertTrue(
            wait_until(
                lambda: any(m.file_info is not None for m in member2.active_messages()),
                pump=self.pump,
            ),
            "file message metadata must survive restart",
        )
        restored = next(m for m in member2.active_messages() if m.file_info is not None)
        self.assertEqual(restored.file_info.file_name, "lc_test_send.dat")
        self.assertEqual(restored.file_info.file_size, len(payload))
        for p in (fpath, target):
            if os.path.exists(p):
                os.remove(p)

    def test_folder_protocol_fields_and_traversal_safety(self):
        """Folder offers reuse FileInfo with optional folderId/folderName/
        relativePath/folderTotal. A crafted relative path must never escape the
        destination directory, a crafted entry count must not be trusted, and a
        plain file offer must stay byte-identical (fields omitted)."""
        from localchat.models import FileInfo, sanitize_relative_path

        self.assertEqual(sanitize_relative_path("../../etc/passwd"), "etc/passwd")
        self.assertEqual(sanitize_relative_path("..\\..\\win.ini"), "win.ini")
        self.assertEqual(sanitize_relative_path("/abs/x.txt"), "abs/x.txt")
        self.assertEqual(sanitize_relative_path("dir/sub/f.txt"), "dir/sub/f.txt")
        self.assertEqual(sanitize_relative_path(".."), "")
        self.assertEqual(sanitize_relative_path(""), "")

        plain = FileInfo("id", "f.txt", 3, "h", 1)
        self.assertNotIn("folderId", plain.to_dict())
        self.assertNotIn("relativePath", plain.to_dict())

        fi = FileInfo(
            "id",
            "f.txt",
            3,
            "h",
            1234,
            folder_id="abc",
            folder_name="dir",
            relative_path="sub/f.txt",
            folder_total=2,
        )
        d = fi.to_dict()
        self.assertEqual(d["folderId"], "abc")
        self.assertEqual(d["folderName"], "dir")
        self.assertEqual(d["relativePath"], "sub/f.txt")
        self.assertEqual(d["folderTotal"], 2)
        back = FileInfo.from_dict(d)
        self.assertEqual(back.folder_id, "abc")
        self.assertEqual(back.folder_name, "dir")
        self.assertEqual(back.relative_path, "sub/f.txt")
        self.assertEqual(back.folder_total, 2)

        crafted = FileInfo.from_dict(
            {
                "fileId": "x",
                "fileName": "f.txt",
                "fileSize": 1,
                "downloadHost": "h",
                "downloadPort": 1,
                "folderId": "a",
                "relativePath": "../../evil.txt",
                "folderTotal": 999999,
            }
        )
        self.assertEqual(crafted.relative_path, "evil.txt")
        self.assertEqual(crafted.folder_total, 0)

    def test_folder_offer_grouping_and_download(self):
        """End to end: a folder is offered as one file_message per entry with
        shared folder metadata; the receiver downloads every entry into
        <dest>/<folderName>/ preserving the tree."""
        port = 10049
        network_module.TCP_PORT = port
        host = make_vm(_fresh_db("lc_folder_host.db"))
        host.create_group("\u4e3b\u673a", "\u6587\u4ef6\u5939\u6d4b\u8bd5")
        password = host.active_group_password
        join_id = host.active_group_numeric_id()
        member = make_vm(_fresh_db("lc_folder_member.db"))
        self._vms = [host, member]

        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group("\u6210\u5458", join_id, "127.0.0.1", port=port, password=password)
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))

        root = os.path.join(tempfile.gettempdir(), "kilo", "lc_folder_src")
        if os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(os.path.join(root, "sub"))
        files = {"a.txt": b"alpha", "sub/b.bin": b"beta" * 100}
        for rel, data in files.items():
            with open(os.path.join(root, *rel.split("/")), "wb") as f:
                f.write(data)

        # send_folder is async (worker thread): wait for its completion signal
        send_done = []
        member.folder_send_finished.connect(lambda ok: send_done.append(ok))
        member.send_folder(root)
        self.assertTrue(wait_until(lambda: send_done, pump=self.pump))
        self.assertTrue(send_done[0], "send_folder must succeed")

        def offers():
            return [
                m for m in host.active_messages() if m.file_info and m.file_info.folder_id
            ]

        self.assertTrue(wait_until(lambda: len(offers()) >= 2, pump=self.pump))
        entries = offers()
        folder_ids = {m.file_info.folder_id for m in entries}
        self.assertEqual(len(folder_ids), 1, "all entries share one folderId")
        meta = entries[0].file_info
        self.assertEqual(meta.folder_name, "lc_folder_src")
        self.assertEqual(meta.folder_total, 2)
        self.assertEqual(
            sorted(m.file_info.relative_path for m in entries), ["a.txt", "sub/b.bin"]
        )

        dest = os.path.join(tempfile.gettempdir(), "kilo", "lc_folder_dest")
        if os.path.isdir(dest):
            shutil.rmtree(dest)
        os.makedirs(dest)
        done = []
        host.folder_download_finished.connect(lambda fid, ok, msg: done.append((ok, msg)))
        host.download_folder(folder_ids.pop(), dest)
        self.assertTrue(wait_until(lambda: done, timeout=20.0, pump=self.pump))
        ok, message = done[0]
        self.assertTrue(ok, message)

        out_root = os.path.join(dest, "lc_folder_src")
        for rel, data in files.items():
            path = os.path.join(out_root, *rel.split("/"))
            self.assertTrue(os.path.isfile(path), path)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), data)

    def test_collect_folder_entries_reports_truncation(self):
        """_collect_folder_entries caps at MAX_FOLDER_FILES and reports (not
        silently hides) that sendable files existed beyond the cap."""
        from localchat.models import MAX_FOLDER_FILES

        root = os.path.join(tempfile.gettempdir(), "kilo", "lc_folder_cap")
        if os.path.isdir(root):
            shutil.rmtree(root)
        os.makedirs(root)
        total = MAX_FOLDER_FILES + 2
        for i in range(total):
            with open(os.path.join(root, f"f{i:05d}.txt"), "wb") as f:
                f.write(b"x")
        entries, folder_name, truncated = ChatViewModel._collect_folder_entries(root)
        self.assertEqual(len(entries), MAX_FOLDER_FILES)
        self.assertTrue(truncated)
        self.assertEqual(folder_name, "lc_folder_cap")

        # exactly at the cap: complete, not truncated
        os.remove(os.path.join(root, f"f{total - 1:05d}.txt"))
        os.remove(os.path.join(root, f"f{total - 2:05d}.txt"))
        entries, _, truncated = ChatViewModel._collect_folder_entries(root)
        self.assertEqual(len(entries), MAX_FOLDER_FILES)
        self.assertFalse(truncated)

    def test_confirm_join_dialog_constructs_without_crash(self):
        """Regression: ConfirmJoinDialog.__init__ used to call set_loading()
        before confirm_btn/cancel_btn existed, crashing the app from inside a
        Qt slot whenever a user joined a group."""
        from localchat.models import GroupInfo
        from localchat.ui.setup_page import ConfirmJoinDialog

        vm = make_vm(_fresh_db("lc_ui_confirm.db"))
        self._vms = [vm]
        dlg = ConfirmJoinDialog(
            vm, GroupInfo("\u7fa4", "\u521b\u5efa\u8005", "creator-id", 1)
        )
        self.assertIsNotNone(dlg.confirm_btn)
        self.assertIsNotNone(dlg.cancel_btn)
        self.assertFalse(dlg.confirm_btn.isEnabled() is None)
        dlg.set_loading(True)
        self.assertFalse(dlg.confirm_btn.isEnabled())
        dlg.close()

    def test_chat_page_paints_file_message_without_crash(self):
        """Regression: MessageDelegate._paint_file_message used to reference an
        undefined size_text, crashing the app when a file message was shown."""
        from localchat.models import ChatMessage, FileInfo
        from localchat.ui.main_window import MainWindow

        network_module.TCP_PORT = 10011
        vm = make_vm(_fresh_db("lc_ui_paint.db"))
        self._vms = [vm]
        win = MainWindow(vm)
        vm.create_group("\u4e3b\u673a", "\u6e32\u67d3\u6d4b\u8bd5")
        p2p = vm.group_p2p_map[vm.active_group_id]
        fi = FileInfo("f1", "\u6d4b\u8bd5\u62a5\u544a.pdf", 2048, "192.168.1.5", 42001)
        p2p.messages.append(
            ChatMessage(
                "f1", "\u6d4b\u8bd5\u62a5\u544a.pdf", 1700000000000,
                "someone", "\u5f20\u4e09", file_info=fi,
            )
        )
        vm.active_messages_changed.emit()
        win._go_chat()
        self.pump()
        page = win.pages[3]
        # force the delegate to paint the file card
        page.list_view.viewport().update()
        page.list_view.repaint()
        self.pump()
        self.assertGreaterEqual(page.model.rowCount(), 1)
        win.close()

    def test_chat_page_groups_folder_messages_into_one_card(self):
        """Folder entries sharing a folderId collapse into a single folder row
        (ordered by relative_path) and painting it must not crash."""
        from localchat.models import ChatMessage, FileInfo
        from localchat.ui.chat_page import (
            MSG_ROLE,
            ChatPage,
            FolderGroup,
            iter_message_rows,
        )

        network_module.TCP_PORT = 10050
        vm = make_vm(_fresh_db("lc_ui_folder.db"))
        self._vms = [vm]
        vm.create_group("\u4e3b\u673a", "\u6587\u4ef6\u5939\u6e32\u67d3")
        p2p = vm.group_p2p_map[vm.active_group_id]
        for index, (rel, size) in enumerate((("a.txt", 10), ("sub/b.bin", 20))):
            fi = FileInfo(
                "f%d" % index,
                rel.split("/")[-1],
                size,
                "192.168.1.5",
                42001,
                folder_id="fold1",
                folder_name="bundle",
                relative_path=rel,
                folder_total=2,
            )
            p2p.messages.append(
                ChatMessage(
                    "f%d" % index,
                    rel.split("/")[-1],
                    1700000000000 + index,
                    "someone",
                    "\u5f20\u4e09",
                    file_info=fi,
                )
            )

        rows = iter_message_rows(p2p.messages)
        self.assertEqual([kind for kind, _ in rows].count("folder"), 1)
        group = next(payload for kind, payload in rows if kind == "folder")
        self.assertIsInstance(group, FolderGroup)
        self.assertEqual(group.total, 2)
        self.assertEqual(
            [e.file_info.relative_path for e in group.entries], ["a.txt", "sub/b.bin"]
        )

        page = ChatPage(vm, lambda: None)
        vm.active_messages_changed.emit()
        self.pump()
        # one date header + exactly one folder card
        self.assertEqual(page.model.rowCount(), 2)
        page.list_view.viewport().update()
        page.list_view.repaint()
        self.pump()
        rendered = page.model.item(1).data(MSG_ROLE)
        self.assertIsInstance(rendered, FolderGroup)
        self.assertEqual(rendered.folder_id, "fold1")
        self.assertEqual(rendered.total, 2)
        page.deleteLater()

    @staticmethod
    def _drag_enter(widget, mime):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QDragEnterEvent

        event = QDragEnterEvent(
            QPointF(5, 5).toPoint(),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        widget.dragEnterEvent(event)
        return event.isAccepted()

    @staticmethod
    def _drop(widget, mime):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QDropEvent

        event = QDropEvent(
            QPointF(5, 5),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        widget.dropEvent(event)
        return event.isAccepted()

    def test_input_accepts_dropped_files_and_sends(self):
        """Dropping image/file(s)/folder(s) onto a chat input offers them over
        the same path as the paperclip picker; remote URLs are ignored.
        Regression for the drag-and-drop entry point, which had no handler
        before."""
        from PyQt6.QtCore import QMimeData, QUrl
        from PyQt6.QtGui import QDragLeaveEvent
        from localchat.ui.chat_page import ChatPage
        from localchat.ui.direct_chat_page import DirectChatPage

        network_module.TCP_PORT = 10048
        vm = make_vm(_fresh_db("lc_input_drop.db"))
        self._vms = [vm]
        vm.create_group("\u4e3b\u673a", "\u62d6\u62fd\u6d4b\u8bd5")

        tmpdir = tempfile.mkdtemp(prefix="lc_drop_")
        img = os.path.join(tmpdir, "pic.png")
        doc = os.path.join(tmpdir, "notes.txt")
        for path in (img, doc):
            with open(path, "wb") as f:
                f.write(b"payload")
        folder = os.path.join(tmpdir, "bundle")
        os.makedirs(folder)
        with open(os.path.join(folder, "inner.txt"), "wb") as f:
            f.write(b"inner")

        group_calls = []
        folder_calls = []
        vm.send_file = lambda path: group_calls.append(path) or True
        vm.send_folder = lambda path: folder_calls.append(path) or True
        page = ChatPage(vm, lambda: None)

        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(img), QUrl.fromLocalFile(doc)])
        self.assertTrue(self._drag_enter(page.input_edit, mime))
        self.assertTrue(page.input_edit.property("dragActive"))
        page.input_edit.dragLeaveEvent(QDragLeaveEvent())
        self.assertFalse(page.input_edit.property("dragActive"))
        self._drop(page.input_edit, mime)
        self.assertFalse(page.input_edit.property("dragActive"))
        self.assertEqual(group_calls, [img, doc])

        folder_mime = QMimeData()
        folder_mime.setUrls([QUrl.fromLocalFile(folder)])
        self.assertTrue(
            self._drag_enter(page.input_edit, folder_mime),
            "a dropped directory must be accepted",
        )
        self._drop(page.input_edit, folder_mime)
        self.assertEqual(folder_calls, [folder])
        self.assertEqual(group_calls, [img, doc])

        remote = QMimeData()
        remote.setUrls([QUrl("https://example.com/pic.png")])
        self._drop(page.input_edit, remote)
        self.assertEqual(group_calls, [img, doc], "remote URLs must not be sent")
        page.deleteLater()

        direct_calls = []
        direct_folder_calls = []
        vm.send_direct_file = (
            lambda peer_id, path: direct_calls.append((peer_id, path)) or True
        )
        vm.send_direct_folder = (
            lambda peer_id, path: direct_folder_calls.append((peer_id, path)) or True
        )
        dpage = DirectChatPage(vm, lambda: None)
        dpage._peer_id = "dev-1"
        dmime = QMimeData()
        dmime.setUrls([QUrl.fromLocalFile(img), QUrl.fromLocalFile(folder)])
        self._drop(dpage.input_edit, dmime)
        self.assertEqual(direct_calls, [("dev-1", img)])
        self.assertEqual(direct_folder_calls, [("dev-1", folder)])
        dpage.deleteLater()

    def test_tray_aggregation_flushes_on_group_change(self):
        """A gid change inside the burst window flushes the old bubble first:
        counts/previews from one group must never bleed into another's."""
        vm = make_vm(_fresh_db("lc_test_tray_gid_flush.db"))
        self._vms = [vm]
        vm.set_window_active(False)
        vm.TRAY_AGGREGATE_MS = 80
        received = []
        vm.tray_notification.connect(lambda gid, title, body: received.append((gid, title, body)))
        vm._on_raw_tray("g1", "\u5f20\u4e09", "\u7b2c\u4e00\u6761")
        vm._on_raw_tray("g2", "\u674e\u56db", "\u7b2c\u4e8c\u6761")
        self.assertTrue(
            wait_until(lambda: received, timeout=2.0, pump=self.pump),
            "the old group's bubble must flush when another group arrives",
        )
        self.assertEqual(
            received[0],
            ("g1", "\u5f20\u4e09", "\u7b2c\u4e00\u6761"),
            "the flushed bubble must carry the OLD group's data",
        )
        self.assertTrue(
            wait_until(lambda: len(received) >= 2, timeout=2.0, pump=self.pump)
        )
        self.assertEqual(received[1][0], "g2")
        self.assertEqual(
            received[1][1],
            "\u674e\u56db",
            "the new group's bubble must start fresh (count 1)",
        )

    def test_tray_notification_aggregation(self):
        vm = make_vm(_fresh_db("lc_test_tray_agg.db"))
        self._vms = [vm]
        vm.set_window_active(False)
        vm.TRAY_AGGREGATE_MS = 80  # shorten the burst window for the test
        received = []
        vm.tray_notification.connect(lambda gid, title, body: received.append((gid, title, body)))

        # two notifications within the burst window merge into one bubble
        first = "\u7b2c\u4e00\u6761"
        second = "\u7b2c\u4e8c\u6761"
        vm._on_raw_tray("g1", "\u5f20\u4e09", first)
        vm._on_raw_tray("g1", "\u674e\u56db", second)
        self.assertTrue(
            wait_until(lambda: received, timeout=2.0, pump=self.pump),
            "aggregated tray notification must fire after the burst window",
        )
        self.assertEqual(len(received), 1, "burst notifications must be merged into one")
        gid, title, body = received[0]
        self.assertEqual(gid, "g1")
        self.assertEqual(title, "\u674e\u56db \u7b49 2 \u6761\u65b0\u6d88\u606f")
        self.assertEqual(body, second)

        # a later notification outside the window fires its own bubble
        received.clear()
        vm._on_raw_tray("g2", "\u738b\u4e94", "\u7b2c\u4e09\u6761")
        self.assertTrue(
            wait_until(lambda: received, timeout=2.0, pump=self.pump)
        )
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][1], "\u738b\u4e94")

    def test_current_group_tray_when_window_inactive(self):
        """An incoming message in the group the user has OPEN but cannot see
        (window minimized/hidden) must still pop a tray bubble; unread stays 0
        for the current group (Android parity: notifyNewMessages covers the
        active group when the app is not foreground)."""
        from localchat.models import ChatMessage

        network_module.TCP_PORT = 10047
        vm = make_vm(_fresh_db("lc_test_tray_current.db"))
        self._vms = [vm]
        vm.create_group("\u4e3b\u673a", "\u5f53\u524d\u7fa4")
        gid = vm.active_group_id
        self.assertIsNotNone(gid)
        p2p = vm.group_p2p_map[gid]
        vm.TRAY_AGGREGATE_MS = 80
        received = []
        vm.tray_notification.connect(lambda g, t, b: received.append((g, t, b)))

        # window hidden: a new incoming message in the OPEN group notifies
        vm.set_window_active(False)
        p2p.messages.append(
            ChatMessage(
                "m-cur-1",
                "\u5f53\u524d\u7fa4\u65b0\u6d88\u606f",
                1700000000000,
                "peer-1",
                "\u5f20\u4e09",
            )
        )
        vm.messages_changed(p2p)
        self.assertTrue(
            wait_until(lambda: received, timeout=2.0, pump=self.pump),
            "the open group must notify while the window is hidden",
        )
        self.assertEqual(received[-1][0], gid)
        meta = next(g for g in vm.groups_list() if g.group_id == gid)
        self.assertEqual(meta.unread_count, 0, "current group keeps unread 0")

        # window active again: no bubble for the open group
        received.clear()
        vm.set_window_active(True)
        p2p.messages.append(
            ChatMessage(
                "m-cur-2",
                "\u7b2c\u4e8c\u6761",
                1700000001000,
                "peer-1",
                "\u5f20\u4e09",
            )
        )
        vm.messages_changed(p2p)
        self.pump()
        self.assertIsNone(vm._tray_accum, "active window must not queue a bubble")
        self.assertEqual(received, [])

    def test_direct_tray_when_window_inactive(self):
        """A 1:1 message received while the window is hidden pops a tray
        bubble keyed "direct:<peerId>" with the contact name as sender; own
        messages stay silent."""
        from localchat.models import ChatMessage

        network_module.TCP_PORT = 10048
        vm = make_vm(_fresh_db("lc_test_tray_direct.db"))
        self._vms = [vm]
        vm.direct.add_contact(Peer("peer-9", "\u5c0f\u674e", "127.0.0.1", 9))
        vm.TRAY_AGGREGATE_MS = 80
        vm.set_window_active(False)
        received = []
        vm.tray_notification.connect(lambda g, t, b: received.append((g, t, b)))

        vm.direct.seed_messages(
            "peer-9",
            [
                ChatMessage(
                    "dm-1", "\u4f60\u597d", 1700000000000, "peer-9", "\u5c0f\u674e"
                )
            ],
        )
        self.assertTrue(
            wait_until(lambda: received, timeout=2.0, pump=self.pump),
            "an incoming 1:1 message must notify while the window is hidden",
        )
        self.assertEqual(received[-1][0], "direct:peer-9")
        self.assertEqual(received[-1][1], "\u5c0f\u674e")
        self.assertEqual(received[-1][2], "\u4f60\u597d")

        # an OWN message never notifies
        vm.direct.seed_messages(
            "peer-9",
            [
                ChatMessage(
                    "dm-2",
                    "\u56de\u590d",
                    1700000001000,
                    "me",
                    "\u6211",
                    is_from_me=True,
                )
            ],
        )
        self.pump()
        self.assertIsNone(vm._tray_accum)
        self.assertEqual(len(received), 1)

    def test_tray_click_opens_direct_chat(self):
        """Clicking a "direct:<peerId>" tray bubble returns to the window and
        opens the matching 1:1 chat page (group ids keep the old behavior)."""
        from localchat.ui.main_window import MainWindow

        network_module.TCP_PORT = 10049
        vm = make_vm(_fresh_db("lc_ui_tray_direct_click.db"))
        self._vms = [vm]
        vm.direct.add_contact(Peer("peer-9", "\u5c0f\u674e", "127.0.0.1", 9))
        win = MainWindow(vm)
        win._last_notify_gid = "direct:peer-9"
        win._on_message_clicked()
        self.pump()
        self.assertEqual(win.stack.currentIndex(), 5, "PAGE_DIRECT must be shown")
        self.assertEqual(win.pages[5]._peer_id, "peer-9")
        win.close()

    def test_add_direct_contact_normalizes_and_validates(self):
        """Regression: adding a member by IP must NORMALIZE full-width IME
        punctuation (a Chinese IME emits \uff1a/\uff0e/\u3002) and REJECT
        mangled endpoints. Before the fix any non-empty junk was accepted as
        a "contact" that showed in the member list but could never connect —
        reported as "adding a peer by IP has no effect"."""
        network_module.TCP_PORT = 10036
        vm = make_vm(_fresh_db("lc_test_addip.db"))
        self._vms = [vm]

        # full-width digits + ideographic/full-stop dots + full-width colon
        fw = (
            "\uff11\uff19\uff12\u3002\uff11\uff16\uff18\uff0e\uff10"
            "\u3002\uff11\uff19\uff11\uff1a\uff19\uff19\uff19\uff19"
        )
        self.assertTrue(vm.add_direct_contact(fw, ""))
        contact = vm.direct_contacts_list()[-1]
        self.assertEqual(contact.ip_address, "192.168.0.191")
        self.assertEqual(contact.port, 9999)
        self.assertEqual(contact.id, "ip:192.168.0.191:9999")

        # spaces around host and port are tolerated
        self.assertTrue(vm.add_direct_contact(" 192.168.0.192 : 10000 ", ""))
        contact = vm.direct_contacts_list()[-1]
        self.assertEqual((contact.ip_address, contact.port), ("192.168.0.192", 10000))

        # bare IP gets the default program port; a hostname stays allowed
        self.assertTrue(vm.add_direct_contact("192.168.0.193", ""))
        contact = vm.direct_contacts_list()[-1]
        self.assertEqual((contact.ip_address, contact.port), ("192.168.0.193", 10036))
        self.assertTrue(vm.add_direct_contact("mypc", ""))

        # mangled / out-of-range endpoints are rejected, not silently added
        before = len(vm.direct_contacts_list())
        bad_inputs = [
            "",
            "   ",
            "127001",           # dots lost
            "192.168.0",        # too few octets
            "1.2.3",
            "192.168.0.300",    # octet out of range
            "999",
            "192.168.0.1:0",    # port out of range
            "192.168.0.1:70000",
            "192.168.0.1:abc",
            "host name!",       # space / '!' are not hostname chars
        ]
        for bad in bad_inputs:
            self.assertFalse(vm.add_direct_contact(bad, ""), f"must reject {bad!r}")
        self.assertEqual(len(vm.direct_contacts_list()), before)

    def test_readd_by_ip_keeps_real_contact(self):
        """Regression: manually re-adding by IP an endpoint already known
        under its REAL device id must keep the real contact. The endpoint
        dedupe used to let the fresh "ip:..." placeholder CLOBBER the real
        contact, orphaning the chat history keyed by the real id — the user
        saw their existing member get wiped by an add (and, with the peer
        offline, no migration ever repaired it)."""
        vm = make_vm(_fresh_db("lc_test_readd_ip.db"))
        self._vms = [vm]

        real = Peer(
            "8fe9b98a-59d7-4f39-89ab-a333299a6d6d",
            "MyPhone",
            "192.168.0.191",
            9999,
        )
        vm.direct.configure("my-device-id", "me", "127.0.0.1", 10038,
                            saved_contacts=[real])

        self.assertTrue(vm.add_direct_contact("192.168.0.191:9999", "Again"))
        contacts = vm.direct_contacts_list()
        self.assertEqual(len(contacts), 1, "no second row for a known endpoint")
        self.assertEqual(contacts[0].id, real.id, "real contact must survive")
        self.assertFalse(
            any(c.id.startswith("ip:") for c in contacts),
            "the manual placeholder must not be stored over the real contact",
        )

    def test_second_add_to_connected_member_surfaces_toast(self):
        """Regression: re-adding an ALREADY-CONNECTED member by IP must NOT
        be silent. start_chat short-circuits on a live session with no event
        at all, so the second add did nothing visible — reported as "only
        the first add works". The manual-add path must surface the
        connection state itself."""
        vm = make_vm(_fresh_db("lc_test_second_add.db"))
        self._vms = [vm]
        vm.direct.configure("my-device", "me", "127.0.0.1", 10039)
        real = Peer(
            "8fe9b98a-85e4-4d95-aba4-b5bf4848dbc5", "MyPhone",
            "192.168.0.191", 9999,
        )
        vm.direct.add_contact(real)
        vm.direct._sessions[real.id] = {
            "peer_id": real.id,
            "peer_name": real.name,
            "sock": socket.socket(),
            "wire": None,
            "alive": True,
            "send_queue": None,
        }
        toasts = []
        vm.status_message.connect(lambda t: toasts.append(t))

        self.assertTrue(vm.add_direct_contact("192.168.0.191:9999", ""))
        deadline = time.time() + 5
        while time.time() < deadline and not toasts:
            self.pump()
            time.sleep(0.05)
        self.assertTrue(
            any("\u5df2\u8fde\u63a5" in t for t in toasts),  # 已连接
            f"re-add of a connected member must toast its state: {toasts}",
        )


    def test_query_group_validates_endpoint(self):
        """Regression: query_group used to accept any non-empty host/id — a
        mangled IP, out-of-range port or wrong-length join id sailed through
        and the query then just timed out with no clue. It must now validate
        like add_direct_contact (8-digit id, plausible host, 1-65535 port)."""
        network_module.TCP_PORT = 10041
        vm = make_vm(_fresh_db("lc_test_query_valid.db"))
        self._vms = [vm]
        toasts = []
        vm.status_message.connect(lambda t: toasts.append(t))

        bad_cases = [
            ("1234567", "192.168.0.1", None),  # 7-digit join id
            ("123456789", "192.168.0.1", None),  # 9-digit join id
            ("12345678", "127001", None),  # mangled host (dots lost)
            ("12345678", "192.168.0.300", None),  # octet out of range
            ("12345678", "192.168.0.1", 0),  # explicit port out of range
            ("12345678", "192.168.0.1", 70000),
            ("12345678", "192.168.0.1:0", None),  # port inside the address
        ]
        for gid, ip, port in bad_cases:
            toasts.clear()
            vm.query_group("\u6210\u5458", gid, ip, port=port)
            self.assertIsNone(vm.setup_p2p, f"must not dial for {gid}/{ip}/{port}")
            self.assertIn(
                "\u5730\u5740\u65e0\u6548", "".join(toasts), f"must toast for {gid}/{ip}/{port}"
            )

        # empty nick: silent return, and the empty name is never persisted
        vm.query_group("   ", "12345678", "192.168.0.1")
        self.assertIsNone(vm.setup_p2p)
        self.assertEqual(vm.store.get_setting("nickname", ""), "")

        # endpoint validation wins over the nickname check: a blank nick with
        # a bad endpoint still surfaces the toast (not a silent no-op)
        toasts.clear()
        vm.query_group("   ", "1234567", "192.168.0.1")
        self.assertIsNone(vm.setup_p2p)
        self.assertIn("\u5730\u5740\u65e0\u6548", "".join(toasts))

        # a valid endpoint proceeds to an actual query
        vm.query_group("\u6210\u5458", "12345678", "127.0.0.1", port=10041)
        self.assertIsNotNone(vm.setup_p2p)
        self.assertEqual(vm.pending_host_ip, "127.0.0.1")
        self.assertEqual(vm.pending_host_port, 10041)

    def test_group_name_exists(self):
        """Same-name creation silently replaces the old instance (same derived
        group id); the VM must expose a lookup so the UI can confirm first."""
        network_module.TCP_PORT = 10042
        dba = _fresh_db("lc_test_gname_exists.db")
        vm = make_vm(dba)
        self._vms = [vm]
        self.assertFalse(vm.group_name_exists("\u5df2\u5b58\u5728"))
        vm.create_group("\u4e3b\u673a", "\u5df2\u5b58\u5728")
        self.assertTrue(vm.group_name_exists("\u5df2\u5b58\u5728"))
        self.assertFalse(vm.group_name_exists("\u5176\u4ed6\u7fa4"))
        self.assertFalse(vm.group_name_exists("   "))
        # still detected after a restart, from the persisted row alone
        vm.shutdown()
        vm2 = make_vm(dba)
        self._vms = [vm2]
        self.assertTrue(vm2.group_name_exists("\u5df2\u5b58\u5728"))

    def test_display_names_capped_at_20(self):
        """Nicknames, group names and contact remarks are truncated to
        MAX_NAME_LENGTH (Android parity, 20 chars)."""
        from localchat.view_model import MAX_NAME_LENGTH

        self.assertEqual(MAX_NAME_LENGTH, 20)
        network_module.TCP_PORT = 10043
        vm = make_vm(_fresh_db("lc_test_namecap.db"))
        self._vms = [vm]
        vm.create_group("N" * 35, "G" * 35)
        self.assertEqual(vm.nickname, "N" * 20)
        meta = vm.groups_list()[0]
        self.assertEqual(meta.group_name, "G" * 20)

        vm.set_nickname("M" * 35)
        self.assertEqual(vm.nickname, "M" * 20)
        self.assertEqual(vm.store.get_setting("nickname", ""), "M" * 20)

        self.assertTrue(vm.add_direct_contact("192.168.0.5:10043", "R" * 35))
        contact = vm.direct_contacts_list()[-1]
        self.assertEqual(contact.name, "R" * 20)

    def test_parse_host_port_superscript_no_crash(self):
        '''Regression: "²".isdigit() is True but int("²") raises ValueError —
        _parse_host_port must guard with isascii() instead of crashing.'''
        network_module.TCP_PORT = 10044
        vm = make_vm(_fresh_db("lc_test_superscript.db"))
        self._vms = [vm]
        host, port = vm._parse_host_port("192.168.0.1:\u00b2\u00b2")
        self.assertEqual(port, network_module.TCP_PORT)
        self.assertFalse(vm.add_direct_contact("192.168.0.1:\u00b2\u00b2", ""))
        # full-width digits still normalize and parse
        host, port = vm._parse_host_port("192.168.0.1:\uff11\uff10\uff10\uff14\uff14")
        self.assertEqual((host, port), ("192.168.0.1", 10044))

    def test_corrupt_port_setting_falls_back_to_default(self):
        """Regression: startup used a bare int() on the persisted port — a
        corrupted/hand-edited value crashed the constructor."""
        dba = _fresh_db("lc_test_badport.db")
        store = ChatStore(dba)
        store.set_setting("port", "not_a_port")
        store.close()
        network_module.TCP_PORT = 10045
        vm = make_vm(dba)
        self._vms = [vm]
        self.assertEqual(vm.port, 10045)

    def test_name_input_widgets_have_max_length(self):
        """The UI caps nickname/group/remark inputs at 20 chars (double
        protection on top of the VM-side truncation)."""
        from localchat.ui.member_list_page import MemberListPage
        from localchat.ui.settings_page import SettingsPage
        from localchat.ui.setup_page import SetupPage

        network_module.TCP_PORT = 10046
        vm = make_vm(_fresh_db("lc_ui_caps.db"))
        self._vms = [vm]
        setup = SetupPage(vm, lambda: None, lambda: None)
        for edit in (setup.create_name_edit, setup.create_group_edit, setup.join_name_edit):
            self.assertEqual(edit.maxLength(), 20)
        settings = SettingsPage(vm, lambda: None)
        self.assertEqual(settings.nick_edit.maxLength(), 20)
        members = MemberListPage(vm, lambda: None, lambda: None, lambda c: None)
        setup.deleteLater()
        settings.deleteLater()
        members.deleteLater()

    def test_contact_request_buttons_dispatch_request_id(self):
        """Regression: QPushButton.clicked emits a bool, which PyQt injected
        into the defaulted `rid` parameter of the request-card lambdas, so
        accept and ignore both ran with rid=False (no matching request) and
        the buttons looked dead. Clicking must dispatch the real request id."""
        from localchat.models import ContactRequest
        from localchat.ui.member_list_page import MemberListPage, RequestCard

        network_module.TCP_PORT = 10047
        vm = make_vm(_fresh_db("lc_request_buttons.db"))
        self._vms = [vm]

        req = ContactRequest(
            id="dev-42",
            name="\u7528\u6237",
            ip="192.168.0.157",
            port=9999,
        )
        calls = []
        vm.direct_requests_list = lambda: [req]
        vm.direct_contacts_list = lambda: []
        vm.accept_contact_request = lambda rid: calls.append(("accept", rid))
        vm.ignore_contact_request = lambda rid: calls.append(("ignore", rid))

        page = MemberListPage(vm, lambda: None, lambda: None, lambda c: None)
        card = page.list.findChild(RequestCard)
        self.assertIsNotNone(card, "request card must be rendered")
        buttons = {b.text(): b for b in card.findChildren(QPushButton)}
        buttons["\u63a5\u53d7"].click()
        buttons["\u5ffd\u7565"].click()
        self.assertEqual(calls, [("accept", "dev-42"), ("ignore", "dev-42")])
        page.deleteLater()

    # ------------------------------------------- reply / read / typing (chat UX)

    def test_store_reply_and_read_roundtrip(self):
        """SavedMessage carries the reply triple and the own-message read flag
        across a reopen: a restart must not lose the quoted header or flip a
        delivered message back to 未读."""
        from localchat.storage import SavedMessage

        db = _fresh_db("lc_test_reply_store.db")
        store = ChatStore(db)
        store.upsert_group(
            SavedGroup(group_id="direct:dev-2", group_name="\u5c0f\u4e59", is_host=False)
        )
        store.insert_message(
            SavedMessage(
                id="m1",
                group_id="direct:dev-2",
                content="\u56de\u590d\u6b63\u6587",  # 回复正文
                timestamp=1700000000000,
                sender_id="dev-me",
                sender_name="\u6211",  # 我
                is_from_me=True,
                reply_to="m0",
                reply_preview="\u88ab\u5f15\u7528\u7684\u5185\u5bb9",  # 被引用的内容
                reply_sender="\u5c0f\u4e59",
                read=True,
            )
        )
        store.close()

        reopened = ChatStore(db)
        saved = reopened.get_messages_for_group("direct:dev-2")
        reopened.close()
        self.assertEqual(len(saved), 1)
        m = saved[0]
        self.assertEqual(m.reply_to, "m0")
        self.assertEqual(m.reply_preview, "\u88ab\u5f15\u7528\u7684\u5185\u5bb9")
        self.assertEqual(m.reply_sender, "\u5c0f\u4e59")
        self.assertTrue(m.read)

    def test_direct_page_reply_bar_arms_sends_and_cancels(self):
        """DirectChatPage: picking 回复 shows the quote bar, the sent message
        carries the reply triple, and the cancel button (whose clicked signal
        injects a bool — AGENTS.md trap) clears it."""
        from PyQt6.QtWidgets import QPushButton

        from localchat.models import ChatMessage
        from localchat.ui.direct_chat_page import DirectChatPage

        network_module.TCP_PORT = 10049
        vm = make_vm(_fresh_db("lc_ui_reply.db"))
        self._vms = [vm]
        page = DirectChatPage(vm, lambda: None)
        page._peer_id = "dev-2"

        target = ChatMessage(
            id="m0",
            content="\u88ab\u5f15\u7528\u7684\u5185\u5bb9",
            timestamp=1700000000000,
            sender_id="dev-2",
            sender_name="\u5c0f\u4e59",
        )
        page._set_reply_target(target)
        self.assertFalse(page.reply_bar.isHidden(), "quote bar must show")
        self.assertIn("\u5c0f\u4e59", page.reply_label.text())

        # cancel via the real button click path
        buttons = {b.text(): b for b in page.reply_bar.findChildren(QPushButton)}
        buttons["\u00d7"].click()
        self.assertTrue(page.reply_bar.isHidden(), "cancel must hide the quote bar")
        self.assertIsNone(page._reply_target)

        # arm again, type, send: the VM receives the reply triple
        page._set_reply_target(target)
        calls = []

        def fake_send(peer_id, content, reply_to=None, reply_preview=None, reply_sender=None):
            calls.append((peer_id, content, reply_to, reply_preview, reply_sender))
            return True

        vm.send_direct_message = fake_send
        page.input_edit.setPlainText("hello")
        page.send_btn.click()
        self.assertEqual(
            calls,
            [("dev-2", "hello", "m0", "\u88ab\u5f15\u7528\u7684\u5185\u5bb9", "\u5c0f\u4e59")],
        )
        self.assertTrue(page.reply_bar.isHidden(), "send must clear the quote bar")
        self.assertEqual(page.input_edit.toPlainText(), "")
        page.deleteLater()

    def test_group_page_shows_typing_names(self):
        """ChatPage renders 群成员正在输入 from the ViewModel's live indicator
        set and hides it once the set empties."""
        import time as time_mod

        from localchat.ui.chat_page import ChatPage

        network_module.TCP_PORT = 10050
        vm = make_vm(_fresh_db("lc_ui_typing.db"))
        self._vms = [vm]
        vm.active_group_id = "g1"
        page = ChatPage(vm, lambda: None)
        self.assertTrue(page.typing_label.isHidden())

        vm._group_typing = {
            "g1": {
                "u1": ("\u5f20\u4e09", time_mod.monotonic() + 10),  # 张三
                "u2": ("\u674e\u56db", time_mod.monotonic() + 10),  # 李四
            }
        }
        vm.typing_state_changed.emit()
        self.pump()
        self.assertFalse(page.typing_label.isHidden())
        self.assertIn("\u5f20\u4e09", page.typing_label.text())
        self.assertIn("\u674e\u56db", page.typing_label.text())

        vm._group_typing = {}
        vm.typing_state_changed.emit()
        self.pump()
        self.assertTrue(page.typing_label.isHidden())
        page.deleteLater()

    def test_typing_indicator_expires_without_refresh(self):
        """A received typing indicator is dropped after TYPING_TIMEOUT with no
        refresh (the expiry tick), so a peer that dies mid-typing clears."""
        import time as time_mod

        network_module.TCP_PORT = 10051
        vm = make_vm(_fresh_db("lc_typing_expiry.db"))
        self._vms = [vm]
        vm._on_direct_typing("dev-9", True)
        self.assertTrue(vm.direct_peer_typing("dev-9"))
        vm._on_direct_typing("dev-9", False)
        self.assertFalse(vm.direct_peer_typing("dev-9"))

        vm._on_direct_typing("dev-9", True)
        # simulate the deadline passing, then run one expiry tick
        vm._direct_typing["dev-9"] = time_mod.monotonic() - 0.01
        vm._on_typing_tick()
        self.assertFalse(vm.direct_peer_typing("dev-9"))
        self.assertFalse(vm._typing_timer.isActive(), "no indicator left: tick stops")

    def test_typing_activity_throttles_and_stops(self):
        """The outbound throttle sends active=True at most once per interval
        and only on transitions; _end_active_typing stops it immediately."""
        network_module.TCP_PORT = 10052
        vm = make_vm(_fresh_db("lc_typing_throttle.db"))
        self._vms = [vm]
        sent = []
        # no real session: intercept the send callback directly
        vm._typing_activity("direct:dev-9", lambda active: sent.append(active))
        self.assertEqual(sent, [True], "first keystroke sends active")
        vm._typing_activity("direct:dev-9", lambda active: sent.append(active))
        self.assertEqual(sent, [True], "within the interval it must be throttled")
        vm._end_active_typing("direct:dev-9")
        self.assertEqual(sent, [True, False], "ending typing sends active=False now")
        self.assertNotIn("direct:dev-9", vm._typing_out)


if __name__ == "__main__":
    unittest.main()
