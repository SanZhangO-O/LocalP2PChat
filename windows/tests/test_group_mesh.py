"""Group mesh tests: members chat directly when the host is offline, and a
member coming online backfills missed history from other members.

Every mesh link is secured with the password-bound ECDH handshake from
securewire.py (mode "mesh") and every line after it is AES-256-GCM
encrypted - these tests drive the REAL secured handshake end to end over
loopback sockets, so no plaintext legacy path is exercised. network.py is
Qt-free so this works headless.

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.crypto import aes_gcm_decrypt, from_b64
from localchat.network import GroupMeshListener, GroupMeshManager, HostGroupServer
from localchat.models import ChatMessage, MAX_LINE_LENGTH, NetworkPacket, Peer
from localchat.securewire import Wire
from tests.fake_peer import wait_until


GRP = "\u7fa4G"
GRP_OTHER = "\u522b\u7684\u7fa4"
GROUP_PASSWORD = "123456"


class Rec(GroupMeshListener):
    def __init__(self):
        self.messages = []  # (group_id, ChatMessage)
        self.link_changes = []
        self.deletes = []  # (group_id, message_id, sender_id)

    def group_mesh_message(self, group_id: str, msgs) -> None:
        for m in msgs:
            self.messages.append((group_id, m))

    def group_mesh_links_changed(self, group_id: str) -> None:
        self.link_changes.append(group_id)

    def group_mesh_delete(self, group_id: str, message_id: str, sender_id: str) -> None:
        self.deletes.append((group_id, message_id, sender_id))


def make_msg(content: str, sender_id: str, sender_name: str, mid: str) -> ChatMessage:
    return ChatMessage(
        id=mid,
        content=content,
        timestamp=int(time.time() * 1000),
        sender_id=sender_id,
        sender_name=sender_name,
    )


class GroupMeshTest(unittest.TestCase):
    PORT_A = 19521
    PORT_B = 19522

    def setUp(self):
        # The mesh dialer retries a dead peer every RETRY_INTERVAL; shrink it
        # so "B comes online later" tests converge quickly instead of waiting
        # the production 10s between retries.
        self._old_retry = GroupMeshManager.RETRY_INTERVAL
        GroupMeshManager.RETRY_INTERVAL = 0.05

        self.server_a = HostGroupServer(self.PORT_A)
        self.server_a.ensure_running()
        self.rec_a = Rec()
        self.a = GroupMeshManager()
        self.a.attach(self.rec_a)
        self.server_a.mesh_manager = self.a
        # Mesh handshakes are password-bound: resolve each group's password
        # (None for groups this device does not know).
        self.server_a.password_lookup = lambda mode, gid: self.a.password_for(gid)

        self.server_b = HostGroupServer(self.PORT_B)
        self.server_b.ensure_running()
        self.rec_b = Rec()
        self.b = GroupMeshManager()
        self.b.attach(self.rec_b)
        self.server_b.mesh_manager = self.b
        self.server_b.password_lookup = lambda mode, gid: self.b.password_for(gid)

        self.peer_a = Peer("aaa-member", "\u6210\u5458A", "127.0.0.1", self.PORT_A)
        self.peer_b = Peer("bbb-member", "\u6210\u5458B", "127.0.0.1", self.PORT_B)

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server_a.shutdown()
        self.server_b.shutdown()
        GroupMeshManager.RETRY_INTERVAL = self._old_retry

    def _link(self):
        # B enters first so A's dial (smaller id) succeeds immediately; the
        # link requires the right group password on both sides.
        self.b.enter_group(GRP, self.peer_b, [self.peer_a], [], GROUP_PASSWORD)
        self.a.enter_group(GRP, self.peer_a, [self.peer_b], [], GROUP_PASSWORD)
        self.assertTrue(
            wait_until(lambda: self.a.has_links(GRP) and self.b.has_links(GRP)),
            "mesh links should establish over the secured handshake",
        )

    def test_members_chat_when_host_offline(self):
        """A broadcasts directly to B with no host in the loop at all."""
        self._link()
        msg = make_msg(
            "\u4e3b\u673a\u4e0d\u5728\u7ebf\u4e5f\u80fd\u804a",
            "aaa-member", "\u6210\u5458A", "m-1",
        )
        self.a.broadcast(GRP, msg)
        self.assertTrue(
            wait_until(lambda: any(m.id == "m-1" for _, m in self.rec_b.messages)),
            "B should receive A's message over the mesh",
        )
        # and B can answer back
        msg2 = make_msg(
            "\u6536\u5230\uff0c\u6211\u8fd9\u8fb9\u4e5f\u662f\u76f4\u8fde",
            "bbb-member", "\u6210\u5458B", "m-2",
        )
        self.b.broadcast(GRP, msg2)
        self.assertTrue(
            wait_until(lambda: any(m.id == "m-2" for _, m in self.rec_a.messages)),
            "A should receive B's reply over the mesh",
        )

    def test_offline_member_backfills_history(self):
        """A has history; B enters later with none and gets backfilled."""
        old = make_msg(
            "\u6211\u4e0d\u5728\u65f6\u7684\u6d88\u606f1",
            "aaa-member", "\u6210\u5458A", "h-1",
        )
        old2 = make_msg(
            "\u6211\u4e0d\u5728\u65f6\u7684\u6d88\u606f2",
            "bbb-member", "\u6210\u5458B", "h-2",
        )
        self.a.enter_group(GRP, self.peer_a, [self.peer_b], [old, old2], GROUP_PASSWORD)
        # B comes online later with no history; the link push backfills it
        self.b.enter_group(GRP, self.peer_b, [self.peer_a], [], GROUP_PASSWORD)
        self.assertTrue(
            wait_until(
                lambda: any(m.id == "h-1" for _, m in self.rec_b.messages)
                and any(m.id == "h-2" for _, m in self.rec_b.messages)
            ),
            f"B should backfill A's history, got {self.rec_b.messages}",
        )

    def test_mesh_broadcast_dedups(self):
        """The same message id never lands twice (host relay + mesh overlap)."""
        self._link()
        msg = make_msg(
            "\u53bb\u91cd",
            "aaa-member", "\u6210\u5458A", "dup-1",
        )
        self.a.broadcast(GRP, msg)
        self.a.broadcast(GRP, msg)  # duplicate send
        self.assertTrue(wait_until(lambda: any(m.id == "dup-1" for _, m in self.rec_b.messages)))
        time.sleep(0.3)
        hits = [m for _, m in self.rec_b.messages if m.id == "dup-1"]
        self.assertEqual(len(hits), 1, "duplicate message id must be merged")

    def test_unrelated_mesh_hello_rejected(self):
        """A mesh hello for a group we are not in is refused at the (password
        bound) handshake: A knows no password for that group, so the link
        cannot form."""
        self.a.enter_group(GRP, self.peer_a, [self.peer_b], [], GROUP_PASSWORD)
        # C tries to link a group A is not in (use a third manager). C's id is
        # smaller than A's so C is the dialer toward A.
        server_c = HostGroupServer(self.PORT_A + 10)
        server_c.ensure_running()
        rec_c = Rec()
        c = GroupMeshManager()
        c.attach(rec_c)
        server_c.mesh_manager = c
        server_c.password_lookup = lambda mode, gid: c.password_for(gid)
        try:
            c.enter_group(
                GRP_OTHER,
                Peer("aa-member", "\u6210\u5458C", "127.0.0.1", self.PORT_A),
                [self.peer_a], [], GROUP_PASSWORD,
            )
            time.sleep(0.5)
            self.assertFalse(self.a.has_links(GRP), "A must not link for a group it is not in")
            self.assertFalse(c.has_links(GRP_OTHER), "C must not link to a group A refused")
        finally:
            c.shutdown()
            server_c.shutdown()

    def test_mesh_delete_converges_when_host_offline(self):
        """broadcast_delete reaches every link so deletes converge without the
        host, and only the original sender may delete."""
        self._link()
        msg = make_msg(
            "\u5220\u9664\u6211",
            "aaa-member", "\u6210\u5458A", "del-1",
        )
        self.a.broadcast(GRP, msg)
        self.assertTrue(wait_until(lambda: any(m.id == "del-1" for _, m in self.rec_b.messages)))
        self.a.broadcast_delete(GRP, "del-1")
        self.assertTrue(
            wait_until(
                lambda: any(d[0] == GRP and d[1] == "del-1" for d in self.rec_b.deletes)
            ),
            "B should hear the mesh delete and remove the message",
        )
        self.assertEqual(
            self.rec_b.deletes[-1][2],
            "aaa-member",
            "the delete must be attributed to the original sender",
        )

    def test_history_pushed_in_bounded_batches(self):
        """History push must be split so every written (encrypted) line stays
        under MAX_LINE_LENGTH: the receiver reads with a bounded line reader
        and drops the link on an oversized line (Android parity: 48KB
        chunks). The lines are Base64(AES-GCM(json)), so the plaintext batch
        bound has to leave room for the ciphertext expansion."""
        mm = GroupMeshManager()
        mm.attach(Rec())
        history = [
            make_msg("\u957f" * 2000, "aaa-member", "\u6210\u5458A", f"big-{i}")
            for i in range(30)
        ]
        lines = []
        key = os.urandom(32)
        wire = Wire(lambda: None, lines.append)
        wire.activate(key)
        mm._send_history(wire, GRP, history)
        self.assertTrue(lines, "history must be written")
        self.assertGreater(len(lines), 1, "large history must be split into batches")
        total = 0
        for line in lines:
            self.assertLessEqual(
                len(line), MAX_LINE_LENGTH, "every encrypted line must fit the bounded reader"
            )
            plain = aes_gcm_decrypt(key, from_b64(line)).decode("utf-8")
            packet = NetworkPacket.from_json(plain)
            self.assertEqual(packet.type, "history_reply")
            self.assertEqual(packet.group_id, GRP)
            total += len(packet.messages)
        self.assertEqual(total, len(history), "no message may be lost across batches")

    def test_large_history_backfills_in_batches(self):
        """A history bigger than one bounded line is fully backfilled over real
        mesh links (each batch merged independently, dedup by id)."""
        history = [
            make_msg(f"\u6d88\u606f {i} " + "x" * 300, "aaa-member", "\u6210\u5458A", f"h-{i}")
            for i in range(150)
        ]
        self.a.enter_group(GRP, self.peer_a, [self.peer_b], history, GROUP_PASSWORD)
        self.b.enter_group(GRP, self.peer_b, [self.peer_a], [], GROUP_PASSWORD)
        self.assertTrue(
            wait_until(
                lambda: len({m.id for _, m in self.rec_b.messages}) >= 150,
                timeout=12.0,
            ),
            f"B should backfill the whole large history, got {len(self.rec_b.messages)}",
        )


if __name__ == "__main__":
    unittest.main()
