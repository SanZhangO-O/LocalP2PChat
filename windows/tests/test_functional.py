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
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import localchat.network as network_module
from localchat.network import P2PListener, P2PManager
from localchat.storage import ChatStore
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

    def join_state_changed(self, p2p):
        if p2p.connection_result is not None:
            self.join_results.append(p2p.connection_result)

    def query_result_changed(self, p2p):
        self.query_results.append((p2p.queried_group_info, p2p.query_error))


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


if __name__ == "__main__":
    unittest.main()
