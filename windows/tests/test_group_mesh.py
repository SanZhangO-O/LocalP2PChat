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
        self.deleted_ids = []  # (group_id, [msgId...]) tombstone convergence

    def group_mesh_message(self, group_id: str, msgs) -> None:
        for m in msgs:
            self.messages.append((group_id, m))

    def group_mesh_links_changed(self, group_id: str) -> None:
        self.link_changes.append(group_id)

    def group_mesh_delete(self, group_id: str, message_id: str, sender_id: str) -> None:
        self.deletes.append((group_id, message_id, sender_id))

    def group_mesh_deleted_ids(self, group_id: str, deleted_ids) -> None:
        self.deleted_ids.append((group_id, list(deleted_ids)))


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

    def test_broadcast_delete_cleans_local_mesh_history(self):
        """broadcast_delete also removes the message from the SENDER's own
        mesh history: a member linking up later must not get the deleted
        message backfilled (regression: deletes resurrected via history
        push)."""
        self._link()
        msg = make_msg(
            "\u5f85\u5220\u9664", "aaa-member", "\u6210\u5458A", "gone-1"
        )
        self.a.broadcast(GRP, msg)
        self.assertTrue(wait_until(lambda: any(m.id == "gone-1" for _, m in self.rec_b.messages)))
        self.a.broadcast_delete(GRP, "gone-1")
        self.assertTrue(
            wait_until(lambda: any(d[1] == "gone-1" for d in self.rec_a.deletes)),
            "the sender must run its own local delete path (mesh state cleaned)",
        )
        with self.a._lock:
            ids = [m.id for m in self.a._groups[GRP]["messages"]]
        self.assertNotIn("gone-1", ids, "the deleted message must leave the mesh history")

    def test_broadcast_dedups_local_mesh_state(self):
        """A duplicated broadcast keeps ONE copy in the sender's history, so
        a late joiner backfills the message exactly once (Android
        noteMessage parity)."""
        self.a.enter_group(GRP, self.peer_a, [self.peer_b], [], GROUP_PASSWORD)
        msg = make_msg(
            "\u53bb\u91cd\u5386\u53f2", "aaa-member", "\u6210\u5458A", "dup-h"
        )
        self.a.broadcast(GRP, msg)
        self.a.broadcast(GRP, msg)
        with self.a._lock:
            hits = [m for m in self.a._groups[GRP]["messages"] if m.id == "dup-h"]
        self.assertEqual(len(hits), 1, "sender-side state must dedupe by message id")

    def test_history_push_carries_deleted_ids_byte_contract(self):
        """Tombstones travel in a DEDICATED history_reply after the batches
        (Android parity: sendHistory/sendDeletedIds). Batches never carry
        deletedIds, so the HISTORY_CHUNK_BYTES budget never has to account for
        them; the tombstone packet is also sent when the history is empty
        (everything the peer missed may have been deleted while it was away),
        and with no tombstones no extra packet is written (byte-level wire
        compat: the key stays absent entirely)."""
        self.a.deleted_ids_provider = lambda gid: []
        old = make_msg("\u5386\u53f2", "aaa-member", "\u6210\u5458A", "h-x")
        key = os.urandom(32)
        lines = []
        wire = Wire(lambda: None, lines.append)
        wire.activate(key)
        self.a._send_history(wire, GRP, [old])
        self.assertEqual(len(lines), 1)
        packet = NetworkPacket.from_json(
            aes_gcm_decrypt(key, from_b64(lines[0])).decode("utf-8")
        )
        self.assertIsNone(packet.deleted_ids, "batches must never carry deletedIds")
        self.assertNotIn("deletedIds", packet.to_dict())

        # history + tombstones: batch first (no deletedIds), then the
        # dedicated tombstone-only packet
        self.a.deleted_ids_provider = lambda gid: ["h-x", "h-gone"]
        lines.clear()
        self.a._send_history(wire, GRP, [old])
        self.assertEqual(len(lines), 2)
        batch = NetworkPacket.from_json(
            aes_gcm_decrypt(key, from_b64(lines[0])).decode("utf-8")
        )
        self.assertIsNone(batch.deleted_ids)
        tomb = NetworkPacket.from_json(
            aes_gcm_decrypt(key, from_b64(lines[1])).decode("utf-8")
        )
        self.assertEqual(tomb.deleted_ids, ["h-x", "h-gone"])
        self.assertFalse(tomb.messages)

        # empty history + tombstones: the dedicated packet still goes out
        lines.clear()
        self.a._send_history(wire, GRP, [])
        self.assertEqual(len(lines), 1)
        tomb = NetworkPacket.from_json(
            aes_gcm_decrypt(key, from_b64(lines[0])).decode("utf-8")
        )
        self.assertEqual(tomb.deleted_ids, ["h-x", "h-gone"])
        self.assertFalse(tomb.messages)

    def test_mesh_deleted_ids_converge_without_rebroadcast(self):
        """Received deletedIds drop the named messages from the local mesh
        history and surface to the listener -- and are never re-broadcast as
        deletes (convergence is not a new delete event)."""
        self._link()
        msg = make_msg(
            "\u5c06\u88ab\u6536\u655b", "aaa-member", "\u6210\u5458A", "conv-1"
        )
        self.a.broadcast(GRP, msg)
        self.assertTrue(wait_until(lambda: any(m.id == "conv-1" for _, m in self.rec_b.messages)))
        self.b.apply_deleted_ids(GRP, ["conv-1", "never-seen"])
        self.assertTrue(
            wait_until(
                lambda: any(
                    d[0] == GRP and d[1] == ["conv-1", "never-seen"]
                    for d in self.rec_b.deleted_ids
                )
            ),
            "B must surface the convergence data to its listener",
        )
        with self.b._lock:
            ids = [m.id for m in self.b._groups[GRP]["messages"]]
        self.assertNotIn("conv-1", ids, "the named message must leave B's mesh history")
        self.assertFalse(
            any(d[1] == "conv-1" for d in self.rec_b.deletes),
            "convergence must not be rebroadcast as a delete",
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

    def test_history_reply_cannot_overwrite_existing_message(self):
        """A history batch entry reusing a locally-known id with DIFFERENT
        content is accepted ONLY when the author pushes its OWN message over
        its OWN link (that is how edits converge to a member that was
        offline). The author id claimed over another member's link, or a
        DIFFERENT sender's id, must never rewrite stored history. Brand-new
        ids in the same batch are still accepted."""
        self._link()
        original = make_msg(
            "\u539f\u59cb\u5185\u5bb9", "aaa-member", "\u6210\u5458A", "h-conflict",
        )
        self.a.broadcast(GRP, original)
        self.assertTrue(
            wait_until(lambda: any(m.id == "h-conflict" for _, m in self.rec_b.messages)),
            "B should hold the original message first",
        )
        # A pushes a history batch over its live link to B: one entry is the
        # author's OWN edited message (accepted as edit convergence), one
        # reuses a foreign author's id with tampered content (dropped), and
        # one is brand new.
        edited_own = make_msg(
            "\u4fee\u6539\u540e\u7684\u5185\u5bb9", "aaa-member", "\u6210\u5458A", "h-conflict",
        )
        forged_foreign = make_msg(
            "\u7be1\u6539\u8fc7\u7684\u5185\u5bb9", "ccc-member", "\u6210\u5458C", "h-foreign",
        )
        fresh = make_msg(
            "\u5168\u65b0\u5386\u53f2", "aaa-member", "\u6210\u5458A", "h-fresh",
        )
        # B must first hold a message authored by the FOREIGN id so the forged
        # entry actually collides with a locally-known row
        foreign_original = make_msg(
            "\u5916\u6765\u539f\u6587", "ccc-member", "\u6210\u5458C", "h-foreign",
        )
        self.b.note_message(GRP, foreign_original)
        links = list(self.a._groups[GRP]["links"].values())
        self.assertTrue(links, "A must have a live link to push over")
        for link in links:
            link["wire"].send_packet(
                NetworkPacket(
                    type="history_reply",
                    group_id=GRP,
                    messages=[edited_own, forged_foreign, fresh],
                )
            )
        self.assertTrue(
            wait_until(lambda: any(m.id == "h-fresh" for _, m in self.rec_b.messages)),
            "the new entry in the batch must still be accepted",
        )
        # the author's own rewrite converges (edit semantics)
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.id == "h-conflict" and m.content == "\u4fee\u6539\u540e\u7684\u5185\u5bb9"
                    for _, m in self.rec_b.messages
                )
            ),
            "an author pushing its own edited message over its own link must converge",
        )
        time.sleep(0.3)
        stored = [
            m for m in self.b._groups[GRP]["messages"] if m.id == "h-foreign"
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(
            stored[0].content,
            "\u5916\u6765\u539f\u6587",
            "a foreign author's id claimed over another member's link must be dropped",
        )
        self.assertFalse(
            any(m.content == "\u7be1\u6539\u8fc7\u7684\u5185\u5bb9" for _, m in self.rec_b.messages),
            "the tampered content must never surface",
        )

    def test_add_peer_ignores_invalid_endpoints(self):
        """mesh_announce carries attacker-controlled peer records: an empty
        host or an out-of-range port is ignored entirely (never dialed,
        never entered into the roster)."""
        mm = GroupMeshManager()
        mm.attach(Rec())
        # "zz-me" sorts after every peer id below, so no dial thread spawns
        mm.enter_group(GRP, Peer("zz-me", "me", "127.0.0.1", 1), [], [], GROUP_PASSWORD)
        try:
            for bad in (
                Peer("p1", "p", "", 1000),          # empty host
                Peer("p2", "p", "10.0.0.1", 0),     # port 0
                Peer("p3", "p", "10.0.0.1", 65536), # out of range
                Peer("p4", "p", "10.0.0.1", -1),    # negative
            ):
                mm.add_peer(GRP, bad)
            self.assertEqual(mm._groups[GRP]["peers"], {}, "invalid endpoints must be ignored")
            mm.add_peer(GRP, Peer("p5", "p", "10.0.0.1", 5000))
            self.assertIn("p5", mm._groups[GRP]["peers"])
        finally:
            mm.shutdown()

    def test_add_peer_roster_capped(self):
        """The per-group roster stops growing at MAX_PEERS_PER_GROUP: a NEW
        id is ignored once full, while an already-known id is still
        refreshed (e.g. an endpoint update)."""
        mm = GroupMeshManager()
        mm.attach(Rec())
        mm.enter_group(GRP, Peer("zz-me", "me", "127.0.0.1", 1), [], [], GROUP_PASSWORD)
        try:
            cap = GroupMeshManager.MAX_PEERS_PER_GROUP
            for i in range(cap):
                mm.add_peer(GRP, Peer(f"q-{i:03d}", "p", "10.0.0.1", 5000))
            self.assertEqual(len(mm._groups[GRP]["peers"]), cap)
            mm.add_peer(GRP, Peer("q-new", "p", "10.0.0.1", 5000))
            self.assertNotIn("q-new", mm._groups[GRP]["peers"], "the roster is capped")
            mm.add_peer(GRP, Peer("q-000", "p", "10.0.0.2", 6000))
            self.assertEqual(
                mm._groups[GRP]["peers"]["q-000"].ip_address, "10.0.0.2",
                "a known id is still refreshed at the cap",
            )
        finally:
            mm.shutdown()


if __name__ == "__main__":
    unittest.main()
