"""Message-experience features: edit / reactions / pins / group read
receipts / mentions / voice kind detection.

Covers the wire layer (new packet types round-trip and validate strictly,
plain messages stay byte-identical), the store (reactions / pins / group
reads / author edits, FK cleanup, conversation re-keying), the group mesh
(author-only edit delivery + forged rejections + reaction/pin/receipt
delivery) and the direct session (edit/reaction/pin with sender checks).

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.models import (
    FILE_KIND_AUDIO,
    FILE_KIND_FILE,
    MENTION_ALL,
    ChatMessage,
    NetworkPacket,
    detect_media_kind,
    is_big_emoji,
    sanitize_emoji,
    sanitize_mentions,
)
from localchat.network import (
    DirectChatListener,
    DirectChatManager,
    GroupMeshListener,
    GroupMeshManager,
    HostGroupServer,
    Peer,
)
from localchat.storage import ChatStore, SavedMessage, to_saved_message
from tests.fake_peer import install_identity


def wait_until(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ----------------------------------------------------------------- wire layer


class WireFormatTest(unittest.TestCase):
    def test_plain_message_stays_byte_identical(self):
        """A message without the new fields must serialize exactly like the
        pre-extras format (backward compatibility is a hard constraint)."""
        msg = ChatMessage(
            id="m-1",
            content="hi",
            timestamp=123,
            sender_id="dev-A",
            sender_name="A",
        )
        self.assertEqual(
            msg.to_dict(),
            {
                "id": "m-1",
                "content": "hi",
                "timestamp": 123,
                "senderId": "dev-A",
                "senderName": "A",
            },
        )

    def test_message_mentions_edited_roundtrip(self):
        msg = ChatMessage(
            id="m-2",
            content="hey",
            timestamp=5,
            sender_id="dev-A",
            sender_name="A",
            mentions=["dev-B", MENTION_ALL],
            edited=True,
        )
        d = msg.to_dict()
        self.assertEqual(d["mentions"], ["dev-B", "all"])
        self.assertTrue(d["edited"])
        parsed = ChatMessage.from_dict(d)
        self.assertEqual(parsed.mentions, ["dev-B", MENTION_ALL])
        self.assertTrue(parsed.edited)

    def test_message_edited_rejects_non_bool(self):
        with self.assertRaises(ValueError):
            ChatMessage.from_dict(
                {
                    "id": "m",
                    "content": "x",
                    "timestamp": 1,
                    "senderId": "a",
                    "senderName": "a",
                    "edited": "true",
                }
            )

    def test_edit_message_packet_roundtrip_and_validation(self):
        pkt = NetworkPacket(
            type="edit_message",
            group_id="g",
            message_id="m-1",
            sender_id="dev-A",
            new_content="new body",
        )
        parsed = NetworkPacket.from_json(pkt.to_json())
        self.assertEqual(parsed.new_content, "new body")
        self.assertEqual(parsed.message_id, "m-1")
        # missing fields fail closed
        with self.assertRaises(ValueError):
            NetworkPacket.from_dict({"type": "edit_message", "messageId": "m"})
        with self.assertRaises(ValueError):
            NetworkPacket.from_dict(
                {
                    "type": "edit_message",
                    "messageId": "m",
                    "senderId": "a",
                    "newContent": "   ",
                }
            )

    def test_reaction_packet_roundtrip_and_validation(self):
        pkt = NetworkPacket(
            type="reaction",
            group_id="g",
            message_id="m-1",
            sender_id="dev-A",
            emoji="\U0001F44D",
            active=True,
        )
        parsed = NetworkPacket.from_json(pkt.to_json())
        self.assertEqual(parsed.emoji, "\U0001F44D")
        self.assertTrue(parsed.active)
        with self.assertRaises(ValueError):
            NetworkPacket.from_dict(
                {"type": "reaction", "messageId": "m", "senderId": "a"}
            )

    def test_pin_message_packet_roundtrip_and_validation(self):
        pkt = NetworkPacket(
            type="pin_message", group_id="g", message_id="m-1",
            sender_id="dev-A", active=True,
        )
        parsed = NetworkPacket.from_json(pkt.to_json())
        self.assertEqual(parsed.type, "pin_message")
        self.assertTrue(parsed.active)
        with self.assertRaises(ValueError):
            NetworkPacket.from_dict(
                {"type": "pin_message", "senderId": "a", "active": True}
            )

    def test_sanitize_emoji_and_mentions(self):
        self.assertEqual(sanitize_emoji(" \U0001F600\n"), "\U0001F600")
        self.assertEqual(sanitize_emoji("ab\u0007cd"), "abcd")
        self.assertEqual(sanitize_emoji("x" * 99), "x" * 16)
        self.assertEqual(sanitize_mentions(["a", "a", "", "b"]), ["a", "b"])
        self.assertEqual(sanitize_mentions("nope"), [])
        self.assertEqual(len(sanitize_mentions([str(i) for i in range(999)])), 64)

    def test_big_emoji_detection(self):
        self.assertTrue(is_big_emoji("\U0001F600"))
        self.assertTrue(is_big_emoji("\U0001F600\U0001F601"))
        self.assertTrue(is_big_emoji("\U0001F44D"))  # thumbs up
        self.assertFalse(is_big_emoji("hello"))
        self.assertFalse(is_big_emoji("\U0001F600 text"))
        self.assertFalse(is_big_emoji("a" * 20))

    def test_audio_kind_detection(self):
        self.assertEqual(detect_media_kind("voice.WAV"), FILE_KIND_AUDIO)
        self.assertEqual(detect_media_kind("song.mp3"), FILE_KIND_AUDIO)
        self.assertEqual(detect_media_kind("clip.m4a"), FILE_KIND_AUDIO)
        self.assertEqual(detect_media_kind("paper.txt"), FILE_KIND_FILE)
        # unknown kinds degrade to a plain file (old-peer parity)
        from localchat.models import normalize_media_kind
        self.assertEqual(normalize_media_kind("audio"), FILE_KIND_AUDIO)
        self.assertEqual(normalize_media_kind("hologram"), FILE_KIND_FILE)


# ---------------------------------------------------------------------- store


class ExtrasStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ChatStore(os.path.join(self.dir.name, "t.db"))
        self.store.upsert_group(
            type("G", (), {"group_id": "g1", "group_name": "g", "is_host": True})()
            if False
            else __import__("localchat.storage", fromlist=["SavedGroup"]).SavedGroup(
                group_id="g1", group_name="g", is_host=True
            )
        )
        self._gid = 0

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _msg(self, mid="m-1", content="body", sender="dev-A", mine=False, ts=None, group="g1"):
        self._gid += 1
        return SavedMessage(
            id=mid,
            group_id=group,
            content=content,
            timestamp=ts or 1000 + self._gid,
            sender_id=sender,
            sender_name="A",
            is_from_me=mine,
        )

    def test_edit_updates_content_and_flag(self):
        self.store.insert_message(self._msg())
        self.assertTrue(self.store.update_message_content("g1", "m-1", "edited"))
        row = self.store.get_messages_for_group("g1")[0]
        self.assertEqual(row.content, "edited")
        self.assertTrue(row.edited)
        self.assertFalse(self.store.update_message_content("g1", "missing", "x"))

    def test_update_message_content_author_gate(self):
        """Defense in depth: an edit naming a different author must never
        rewrite the stored text, even if a caller skipped the network-layer
        authorization (regression: the forged direct edit was persisted)."""
        self.store.insert_message(self._msg(sender="dev-A"))
        self.assertFalse(
            self.store.update_message_content(
                "g1", "m-1", "forged", sender_id="dev-B"
            )
        )
        row = self.store.get_messages_for_group("g1")[0]
        self.assertEqual(row.content, "body")
        self.assertFalse(row.edited)
        self.assertTrue(
            self.store.update_message_content("g1", "m-1", "real", sender_id="dev-A")
        )
        row = self.store.get_messages_for_group("g1")[0]
        self.assertEqual(row.content, "real")
        self.assertTrue(row.edited)

    def test_record_group_reads_reports_new_rows_only(self):
        """A repeat receipt inserts nothing and must report 0, so the caller
        can skip a full UI rebuild (N members x M messages otherwise)."""
        self.store.insert_messages([self._msg("m-1", mine=True)])
        self.assertEqual(self.store.record_group_reads("g1", ["m-1"], "dev-B"), 1)
        self.assertEqual(self.store.record_group_reads("g1", ["m-1"], "dev-B"), 0)
        self.assertEqual(self.store.record_group_reads("g1", ["m-1"], "dev-C"), 1)

    def test_reactions_add_remove_and_cascade(self):
        self.store.insert_message(self._msg())
        self.store.add_reaction("g1", "m-1", "\U0001F44D", "dev-B")
        self.store.add_reaction("g1", "m-1", "\U0001F44D", "dev-C")
        self.store.add_reaction("g1", "m-1", "\u2764", "dev-B")
        data = self.store.get_reactions("g1")
        self.assertEqual(len(data["m-1"]), 3)
        self.store.remove_reaction("g1", "m-1", "\U0001F44D", "dev-B")
        data = self.store.get_reactions("g1")
        self.assertEqual(len(data["m-1"]), 2)
        # deleting the message cleans its reactions (FK cascade)
        self.store.delete_message("g1", "m-1")
        self.assertEqual(self.store.get_reactions("g1"), {})

    def test_pins_set_get_and_order(self):
        self.store.insert_messages([self._msg("m-1"), self._msg("m-2")])
        self.store.set_message_pinned("g1", "m-2", True, "dev-B", pinned_at=200)
        self.store.set_message_pinned("g1", "m-1", True, "dev-C", pinned_at=100)
        pins = self.store.get_pinned_messages("g1")
        self.assertEqual([p[0] for p in pins], ["m-1", "m-2"], "oldest pin first")
        self.store.set_message_pinned("g1", "m-1", False)
        pins = self.store.get_pinned_messages("g1")
        self.assertEqual([p[0] for p in pins], ["m-2"])

    def test_group_reads_record_and_query(self):
        self.store.insert_messages([self._msg("m-1", mine=True), self._msg("m-2", mine=True)])
        self.store.record_group_reads("g1", ["m-1", "m-2"], "dev-B")
        self.store.record_group_reads("g1", ["m-1"], "dev-C")
        readers = self.store.get_group_readers("g1")
        self.assertEqual(sorted(readers["m-1"]), ["dev-B", "dev-C"])
        self.assertEqual(readers["m-2"], ["dev-B"])

    def test_move_rekeys_extras(self):
        self.store.insert_message(self._msg())
        self.store.add_reaction("g1", "m-1", "\U0001F44D", "dev-B")
        self.store.set_message_pinned("g1", "m-1", True, "dev-B")
        self.store.record_group_reads("g1", ["m-1"], "dev-B")
        self.store.upsert_group(
            __import__("localchat.storage", fromlist=["SavedGroup"]).SavedGroup(
                group_id="g2", group_name="g2", is_host=False
            )
        )
        self.store.move_messages("g1", "g2")
        self.assertEqual(list(self.store.get_reactions("g2")), ["m-1"])
        self.assertEqual([p[0] for p in self.store.get_pinned_messages("g2")], ["m-1"])
        self.assertEqual(list(self.store.get_group_readers("g2")), ["m-1"])

    def test_reinsert_keeps_extras(self):
        """A re-insert of the same (groupId, id) must refresh the payload
        WITHOUT deleting the row: UPSERT, never OR REPLACE -- REPLACE is
        delete+insert and the FK cascade would silently drop the message's
        reactions / pins / read receipts."""
        self.store.insert_message(self._msg(content="first"))
        self.store.add_reaction("g1", "m-1", "\U0001F44D", "dev-B")
        self.store.set_message_pinned("g1", "m-1", True, "dev-B")
        self.store.record_group_reads("g1", ["m-1"], "dev-C")
        # same id arrives again (history replay / outbox redelivery)
        self.store.insert_message(self._msg(content="second"))
        self.assertEqual(self.store.get_messages_for_group("g1")[0].content, "second")
        self.assertEqual(len(self.store.get_reactions("g1")["m-1"]), 1)
        self.assertEqual([p[0] for p in self.store.get_pinned_messages("g1")], ["m-1"])
        self.assertEqual(self.store.get_group_readers("g1")["m-1"], ["dev-C"])

    def test_move_into_existing_target_keeps_target_extras(self):
        """Idempotent re-move / placeholder collision: the target already holds
        a message id with its own extras; the move must not cascade them away
        (the old INSERT OR REPLACE deleted the conflicting parent row)."""
        self.store.insert_message(self._msg(content="source copy"))
        self.store.add_reaction("g1", "m-1", "\U0001F44D", "dev-B")
        self.store.upsert_group(
            __import__("localchat.storage", fromlist=["SavedGroup"]).SavedGroup(
                group_id="g2", group_name="g2", is_host=False
            )
        )
        self.store.insert_message(self._msg(content="target copy", group="g2"))
        self.store.add_reaction("g2", "m-1", "\u2764", "dev-C")
        self.store.set_message_pinned("g2", "m-1", True, "dev-C")
        self.store.move_messages("g1", "g2")
        reactions = self.store.get_reactions("g2")["m-1"]
        self.assertEqual(sorted(e for e, _a in reactions), ["\u2764", "\U0001F44D"])
        self.assertEqual([p[0] for p in self.store.get_pinned_messages("g2")], ["m-1"])

    def test_targeted_read_receipt_lookups(self):
        self.store.insert_messages(
            [self._msg("m-1", mine=True, ts=100), self._msg("m-2", mine=True, ts=200),
             self._msg("m-3", ts=150)]
        )
        self.assertEqual(self.store.get_message_timestamp("g1", "m-2"), 200)
        self.assertIsNone(self.store.get_message_timestamp("g1", "missing"))
        self.assertEqual(
            sorted(self.store.get_own_message_ids_upto("g1", 200)), ["m-1", "m-2"]
        )
        self.assertEqual(self.store.get_own_message_ids_upto("g1", 99), [])

    def test_to_saved_message_carries_mentions(self):
        import json

        msg = ChatMessage(
            id="m-9",
            content="x",
            timestamp=1,
            sender_id="a",
            sender_name="a",
            mentions=[MENTION_ALL],
            edited=True,
        )
        saved = to_saved_message("g1", msg)
        self.assertTrue(saved.edited)
        self.assertEqual(json.loads(saved.mentions), ["all"])


# ------------------------------------------------------------------ mesh paths


class MeshRec(GroupMeshListener):
    def __init__(self):
        self.messages = []
        self.edits = []
        self.reactions = []
        self.pins = []
        self.reads = []

    def group_mesh_message(self, group_id, msgs):
        for m in msgs:
            self.messages.append((group_id, m))

    def group_mesh_edit(self, group_id, message_id, new_content, sender_id):
        self.edits.append((group_id, message_id, new_content, sender_id))

    def group_mesh_reaction(self, group_id, message_id, emoji, sender_id, active):
        self.reactions.append((group_id, message_id, emoji, sender_id, active))

    def group_mesh_pin(self, group_id, message_id, sender_id, active):
        self.pins.append((group_id, message_id, sender_id, active))

    def group_mesh_read_receipt(self, group_id, reader_id, up_to_id):
        self.reads.append((group_id, reader_id, up_to_id))


def make_msg(content, sender_id, sender_name, mid):
    return ChatMessage(
        id=mid,
        content=content,
        timestamp=int(time.time() * 1000),
        sender_id=sender_id,
        sender_name=sender_name,
    )


class MeshExtrasTest(unittest.TestCase):
    PORT_A = 19621
    PORT_B = 19622
    GRP = "\u7fa4G"
    PASSWORD = "123456"

    def setUp(self):
        self._old_retry = GroupMeshManager.RETRY_INTERVAL
        GroupMeshManager.RETRY_INTERVAL = 0.05
        self.server_a = HostGroupServer(self.PORT_A)
        self.server_a.ensure_running()
        self.rec_a = MeshRec()
        self.a = GroupMeshManager()
        self.a.attach(self.rec_a)
        self.server_a.mesh_manager = self.a
        self.server_a.password_lookup = lambda mode, gid: self.a.password_for(gid)
        self.server_b = HostGroupServer(self.PORT_B)
        self.server_b.ensure_running()
        self.rec_b = MeshRec()
        self.b = GroupMeshManager()
        self.b.attach(self.rec_b)
        self.server_b.mesh_manager = self.b
        self.server_b.password_lookup = lambda mode, gid: self.b.password_for(gid)
        self.peer_a = Peer("aaa-member", "A", "127.0.0.1", self.PORT_A)
        self.peer_b = Peer("bbb-member", "B", "127.0.0.1", self.PORT_B)
        self.b.enter_group(self.GRP, self.peer_b, [self.peer_a], [], self.PASSWORD)
        self.a.enter_group(self.GRP, self.peer_a, [self.peer_b], [], self.PASSWORD)
        self.assertTrue(
            wait_until(lambda: self.a.has_links(self.GRP) and self.b.has_links(self.GRP))
        )

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server_a.shutdown()
        self.server_b.shutdown()
        GroupMeshManager.RETRY_INTERVAL = self._old_retry

    def test_edit_delivery_and_author_validation(self):
        msg = make_msg("original", "aaa-member", "A", "em-1")
        self.a.broadcast(self.GRP, msg)
        self.assertTrue(wait_until(lambda: any(m.id == "em-1" for _, m in self.rec_b.messages)))
        # the author edits: B's copy follows
        self.a.broadcast_edit(self.GRP, "em-1", "edited text")
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.id == "em-1" and m.content == "edited text"
                    for _, m in self.rec_b.messages
                )
            ),
            "the author's edit must rewrite B's mesh copy",
        )
        self.assertTrue(self.rec_b.edits, "the listener must learn about the edit")
        # forged: B claims A's message (sender != link peer is impossible to
        # forge, so forge the other way: B edits ITS OWN message id it does
        # not own on A's side)
        self.b.broadcast_edit(self.GRP, "em-1", "hacked")
        time.sleep(0.3)
        stored = [m for _, m in self.rec_b.messages if m.id == "em-1"]
        self.assertTrue(all(m.content == "edited text" for m in stored))

    def test_edit_updates_own_mesh_history(self):
        msg = make_msg("before", "aaa-member", "A", "em-2")
        self.a.broadcast(self.GRP, msg)
        self.assertTrue(wait_until(lambda: self.a.update_mesh_message(
            self.GRP, "em-2", "after", "aaa-member"
        )))
        history = self.a._groups[self.GRP]["messages"]
        target = next(m for m in history if m.id == "em-2")
        self.assertEqual(target.content, "after")
        self.assertTrue(target.edited)
        # a non-author cannot rewrite through update_mesh_message either
        self.assertFalse(self.a.update_mesh_message(self.GRP, "em-2", "x", "bbb-member"))

    def test_reaction_pin_and_read_receipt_delivery(self):
        self.a.broadcast(self.GRP, make_msg("react-me", "aaa-member", "A", "rx-1"))
        self.assertTrue(wait_until(lambda: any(m.id == "rx-1" for _, m in self.rec_b.messages)))
        self.a.broadcast_reaction(self.GRP, "rx-1", "\U0001F389", True)
        self.assertTrue(
            wait_until(lambda: any(r[1] == "rx-1" for r in self.rec_b.reactions)),
            "reaction must reach the linked member",
        )
        self.a.broadcast_pin(self.GRP, "rx-1", True)
        self.assertTrue(
            wait_until(lambda: any(p[1] == "rx-1" for p in self.rec_b.pins)),
            "pin must reach the linked member",
        )
        self.b.broadcast_read_receipt(self.GRP, "rx-1")
        self.assertTrue(
            wait_until(lambda: any(r[2] == "rx-1" for r in self.rec_a.reads)),
            "group read receipt must reach the linked member",
        )


# ---------------------------------------------------------------- direct paths


class DirectRec(DirectChatListener):
    def __init__(self):
        self.edits = []
        self.reactions = []
        self.pins = []
        self.message_changes = []

    def direct_messages_changed(self, peer_id):
        self.message_changes.append(peer_id)

    def direct_message_edited(self, peer_id, message_id, new_content):
        self.edits.append((peer_id, message_id, new_content))

    def direct_reaction_changed(self, peer_id, message_id, emoji, active):
        self.reactions.append((peer_id, message_id, emoji, active))

    def direct_pin_changed(self, peer_id, message_id, active):
        self.pins.append((peer_id, message_id, active))


class DirectExtrasTest(unittest.TestCase):
    PORT = 19711

    def setUp(self):
        install_identity()
        self.server = HostGroupServer(self.PORT)
        self.server.ensure_running()
        self.rec_b = DirectRec()
        self.b = DirectChatManager()
        self.b.attach(self.rec_b)
        self.b.configure("dev-B", "B", "127.0.0.1", self.PORT)
        self.server.direct_manager = self.b
        self.rec_a = DirectRec()
        self.a = DirectChatManager()
        self.a.attach(self.rec_a)
        self.a.configure("dev-A", "A", "127.0.0.1", self.PORT)
        self.b.add_contact(self.a.my_peer())
        self.assertTrue(
            self.a.start_chat(Peer("dev-B", "B", "127.0.0.1", self.PORT))
        )
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive("dev-B")))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server.shutdown()

    def _send_and_wait(self):
        self.assertTrue(self.a.send_message("dev-B", "to-edit"))
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "to-edit" for m in self.b.messages_for("dev-A")
                )
            )
        )
        return next(
            m for m in self.a.messages_for("dev-B") if m.content == "to-edit"
        )

    def test_edit_reaches_peer_and_updates_local(self):
        msg = self._send_and_wait()
        self.assertTrue(self.a.edit_message("dev-B", msg.id, "edited!"))
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.id == msg.id and m.content == "edited!" and m.edited
                    for m in self.b.messages_for("dev-A")
                )
            ),
            "the peer's copy must follow the edit",
        )
        mine = next(m for m in self.a.messages_for("dev-B") if m.id == msg.id)
        self.assertEqual(mine.content, "edited!")
        self.assertTrue(mine.edited)
        self.assertTrue(self.rec_b.edits)
        # only the author can rewrite it: B trying to edit A's message is a
        # no-op on A's side (B's local copy of A's message must not change
        # either)
        self.assertFalse(self.b.edit_message("dev-A", msg.id, "forged"))
        time.sleep(0.3)
        a_copy = next(m for m in self.a.messages_for("dev-B") if m.id == msg.id)
        self.assertEqual(a_copy.content, "edited!")

    def test_forged_edit_of_peer_message_is_never_persisted(self):
        """A peer claiming an edit of MY message must be rejected end to end:
        the in-memory copy stays AND the edit listener (which persists the new
        text) must not fire. Regression: the listener used to fire even when
        the author check refused the edit, so the forged text was written to
        the database and reappeared after a restart."""
        msg = self._send_and_wait()
        # control: B legitimately edits its OWN message and A observes it, so
        # the assertion below cannot pass vacuously
        self.assertTrue(self.b.send_message("dev-A", "from-b"))
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "from-b" for m in self.a.messages_for("dev-B")
                )
            )
        )
        mine_b = next(
            m for m in self.b.messages_for("dev-A") if m.content == "from-b"
        )
        self.assertTrue(self.b.edit_message("dev-A", mine_b.id, "legit-edit"))
        self.assertTrue(
            wait_until(lambda: any(e[0] == "dev-B" for e in self.rec_a.edits)),
            "a legitimate edit must reach the peer's persister",
        )
        legit_count = len(self.rec_a.edits)
        # B now forges an edit packet for A's own message. B's local author
        # check refuses to send it, so write straight on the wire.
        session = self.b._sessions["dev-A"]
        session["wire"].send_packet(
            NetworkPacket(
                type="edit_message",
                group_id="direct:dev-A",
                message_id=msg.id,
                sender_id="dev-B",
                new_content="forged-body",
            )
        )
        time.sleep(0.5)
        copy = next(m for m in self.a.messages_for("dev-B") if m.id == msg.id)
        self.assertEqual(copy.content, "to-edit")
        self.assertFalse(copy.edited)
        self.assertEqual(
            len(self.rec_a.edits),
            legit_count,
            "a refused edit must never reach the persister",
        )

    def test_noop_edit_still_raises_the_edited_marker(self):
        """Saving an edit with unchanged text must still mark the message
        edited on BOTH sides (the author went through the edit flow; Android
        shows the marker immediately -- the receiver must not lag behind)."""
        msg = self._send_and_wait()
        self.assertTrue(self.a.edit_message("dev-B", msg.id, "to-edit"))
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.id == msg.id and m.edited
                    for m in self.b.messages_for("dev-A")
                )
            ),
            "the peer's copy must raise edited even when the text is identical",
        )
        mine = next(m for m in self.a.messages_for("dev-B") if m.id == msg.id)
        self.assertTrue(mine.edited)

    def test_reaction_and_pin_reach_the_peer(self):
        msg = self._send_and_wait()
        self.assertTrue(self.a.send_direct_reaction("dev-B", msg.id, "\U0001F44D", True))
        self.assertTrue(
            wait_until(lambda: any(r[1] == msg.id for r in self.rec_b.reactions))
        )
        self.assertTrue(self.b.send_direct_reaction("dev-A", msg.id, "\u2764", False))
        self.assertTrue(
            wait_until(lambda: any(r[2] == "\u2764" for r in self.rec_a.reactions))
        )
        self.assertTrue(self.a.send_direct_pin("dev-B", msg.id, True))
        self.assertTrue(
            wait_until(lambda: any(p[1] == msg.id for p in self.rec_b.pins))
        )
        # unpin rides the same path
        self.assertTrue(self.a.send_direct_pin("dev-B", msg.id, False))
        self.assertTrue(
            wait_until(
                lambda: any(p[1] == msg.id and p[2] is False for p in self.rec_b.pins)
            )
        )


# --------------------------------------------------------------- voice helpers


class VoiceHelpersTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def _write_wav(self, name, seconds, sample_rate=16000):
        import wave

        path = os.path.join(self.dir.name, name)
        frames = int(seconds * sample_rate)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(b"\x00\x00" * frames)
        return path

    def test_duration_rounds_half_up_like_android(self):
        from localchat.audio_note import wav_duration_seconds

        # Android uses Math.round (half-up): 0.5s -> 1s, 2.5s -> 3s
        self.assertEqual(wav_duration_seconds(self._write_wav("half.wav", 0.5)), 1)
        self.assertEqual(wav_duration_seconds(self._write_wav("two-half.wav", 2.5)), 3)
        self.assertEqual(wav_duration_seconds(self._write_wav("short.wav", 0.4)), 0)
        self.assertEqual(wav_duration_seconds(self._write_wav("seven.wav", 7.2)), 7)
        self.assertEqual(wav_duration_seconds(os.path.join(self.dir.name, "no.wav")), 0)

    def test_prune_removes_only_stale_recordings(self):
        from localchat.audio_note import prune_recordings

        stale = self._write_wav("voice_old.wav", 1)
        fresh = self._write_wav("voice_new.wav", 1)
        os.utime(stale, (time.time() - 7200, time.time() - 7200))
        prune_recordings(self.dir.name, max_age_seconds=3600)
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh), "a fresh recording must be kept")

    def test_prune_ignores_missing_directory(self):
        from localchat.audio_note import prune_recordings

        prune_recordings(os.path.join(self.dir.name, "nope"), max_age_seconds=3600)

    def test_cancel_is_safe_when_idle(self):
        """cancel() is called from navigation/teardown even when nothing is
        recording: it must be a harmless no-op (no sounddevice in tests)."""
        from localchat.audio_note import VoiceRecorder

        rec = VoiceRecorder(self.dir.name)
        rec.cancel()
        self.assertFalse(rec.recording)
        self.assertEqual(rec.stop(), "")
        self.assertEqual(rec.elapsed_seconds(), 0)

    def test_playback_callback_decodes_pcm_bytes(self):
        """Regression: VoicePlayer's callback assigned raw WAV bytes straight
        into the numpy int16 outdata buffer, so every PortAudio callback died
        with "invalid literal for int() with base 10" and inline voice
        playback never produced sound. Frames must be decoded via frombuffer
        (same pattern as call.py) and the tail padded with zeros."""
        import wave as _wave

        import numpy as np

        from localchat import audio_note

        if audio_note._np is None:  # pragma: no cover - optional dependency
            self.skipTest("numpy unavailable")

        class CallbackStop(Exception):
            pass

        captured = {}

        class FakeStream:
            active = True

            def __init__(self, **kwargs):
                captured["callback"] = kwargs["callback"]

            def start(self):
                pass

            def stop(self):
                pass

            def close(self):
                pass

        class FakeSd:
            CallbackStop = CallbackStop
            OutputStream = FakeStream

        # three non-trivial little-endian int16 samples
        pcm = bytes((0x34, 0x12, 0x78, 0x56, 0x00, 0xFF))
        path = os.path.join(self.dir.name, "play.wav")
        with _wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)

        orig_sd = audio_note._sd
        audio_note._sd = FakeSd()
        try:
            player = audio_note.VoicePlayer()
            self.assertTrue(player.play(path))
        finally:
            audio_note._sd = orig_sd

        try:
            callback = captured["callback"]
            expected = np.frombuffer(pcm, dtype=np.int16)

            # short read: pad the tail and stop the stream
            out = np.zeros(8, dtype=np.int16)
            with self.assertRaises(CallbackStop):
                callback(out.reshape(-1, 1), 8, None, None)
            np.testing.assert_array_equal(out[:3], expected)
            self.assertTrue((out[3:] == 0).all())

            # exact read: buffer full, no CallbackStop
            out_full = np.zeros(3, dtype=np.int16)
            callback(out_full.reshape(-1, 1), 3, None, None)
            np.testing.assert_array_equal(out_full, expected)
        finally:
            player.stop()


if __name__ == "__main__":
    unittest.main()
