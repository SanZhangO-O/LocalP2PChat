"""Group management tests: owner-only group_update (rename + announcement) and
kick_member, plus the member-side teardown/persistence and the mesh path.

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
Run with:  python -m pytest tests/test_group_admin.py -q
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

import localchat.network as network_module
from localchat.models import ChatMessage, NetworkPacket, Peer
from localchat.network import (
    GroupMeshListener,
    GroupMeshManager,
    HostGroupServer,
    P2PListener,
    P2PManager,
)
from localchat.storage import ChatStore, SavedGroup
from localchat.view_model import ChatViewModel

HOST_NAME = "\u4e3b\u673a"
GROUP_NAME = "\u529e\u516c\u5ba4"
NEW_NAME = "\u65b0\u529e\u516c\u5ba4"
ANNOUNCEMENT = "\u4eca\u5929\u4e0b\u5348\u5f00\u4f1a"
PASSWORD = "pw123456"
MEMBER_B = "member-b"


def wait_until(cond, timeout=12.0, pump=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pump is not None:
            pump()
        if cond():
            return True
        time.sleep(0.02)
    return False


class Recorder(P2PListener):
    def __init__(self):
        self.group_info_changes = 0
        self.kicked = 0

    def group_info_changed(self, p2p):
        self.group_info_changes += 1

    def kicked_from_group(self, p2p):
        self.kicked += 1


class MeshRecorder(GroupMeshListener):
    def __init__(self):
        self.admins = []  # (group_id, NetworkPacket)

    def group_mesh_admin(self, group_id, packet):
        self.admins.append((group_id, packet))


# ------------------------------------------------------------- wire contract


class GroupAdminPacketTest(unittest.TestCase):
    def test_group_update_roundtrip_matches_kotlin_bytes(self):
        pkt = NetworkPacket(
            type="group_update",
            group_id="g1",
            sender_id="owner",
            group_name="NewName",
            announcement="hello",
        )
        wire = pkt.to_json()
        # compact camelCase JSON, exactly what kotlinx.serialization emits for
        # the same packet (defaults are not encoded there, and None fields are
        # omitted here)
        self.assertEqual(
            '{"type":"group_update","groupId":"g1","senderId":"owner",'
            '"groupName":"NewName","announcement":"hello"}',
            wire,
        )
        decoded = NetworkPacket.from_json(wire)
        self.assertEqual("group_update", decoded.type)
        self.assertEqual("owner", decoded.sender_id)
        self.assertEqual("NewName", decoded.group_name)
        self.assertEqual("hello", decoded.announcement)

    def test_group_update_omits_unchanged_fields(self):
        pkt = NetworkPacket(
            type="group_update", group_id="g1", sender_id="owner", announcement="only"
        )
        wire = pkt.to_json()
        self.assertNotIn("groupName", wire)
        self.assertIn('"announcement":"only"', wire)

    def test_kick_member_roundtrip(self):
        pkt = NetworkPacket(
            type="kick_member", group_id="g1", sender_id="owner", target_id="bob"
        )
        wire = pkt.to_json()
        self.assertEqual(
            '{"type":"kick_member","groupId":"g1","senderId":"owner","targetId":"bob"}',
            wire,
        )
        decoded = NetworkPacket.from_json(wire)
        self.assertEqual("kick_member", decoded.type)
        self.assertEqual("bob", decoded.target_id)

    def test_group_update_requires_group_id(self):
        with self.assertRaises(ValueError):
            NetworkPacket.from_dict({"type": "group_update", "senderId": "owner"})

    def test_kick_member_requires_group_and_target(self):
        with self.assertRaises(ValueError):
            NetworkPacket.from_dict(
                {"type": "kick_member", "groupId": "g1", "senderId": "owner"}
            )


# --------------------------------------------------------- relay path (host)


class GroupAdminNetworkTest(unittest.TestCase):
    PORT = 19411

    def setUp(self):
        self.host = P2PManager(Recorder(), port=self.PORT, password=PASSWORD)
        self.host.initialize_as_host(HOST_NAME, GROUP_NAME, password=PASSWORD)
        self.host.set_join_id(self.host.numeric_group_id)
        self.host.start_as_host()

    def tearDown(self):
        for client in getattr(self, "_clients", []):
            client.stop()
        self.host.stop()

    def _join(self, name, device_id=None):
        client = P2PManager(Recorder(), port=self.PORT, device_id=device_id)
        client.initialize_as_client(name, GROUP_NAME, password=PASSWORD)
        client.set_join_id(self.host.numeric_group_id)
        # the ViewModel normally resolves the creator id from its persisted
        # group info; here the host's own id is the creator
        client.creator_id_provider = lambda: self.host.my_id
        client.query_group("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: client.queried_group_info is not None))
        client.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: client.connection_result is not None))
        ok, message = client.connection_result
        self.assertTrue(ok, message)
        self.assertTrue(wait_until(lambda: self.host.my_id in client.peers))
        self.assertTrue(wait_until(lambda: client.my_id in self.host.peers))
        if not hasattr(self, "_clients"):
            self._clients = []
        self._clients.append(client)
        return client

    def test_owner_group_update_reaches_member(self):
        member = self._join("\u6210\u5458")
        self.host.send_group_update(NEW_NAME, ANNOUNCEMENT)
        self.assertTrue(
            wait_until(
                lambda: member.listener.group_info_changes > 0
                and member.group_name == NEW_NAME
                and member.group_announcement == ANNOUNCEMENT
            )
        )
        # the owner also applied it locally
        self.assertEqual(self.host.group_name, NEW_NAME)
        self.assertEqual(self.host.group_announcement, ANNOUNCEMENT)

    def test_rename_keeps_numeric_join_id_and_new_members_can_join(self):
        join_id_before = self.host.numeric_group_id
        self.host.send_group_update(NEW_NAME, None)
        self.assertEqual(join_id_before, self.host.numeric_group_id)
        self.assertTrue(self.host._matches_group(join_id_before))
        # a newcomer joining with the PRE-RENAME numeric id still gets in
        member = self._join("\u65b0\u6210\u5458")
        self.assertEqual(member.group_name, NEW_NAME)
        self.assertTrue(wait_until(lambda: member.my_id in self.host.peers))

    def test_announcement_travels_with_join_ack(self):
        self.host.send_group_update("", ANNOUNCEMENT)
        member = self._join("\u65b0\u6210\u5458")
        self.assertEqual(member.group_announcement, ANNOUNCEMENT)

    def test_member_sent_group_update_is_refused_and_disconnects(self):
        member = self._join("\u6210\u5458")
        member._host_wire.send_packet(
            NetworkPacket(
                type="group_update",
                group_id=member.current_group_id,
                sender_id=member.my_id,
                announcement="hacked",
            )
        )
        # the host detaches the offender; the announcement never changed
        self.assertTrue(wait_until(lambda: member.my_id not in self.host.peers))
        self.assertEqual(self.host.group_announcement, "")

    def test_member_sent_kick_is_refused(self):
        member = self._join("\u6210\u5458")
        victim = self._join(MEMBER_B)
        member._host_wire.send_packet(
            NetworkPacket(
                type="kick_member",
                group_id=member.current_group_id,
                sender_id=member.my_id,
                target_id=victim.my_id,
            )
        )
        self.assertTrue(wait_until(lambda: member.my_id not in self.host.peers))
        # the victim stays in the group
        self.assertIn(victim.my_id, self.host.peers)
        self.assertTrue(wait_until(lambda: victim.my_id in self.host.peers))

    def test_forged_relayed_group_update_disconnects_member(self):
        member = self._join("\u6210\u5458")
        # a packet whose senderId is not the creator must be ignored and the
        # connection dropped (fail-closed), never applied
        member._process_packet_as_client(
            NetworkPacket(
                type="group_update",
                group_id=member.current_group_id,
                sender_id="attacker",
                announcement="forged",
            )
        )
        self.assertTrue(wait_until(lambda: member.connection_lost))
        self.assertEqual(member.group_announcement, "")
        self.assertNotEqual(member.group_name, "forged")

    def test_owner_kick_removes_member_everywhere(self):
        victim = self._join("\u88ab\u8e22\u8005")
        bystander = self._join("\u65c1\u89c2\u8005")
        victim_id = victim.my_id
        self.assertTrue(self.host.kick_member(victim_id))
        # the victim runs its teardown callback exactly once
        self.assertTrue(wait_until(lambda: victim.listener.kicked == 1))
        # the owner and every other member drop it
        self.assertTrue(wait_until(lambda: victim_id not in self.host.peers))
        self.assertTrue(wait_until(lambda: victim_id not in bystander.peers))
        # re-kicking an unknown member is a no-op
        self.assertFalse(self.host.kick_member(victim_id))


# --------------------------------------------------------------- mesh path


class GroupAdminMeshTest(unittest.TestCase):
    PORT_A = 19421
    PORT_B = 19422
    OWNER = "owner-id"

    def setUp(self):
        self._old_retry = GroupMeshManager.RETRY_INTERVAL
        GroupMeshManager.RETRY_INTERVAL = 0.05

        self.server_a = HostGroupServer(self.PORT_A)
        self.server_a.ensure_running()
        self.rec_a = MeshRecorder()
        self.a = GroupMeshManager()
        self.a.attach(self.rec_a)
        self.a.creator_id_provider = lambda gid: self.OWNER
        self.server_a.mesh_manager = self.a
        self.server_a.password_lookup = lambda mode, gid: self.a.password_for(gid)

        self.server_b = HostGroupServer(self.PORT_B)
        self.server_b.ensure_running()
        self.rec_b = MeshRecorder()
        self.b = GroupMeshManager()
        self.b.attach(self.rec_b)
        self.b.creator_id_provider = lambda gid: self.OWNER
        self.server_b.mesh_manager = self.b
        self.server_b.password_lookup = lambda mode, gid: self.b.password_for(gid)

        self.peer_a = Peer("aaa-member", "A", "127.0.0.1", self.PORT_A)
        self.peer_b = Peer("bbb-member", "B", "127.0.0.1", self.PORT_B)

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server_a.shutdown()
        self.server_b.shutdown()
        GroupMeshManager.RETRY_INTERVAL = self._old_retry

    def _link(self, group=GROUP_NAME):
        self.b.enter_group(group, self.peer_b, [self.peer_a], [], PASSWORD)
        self.a.enter_group(group, self.peer_a, [self.peer_b], [], PASSWORD)
        self.assertTrue(
            wait_until(lambda: self.a.has_links(group) and self.b.has_links(group))
        )

    def test_owner_update_over_mesh_is_forwarded(self):
        self._link()
        self.a.broadcast_admin(
            GROUP_NAME,
            NetworkPacket(
                type="group_update",
                group_id=GROUP_NAME,
                sender_id=self.OWNER,
                announcement=ANNOUNCEMENT,
            ),
        )
        self.assertTrue(
            wait_until(
                lambda: any(p.type == "group_update"
                            and p.announcement == ANNOUNCEMENT
                            for _, p in self.rec_b.admins)
            )
        )

    def test_non_owner_mesh_update_drops_the_link(self):
        self._link()
        self.a.broadcast_admin(
            GROUP_NAME,
            NetworkPacket(
                type="group_update",
                group_id=GROUP_NAME,
                sender_id="attacker",
                announcement="forged",
            ),
        )
        self.assertTrue(wait_until(lambda: not self.b.has_links(GROUP_NAME)))
        self.assertEqual(self.rec_b.admins, [])

    def test_kick_over_mesh_is_forwarded_to_view_model(self):
        self._link()
        packet = NetworkPacket(
            type="kick_member",
            group_id=GROUP_NAME,
            sender_id=self.OWNER,
            target_id="ccc-member",
        )
        self.a.broadcast_admin(GROUP_NAME, packet)
        self.assertTrue(
            wait_until(
                lambda: any(
                    p.type == "kick_member" and p.target_id == "ccc-member"
                    for _, p in self.rec_b.admins
                )
            )
        )


# -------------------------------------------------------------- persistence


class GroupAdminStorageTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.gettempdir(), "kilo", "lc_admin_store.db")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            os.remove(self.path)
        self.store = ChatStore(self.path)

    def tearDown(self):
        self.store.close()

    def test_announcement_persists_and_none_keeps_it(self):
        self.store.upsert_group(
            SavedGroup(group_id="g1", group_name="G", is_host=False, announcement="hi")
        )
        self.assertEqual(self.store.get_group("g1").announcement, "hi")
        # a metadata refresh without the field must not clear it
        self.store.upsert_group(
            SavedGroup(group_id="g1", group_name="G", is_host=False, member_count=3)
        )
        self.assertEqual(self.store.get_group("g1").announcement, "hi")
        # an explicit "" clears it
        self.store.upsert_group(
            SavedGroup(group_id="g1", group_name="G", is_host=False, announcement="")
        )
        self.assertEqual(self.store.get_group("g1").announcement, "")

    def test_kick_flag_keeps_history(self):
        self.store.upsert_group(SavedGroup(group_id="g1", group_name="G", is_host=False))
        from localchat.storage import SavedMessage

        self.store.insert_message(
            SavedMessage(
                id="m1",
                group_id="g1",
                content="bye",
                timestamp=1,
                sender_id="a",
                sender_name="A",
                is_from_me=False,
            )
        )
        self.store.set_group_kicked("g1", True)
        saved = self.store.get_group("g1")
        self.assertTrue(saved.kicked)
        # the row survives so the FK cascade does not wipe the history
        self.assertEqual(len(self.store.get_messages_for_group("g1")), 1)
        self.store.set_group_kicked("g1", False)
        self.assertFalse(self.store.get_group("g1").kicked)


# ----------------------------------------------------- full ViewModel flows


DATA_DIR = os.path.join(tempfile.gettempdir(), "kilo", "lc_admin_vmdata")


def _fresh_db(name):
    path = os.path.join(tempfile.gettempdir(), "kilo", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    return path


def make_vm(db_path):
    os.makedirs(DATA_DIR, exist_ok=True)
    return ChatViewModel(ChatStore(db_path), data_dir=DATA_DIR)


class GroupAdminViewModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        for vm in getattr(self, "_vms", []):
            vm.shutdown()

    def pump(self):
        self.app.processEvents()

    def _host_and_member(self, port):
        network_module.TCP_PORT = port
        host = make_vm(_fresh_db("lc_admin_host.db"))
        host.create_group(HOST_NAME, GROUP_NAME)
        gid = host.active_group_id
        self.assertIsNotNone(gid)
        member = make_vm(_fresh_db("lc_admin_member.db"))
        joined = []
        member.join_successful.connect(lambda: joined.append(True))
        member.query_group(
            "\u6210\u5458",
            host.active_group_numeric_id(),
            "127.0.0.1",
            password=host.active_group_password,
        )
        self.assertTrue(
            wait_until(lambda: member.queried_group_info() is not None, pump=self.pump)
        )
        member.confirm_join()
        self.assertTrue(wait_until(lambda: joined, pump=self.pump))
        self.assertTrue(
            wait_until(lambda: len(host.active_peers()) == 1, pump=self.pump)
        )
        self._vms = [host, member]
        return host, member, gid

    def test_owner_update_then_kick_end_to_end(self):
        host, member, gid = self._host_and_member(19441)
        member_id = next(iter(host.active_peers().keys()))

        # the member keeps a message so the kick must not delete the history
        host.send_message("\u79bb\u7fa4\u524d\u7684\u6d88\u606f")
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u79bb\u7fa4\u524d\u7684\u6d88\u606f"
                    for m in member.active_messages()
                ),
                pump=self.pump,
            )
        )

        self.assertTrue(host.update_group_info(NEW_NAME, ANNOUNCEMENT))
        self.assertTrue(
            wait_until(
                lambda: member.active_group_announcement() == ANNOUNCEMENT
                and member.active_group_name == NEW_NAME,
                pump=self.pump,
            )
        )
        # the member's rename + announcement persisted
        saved = member.store.get_group(gid)
        self.assertEqual(saved.announcement, ANNOUNCEMENT)
        self.assertEqual(saved.group_name, NEW_NAME)
        self.assertEqual(
            member.store.get_setting("group_creator_id_%s" % gid), host.group_p2p_map[gid].my_id
        )

        messages = []
        member.status_message.connect(lambda text: messages.append(text))
        self.assertTrue(host.kick_active_member(member_id))
        self.assertTrue(
            wait_until(
                lambda: all(g.group_id != gid for g in member.groups_list()),
                pump=self.pump,
            )
        )
        self.assertTrue(member.store.get_group(gid).kicked)
        # local history is kept
        self.assertTrue(member.store.get_messages_for_group(gid))
        self.assertTrue(
            wait_until(
                lambda: any("\u79fb\u51fa" in text for text in messages),
                pump=self.pump,
            ),
            "the member must be told it was removed: %r" % messages,
        )
        self.assertTrue(wait_until(lambda: not host.active_peers(), pump=self.pump))

    def test_group_lobby_shows_announcement_and_owner_controls(self):
        from localchat.ui.group_lobby_page import GroupLobbyPage

        host, member, gid = self._host_and_member(19442)
        host.update_group_info(NEW_NAME, ANNOUNCEMENT)

        host.switch_to_group(gid)
        host_page = GroupLobbyPage(
            host, on_back=lambda: None, on_open_chat=lambda: None, on_leave=lambda: None
        )
        host_page.refresh()
        self.assertTrue(host_page.announce_card.isVisibleTo(host_page))
        self.assertEqual(host_page.announce_label.text(), ANNOUNCEMENT)
        self.assertTrue(host_page.settings_btn.isVisibleTo(host_page))

        # the kick button dispatches the peer id (the clicked(bool) injection
        # trap would overwrite a default argument holding the id)
        peer_ids = list(host.active_peers().keys())
        self.assertTrue(peer_ids)
        dispatched = []
        host_page._confirm_kick = lambda pid: dispatched.append(pid)
        host_page.refresh()
        item = host_page.peer_list.item(1)  # row 0 is self
        row = host_page.peer_list.itemWidget(item)
        kick_buttons = [
            b
            for b in row.findChildren(QPushButton)
            if b.text() == "\u79fb\u51fa"
        ]
        self.assertEqual(len(kick_buttons), 1)
        kick_buttons[0].click()
        self.assertEqual(dispatched, peer_ids)

        # a plain member sees the announcement but has no owner controls
        member.switch_to_group(gid)
        self.assertTrue(
            wait_until(lambda: member.active_group_announcement() == ANNOUNCEMENT, pump=self.pump)
        )
        member_page = GroupLobbyPage(
            member, on_back=lambda: None, on_open_chat=lambda: None, on_leave=lambda: None
        )
        member_page.refresh()
        self.assertFalse(member_page.settings_btn.isVisibleTo(member_page))
        self.assertTrue(member_page.announce_card.isVisibleTo(member_page))
        self.assertEqual(member_page.announce_label.text(), ANNOUNCEMENT)
        # no kick button for members
        for i in range(member_page.peer_list.count()):
            row = member_page.peer_list.itemWidget(member_page.peer_list.item(i))
            texts = [b.text() for b in row.findChildren(QPushButton)]
            self.assertNotIn("\u79fb\u51fa", texts)


if __name__ == "__main__":
    unittest.main()
