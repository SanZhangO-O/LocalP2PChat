"""Group file share area (群文件) tests.

Covers the Windows half of the feature:

  - storage index CRUD, per-group cap and removal tombstones (a removed file
    never resurrects through convergence);
  - removal authorization: uploader or group owner, validated against the
    STORED entry, gating the relay rebroadcast;
  - the ViewModel wiring: share/remove build the group_file_add /
    group_file_remove packets, persist locally and register the local source;
  - convergence callbacks (join_ack / history_reply) apply tombstones first;
  - the mesh/manager providers that feed offline backfill.

Chinese literals are written as \\uXXXX escapes so the file stays pure-ASCII.
"""

import os
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication  # noqa: F401  (offscreen app)

import localchat.network as network_module
from localchat.models import GroupFileInfo
from localchat.storage import ChatStore, SavedGroup, SavedGroupFile
from tests.fake_peer import install_identity
from tests.test_functional import DATA_DIR, _fresh_db, make_vm

_APP = QApplication.instance() or QApplication([])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _temp_store(name):
    path = os.path.join(tempfile.gettempdir(), "kilo", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    return ChatStore(path)


def _entry(group_id, file_id, sender_id, ts=100, size=10):
    return SavedGroupFile(
        group_id=group_id,
        file_id=file_id,
        name=file_id + ".bin",
        size=size,
        sender_id=sender_id,
        sender_name="S",
        ts=ts,
        download_host="1.2.3.4",
        download_port=9999,
        file_key="k",
    )


class GroupFileStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = _temp_store("lc_gf_store.db")
        self.store.upsert_group(SavedGroup(group_id="g1", group_name="G", is_host=True))

    def tearDown(self):
        self.store.close()

    def test_index_newest_first_and_tombstone_hides(self):
        self.store.upsert_group_file(_entry("g1", "f1", "a", ts=100))
        self.store.upsert_group_file(_entry("g1", "f2", "a", ts=200))
        self.assertEqual([e.file_id for e in self.store.get_group_files("g1")], ["f2", "f1"])
        self.store.record_removed_group_files("g1", ["f1"])
        self.assertEqual([e.file_id for e in self.store.get_group_files("g1")], ["f2"])
        self.assertIsNone(self.store.get_group_file("g1", "f1"))
        self.assertEqual(self.store.get_removed_group_file_ids("g1"), ["f1"])

    def test_convergence_never_resurrects_a_tombstoned_entry(self):
        self.store.record_removed_group_files("g1", ["f9"])
        self.store.upsert_group_file(_entry("g1", "f9", "a", ts=999))
        self.assertEqual(self.store.get_group_files("g1"), [])
        # an offline member replaying its index cannot bring it back either
        self.assertEqual(self.store.get_removed_group_file_ids("g1"), ["f9"])

    def test_upsert_keeps_local_path_of_existing_row(self):
        local = _entry("g1", "f1", "a")
        local.local_path = "/tmp/f1"
        self.store.upsert_group_file(local)
        # the wire entry carries no localPath: the stored one must survive
        self.store.upsert_group_file(_entry("g1", "f1", "a", ts=500))
        self.assertEqual(self.store.get_group_file("g1", "f1").local_path, "/tmp/f1")

    def test_index_cap_keeps_newest(self):
        self.store.GROUP_FILES_CAP = 3
        for i in range(6):
            self.store.upsert_group_file(_entry("g1", "f%d" % i, "a", ts=i))
        self.assertEqual(
            [e.file_id for e in self.store.get_group_files("g1")],
            ["f5", "f4", "f3"],
        )


class GroupFileAuthorizationTest(unittest.TestCase):
    def test_uploader_and_owner_may_remove_others_may_not(self):
        can = network_module.can_remove_group_file
        self.assertTrue(can("up", "up", "owner"))
        self.assertTrue(can("owner", "up", "owner"))
        self.assertFalse(can("other", "up", "owner"))
        self.assertFalse(can("", "up", "owner"))
        # unknown creator: only the uploader may remove
        self.assertFalse(can("someone", "up", ""))


class GroupFileViewModelTest(unittest.TestCase):
    def setUp(self):
        install_identity()
        self._orig_tcp_port = network_module.TCP_PORT
        network_module.TCP_PORT = _free_port()
        self.db_path = _fresh_db("lc_gf_vm.db")
        self.vm = make_vm(self.db_path)
        self.vm.set_nickname("\u7fa4\u6587\u4ef6")
        self.vm.create_group("\u7fa4\u6587\u4ef6", "\u6d4b\u8bd5\u7fa4")
        self.gid = self.vm.active_group_id
        self.assertIsNotNone(self.gid)
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        self.vm.shutdown()
        network_module.TCP_PORT = self._orig_tcp_port

    def _make_file(self, name="share.txt", data=b"hello world"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def test_providers_wired_for_offline_convergence(self):
        p2p = self.vm.group_p2p_map[self.gid]
        self.assertEqual(p2p.group_files_provider, self.vm.store.get_group_files)
        self.assertEqual(
            p2p.removed_file_ids_provider, self.vm.store.get_removed_group_file_ids
        )
        self.assertEqual(self.vm.mesh.group_files_provider, self.vm.store.get_group_files)
        self.assertEqual(
            self.vm.mesh.removed_file_ids_provider,
            self.vm.store.get_removed_group_file_ids,
        )

    def test_share_persists_registers_and_announces(self):
        sent = []
        p2p = self.vm.group_p2p_map[self.gid]
        p2p.send_group_packet = lambda pkt: sent.append(pkt)
        self.vm.mesh.broadcast_admin = lambda gid, pkt: sent.append(pkt)
        self.assertTrue(self.vm.share_group_file(self._make_file()))
        entries = self.vm.group_files(self.gid)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertTrue(entry.local_path.endswith("share.txt"))
        self.assertEqual(entry.size, 11)
        self.assertTrue(entry.download_host)
        self.assertTrue(entry.file_key)
        # registered on the shared listener so the downloader can fetch it
        self.assertIsNotNone(self.vm.host_server._lookup_shared_file(entry.file_id))
        # announced over BOTH paths (host relay + mesh), without duplicates
        self.assertEqual(len(sent), 2)
        for pkt in sent:
            self.assertEqual(pkt.type, "group_file_add")
            self.assertEqual(pkt.file_id, entry.file_id)
            self.assertEqual(pkt.name, "share.txt")
            self.assertEqual(pkt.size, 11)
            self.assertEqual(pkt.sender_id, self.vm.my_device_id)
            self.assertIsNotNone(pkt.file_info)
            self.assertEqual(pkt.file_info.file_key, entry.file_key)

    def test_share_rejects_missing_or_empty_file(self):
        p2p = self.vm.group_p2p_map[self.gid]
        p2p.send_group_packet = lambda pkt: None
        self.vm.mesh.broadcast_admin = lambda gid, pkt: None
        self.assertFalse(self.vm.share_group_file(""))
        self.assertFalse(self.vm.share_group_file(os.path.join(self.dir, "nope")))
        empty = self._make_file("empty.bin", b"")
        self.assertFalse(self.vm.share_group_file(empty))
        self.assertEqual(self.vm.group_files(self.gid), [])

    def test_remove_authorized_persists_tombstone_and_announces(self):
        path = self._make_file()
        p2p = self.vm.group_p2p_map[self.gid]
        p2p.send_group_packet = lambda pkt: None
        self.vm.mesh.broadcast_admin = lambda gid, pkt: None
        self.assertTrue(self.vm.share_group_file(path))
        file_id = self.vm.group_files(self.gid)[0].file_id
        sent = []
        p2p.send_group_packet = lambda pkt: sent.append(pkt)
        self.vm.mesh.broadcast_admin = lambda gid, pkt: sent.append(pkt)
        self.assertTrue(self.vm.remove_group_file(file_id))
        self.assertEqual(self.vm.group_files(self.gid), [])
        self.assertEqual(self.vm.store.get_removed_group_file_ids(self.gid), [file_id])
        self.assertIsNone(self.vm.host_server._lookup_shared_file(file_id))
        self.assertEqual([p.type for p in sent], ["group_file_remove", "group_file_remove"])
        self.assertEqual(sent[0].file_id, file_id)

    def test_remove_unauthorized_returns_false(self):
        # act as a plain MEMBER (not the owner): another member's upload may
        # not be removed
        p2p = self.vm.group_p2p_map[self.gid]
        p2p.is_host = False
        self.vm.store.set_setting("group_creator_id_%s" % self.gid, "owner-x")
        self.vm.store.upsert_group_file(_entry(self.gid, "foreign", "someone-else"))
        self.assertFalse(self.vm.can_remove_shared_file(
            self.vm.store.get_group_file(self.gid, "foreign")
        ))
        self.assertFalse(self.vm.remove_group_file("foreign"))
        self.assertEqual(
            [e.file_id for e in self.vm.group_files(self.gid)], ["foreign"]
        )
        self.assertEqual(self.vm.store.get_removed_group_file_ids(self.gid), [])

    def test_owner_may_remove_someone_elses_share(self):
        self.vm.store.upsert_group_file(_entry(self.gid, "foreign", "someone-else"))
        self.assertTrue(self.vm.can_remove_shared_file(
            self.vm.store.get_group_file(self.gid, "foreign")
        ))
        self.assertTrue(self.vm.remove_group_file("foreign"))
        self.assertEqual(self.vm.group_files(self.gid), [])

    def test_inbound_relay_add_persists_and_emits(self):
        seen = []
        self.vm.group_files_changed.connect(seen.append)
        entry = GroupFileInfo(
            file_id="relayed",
            name="r.bin",
            size=5,
            sender_id="member-a",
            sender_name="A",
            ts=123,
            download_host="10.0.0.2",
            download_port=9999,
            file_key="k",
        )
        self.vm.group_file_add_received(self.vm.group_p2p_map[self.gid], entry)
        self.assertEqual([e.file_id for e in self.vm.group_files(self.gid)], ["relayed"])
        self.assertIn(self.gid, seen)

    def test_inbound_remove_authorization_gates_callback(self):
        self.vm.store.upsert_group_file(_entry(self.gid, "f1", "member-a"))
        p2p = self.vm.group_p2p_map[self.gid]
        # a third party must not remove it, and the relay rebroadcast is denied
        self.assertFalse(self.vm.group_file_remove_received(p2p, "f1", "member-b"))
        self.assertEqual([e.file_id for e in self.vm.group_files(self.gid)], ["f1"])
        # the uploader may
        self.assertTrue(self.vm.group_file_remove_received(p2p, "f1", "member-a"))
        self.assertEqual(self.vm.group_files(self.gid), [])

    def test_convergence_applies_tombstones_first(self):
        self.vm.store.upsert_group_file(_entry(self.gid, "old", "member-a"))
        entries = [
            GroupFileInfo(file_id="new", name="n.bin", size=1, sender_id="member-a", ts=10),
            GroupFileInfo(file_id="old", name="o.bin", size=1, sender_id="member-a", ts=20),
        ]
        p2p = self.vm.group_p2p_map[self.gid]
        self.vm.group_files_received(p2p, entries, ["old"])
        self.assertEqual([e.file_id for e in self.vm.group_files(self.gid)], ["new"])

    def test_restart_re_registers_own_shared_files(self):
        from localchat.view_model import ChatViewModel

        path = self._make_file("persist.bin", b"persisted bytes")
        p2p = self.vm.group_p2p_map[self.gid]
        p2p.send_group_packet = lambda pkt: None
        self.vm.mesh.broadcast_admin = lambda gid, pkt: None
        self.assertTrue(self.vm.share_group_file(path))
        file_id = self.vm.group_files(self.gid)[0].file_id
        self.vm.shutdown()
        # a fresh ViewModel over the same database must serve it again
        os.makedirs(DATA_DIR, exist_ok=True)
        vm2 = ChatViewModel(ChatStore(self.db_path), data_dir=DATA_DIR)
        try:
            self.assertIsNotNone(vm2.host_server._lookup_shared_file(file_id))
        finally:
            vm2.shutdown()

    def test_share_blocked_when_no_active_group(self):
        vm = make_vm(_fresh_db("lc_gf_nogroup.db"))
        try:
            vm.set_nickname("x")
            self.assertFalse(vm.share_group_file(self._make_file("z.bin", b"z")))
        finally:
            vm.shutdown()


class GroupFilesUiTest(unittest.TestCase):
    def setUp(self):
        install_identity()
        self._orig_tcp_port = network_module.TCP_PORT
        network_module.TCP_PORT = _free_port()
        self.vm = make_vm(_fresh_db("lc_gf_ui.db"))
        self.vm.set_nickname("\u7fa4\u6587\u4ef6")
        self.vm.create_group("\u7fa4\u6587\u4ef6", "\u6d4b\u8bd5\u7fa4")
        self.gid = self.vm.active_group_id

    def tearDown(self):
        self.vm.shutdown()
        network_module.TCP_PORT = self._orig_tcp_port

    def test_lobby_exposes_group_files_entry(self):
        from localchat.ui.group_lobby_page import GroupLobbyPage
        from PyQt6.QtWidgets import QPushButton

        page = GroupLobbyPage(self.vm, lambda: None, lambda: None, lambda: None)
        labels = {b.text() for b in page.findChildren(QPushButton)}
        self.assertIn("\u7fa4\u6587\u4ef6", labels)

    def test_dialog_lists_entries_and_gates_delete(self):
        from localchat.ui.group_files_dialog import GroupFilesDialog

        self.vm.store.upsert_group_file(_entry(self.gid, "mine", self.vm.my_device_id))
        dialog = GroupFilesDialog(self.vm)
        try:
            self.assertEqual(dialog.list.count(), 1)
            dialog.list.setCurrentRow(0)
            self.assertTrue(dialog.delete_btn.isEnabled())
            # a foreign upload from a plain member is not deletable
            p2p = self.vm.group_p2p_map[self.gid]
            p2p.is_host = False
            self.vm.store.set_setting("group_creator_id_%s" % self.gid, "owner-x")
            self.vm.store.upsert_group_file(_entry(self.gid, "foreign", "other"))
            dialog.refresh()
            self.assertEqual(dialog.list.count(), 2)
            for row in range(dialog.list.count()):
                item = dialog.list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == "foreign":
                    dialog.list.setCurrentItem(item)
                    break
            self.assertFalse(dialog.delete_btn.isEnabled())
        finally:
            dialog._detach()


if __name__ == "__main__":
    unittest.main()
