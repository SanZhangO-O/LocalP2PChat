"""Forward provenance + offline message-experience replay tests.

Covers the Windows half of:

  - the optional wire `forwarded` object (byte-identical when unset, round
    trips through the packet codec and the plaintext storage column);
  - the ViewModel send paths carrying provenance into the created message;
  - the durable pending-op log: an edit/reaction/pin that cannot reach any
    live path is staged, replayed in order once the group is reachable,
    replayed idempotently and survives a restart.

Chinese literals are written as \\uXXXX escapes so the file stays pure-ASCII.
"""

import os
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: F401  (offscreen app)

import localchat.network as network_module
from localchat.models import ChatMessage, ForwardedInfo
from localchat.storage import ChatStore, SavedGroup, to_saved_message
from tests.fake_peer import install_identity
from tests.test_functional import _fresh_db, make_vm

_APP = QApplication.instance() or QApplication([])


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _msg(mid="m1", content="hello", sender_id="me", sender_name="Me"):
    return ChatMessage(
        id=mid, content=content, timestamp=1, sender_id=sender_id,
        sender_name=sender_name,
    )


class ForwardedCodecTest(unittest.TestCase):
    def test_roundtrip_and_default_omitted(self):
        msg = _msg()
        msg.forwarded = ForwardedInfo("Bob", "G", 42)
        d = msg.to_dict()
        self.assertEqual(
            d["forwarded"],
            {"originSender": "Bob", "originGroup": "G", "originTime": 42},
        )
        back = ChatMessage.from_dict(d)
        self.assertEqual(back.forwarded.origin_sender, "Bob")
        self.assertEqual(back.forwarded.origin_group, "G")
        self.assertEqual(back.forwarded.origin_time, 42)

    def test_plain_message_bytes_unchanged(self):
        d = _msg().to_dict()
        self.assertNotIn("forwarded", d)

    def test_defaults_omitted_inside_forwarded(self):
        msg = _msg()
        msg.forwarded = ForwardedInfo("Bob")
        self.assertEqual(msg.to_dict()["forwarded"], {"originSender": "Bob"})

    def test_malformed_forwarded_fails_closed(self):
        d = _msg().to_dict()
        d["forwarded"] = "not-a-dict"
        self.assertIsNone(ChatMessage.from_dict(d).forwarded)

    def test_forwarded_column_roundtrip(self):
        path = os.path.join(tempfile.gettempdir(), "kilo", "lc_fwd.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        store = ChatStore(path)
        try:
            store.upsert_group(SavedGroup(group_id="g1", group_name="G", is_host=True))
            msg = _msg()
            msg.forwarded = ForwardedInfo("Bob", "G", 42)
            store.insert_message(to_saved_message("g1", msg))
            saved = store.get_messages_for_group("g1")[0]
            self.assertIn("originSender", saved.forwarded)
        finally:
            store.close()


class ForwardSendTest(unittest.TestCase):
    def setUp(self):
        install_identity()
        self._orig_tcp_port = network_module.TCP_PORT
        network_module.TCP_PORT = _free_port()
        self.vm = make_vm(_fresh_db("lc_fwd_vm.db"))
        self.vm.set_nickname("Me")
        self.vm.create_group("Me", "G")
        self.gid = self.vm.active_group_id

    def tearDown(self):
        self.vm.shutdown()
        network_module.TCP_PORT = self._orig_tcp_port

    def test_group_send_carries_forwarded(self):
        forwarded = ForwardedInfo("Alice", "Other", 7)
        self.assertTrue(
            self.vm.send_message("fwd text", forwarded=forwarded)
        )
        sent = self.vm.group_p2p_map[self.gid].messages[-1]
        self.assertIsNotNone(sent.forwarded)
        self.assertEqual(sent.forwarded.origin_sender, "Alice")
        self.assertEqual(sent.forwarded.origin_group, "Other")


class PendingOpReplayTest(unittest.TestCase):
    def setUp(self):
        install_identity()
        self._orig_tcp_port = network_module.TCP_PORT
        network_module.TCP_PORT = _free_port()
        self.db_path = _fresh_db("lc_pending.db")
        self.vm = make_vm(self.db_path)
        self.vm.set_nickname("Me")
        self.vm.create_group("Me", "G")
        self.gid = self.vm.active_group_id
        self.p2p = self.vm.group_p2p_map[self.gid]
        # a real target message exists (reactions/pins FK to saved_messages)
        self.vm.store.insert_message(to_saved_message(self.gid, _msg("m1")))
        # force "no live path" so the actions stage instead of sending
        self.p2p.connection_lost = True
        self.vm.mesh.has_links = lambda _gid: False

    def tearDown(self):
        self.vm.shutdown()
        network_module.TCP_PORT = self._orig_tcp_port

    def _ops(self):
        return self.vm.store.get_pending_ops(self.gid)

    def test_offline_reaction_is_staged_and_replayed(self):
        self.p2p.send_reaction = lambda *a, **k: False
        self.assertFalse(self.vm.toggle_group_reaction("m1", "\U0001f44d", True))
        ops = self._ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual((ops[0].kind, ops[0].message_id, ops[0].emoji), ("reaction", "m1", "\U0001f44d"))
        self.assertTrue(ops[0].active)

        # reconnect: replay hands it off, then the log is drained
        calls = []
        self.p2p.send_reaction = lambda mid, emoji, active, on_failed=None: calls.append(
            (mid, emoji, active)
        ) or True
        self.p2p.connection_lost = False
        self.vm._replay_pending_ops(self.gid)
        self.assertEqual(calls, [("m1", "\U0001f44d", True)])
        self.assertEqual(self._ops(), [])

    def test_offline_pin_is_staged_and_replayed(self):
        self.p2p.send_pin = lambda *a, **k: False
        self.assertFalse(self.vm.toggle_group_pin("m1", True))
        self.assertEqual([op.kind for op in self._ops()], ["pin"])
        calls = []
        self.p2p.send_pin = lambda mid, active, on_failed=None: calls.append(
            (mid, active)
        ) or True
        self.p2p.connection_lost = False
        self.vm._replay_pending_ops(self.gid)
        self.assertEqual(calls, [("m1", True)])
        self.assertEqual(self._ops(), [])

    def test_offline_edit_is_staged_and_replayed(self):
        self.p2p.edit_message = lambda mid, content: True
        self.assertTrue(self.vm.edit_message("m1", "edited"))
        self.assertEqual([op.kind for op in self._ops()], ["edit"])
        self.assertEqual(self._ops()[0].content, "edited")
        calls = []
        self.p2p.edit_message = lambda mid, content: calls.append((mid, content)) or True
        self.p2p.connection_lost = False
        self.vm._replay_pending_ops(self.gid)
        self.assertEqual(calls, [("m1", "edited")])
        self.assertEqual(self._ops(), [])

    def test_staging_is_idempotent_per_key(self):
        self.p2p.send_reaction = lambda *a, **k: False
        for active in (True, False, True):
            self.vm.toggle_group_reaction("m1", "\U0001f44d", active)
        ops = self._ops()
        self.assertEqual(len(ops), 1, "same (kind, message, emoji) must collapse")
        self.assertTrue(ops[0].active, "latest payload wins")

    def test_staged_ops_survive_restart(self):
        self.p2p.send_reaction = lambda *a, **k: False
        self.vm.toggle_group_reaction("m1", "\U0001f44d", True)
        self.vm.shutdown()
        vm2 = make_vm(self.db_path)
        try:
            ops = vm2.store.get_pending_ops(self.gid)
            self.assertEqual(len(ops), 1)
            self.assertEqual(ops[0].emoji, "\U0001f44d")
        finally:
            vm2.shutdown()

    def test_replay_waits_for_a_live_path(self):
        self.p2p.send_reaction = lambda *a, **k: False
        self.vm.toggle_group_reaction("m1", "\U0001f44d", True)
        calls = []
        self.p2p.send_reaction = lambda mid, emoji, active, on_failed=None: calls.append(mid) or True
        # still offline: replay must not hand anything off or drain the log
        self.vm._replay_pending_ops(self.gid)
        self.assertEqual(calls, [])
        self.assertEqual(len(self._ops()), 1)


if __name__ == "__main__":
    unittest.main()
