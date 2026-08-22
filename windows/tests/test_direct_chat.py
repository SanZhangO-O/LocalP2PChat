"""Direct member chat tests: pull up a 1:1 chat with no confirmation, message
delivery both ways, delete propagation, contacts learned from the handshake
(including added-by-IP placeholder resolution), file transfer, call signaling
over the session, sender validation and bounded reads (Android parity).

Since the protocol upgrade EVERY direct session starts with the secured
identity handshake (securewire.Handshake: ECDH signed by long-term identity
keys) and every line after it is AES-256-GCM encrypted — there is no
plaintext path. So each test installs a long-term identity
(DeviceIdentity.current) before any dial: DeviceIdentity is process-wide, so
both sides here (A dials, B auto-accepts behind the shared HostGroupServer)
share one key, which is all the basic sender/recipient flows need.

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import localchat.network as network_module
from localchat.models import (
    CallInfo,
    FileInfo,
    MAX_CONTENT_LENGTH,
    MAX_LINE_LENGTH,
    NetworkPacket,
)
from localchat.network import DirectChatListener, DirectChatManager, Peer
from tests.fake_peer import install_identity


class Rec(DirectChatListener):
    def __init__(self):
        self.contact_changes = 0
        self.message_changes = []

    def direct_contacts_changed(self):
        self.contact_changes += 1

    def direct_messages_changed(self, peer_id: str):
        self.message_changes.append(peer_id)


def wait_until(cond, timeout=8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class DirectChatTest(unittest.TestCase):
    PORT = 19411

    def setUp(self):
        # every direct-session handshake signs with DeviceIdentity.current;
        # the secured protocol refuses to dial without one installed
        install_identity()
        self.server = network_module.HostGroupServer(self.PORT)
        self.server.ensure_running()
        self.rec_b = Rec()
        self.b = DirectChatManager()
        self.b.attach(self.rec_b)
        self.b.configure("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)  # 小B
        self.server.direct_manager = self.b
        self.rec_a = Rec()
        self.a = DirectChatManager()
        self.a.attach(self.rec_a)
        self.a.configure("dev-A", "\u5c0fA", "127.0.0.1", self.PORT)  # 小A
        # The receiving side (B) already knows A BY ITS ADVERTISED identity
        # (a saved contact / previous acceptance): a FIRST contact would be
        # parked in B's request box instead of auto-accepted, and these tests
        # exercise the established-session flows, not the request box.
        self.b.add_contact(self.a.my_peer())

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server.shutdown()

    def test_pull_up_chat_no_confirmation(self):
        """Device A pulls up a chat with device B; B auto-accepts over the
        secured identity handshake -- no confirmation needed."""
        established = []
        self.a.on_session_established = established.append
        real_id = self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT))
        self.assertEqual(real_id, "dev-B", "start_chat must return the peer's real id")
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive("dev-B")), "A alive")
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")), "B alive")
        self.assertTrue(
            wait_until(lambda: "dev-B" in established),
            "on_session_established must fire with the real peer id",
        )

        # contacts are learned on both sides from the handshake
        self.assertTrue(
            wait_until(lambda: any(c.id == "dev-A" for c in self.b.contacts_list())),
            "B learned A as a contact",
        )
        self.assertTrue(any(c.id == "dev-B" for c in self.a.contacts_list()))

    def test_messages_deliver_both_ways(self):
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))

        self.assertTrue(self.a.send_message("dev-B", "\u4f60\u597d\u5c0fB"))  # 你好小B
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u4f60\u597d\u5c0fB" and m.sender_id == "dev-A"
                    for m in self.b.messages_for("dev-A")
                )
            ),
            "A -> B",
        )
        self.assertTrue(self.b.send_message("dev-A", "\u4f60\u597d\u5c0fA"))  # 你好小A
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u4f60\u597d\u5c0fA" and m.sender_id == "dev-B"
                    for m in self.a.messages_for("dev-B")
                )
            ),
            "B -> A",
        )

    def test_delete_propagates_to_other_side(self):
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))
        self.assertTrue(self.a.send_message("dev-B", "\u8981\u5220\u9664\u7684\u6d88\u606f"))  # 要删除的消息
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u8981\u5220\u9664\u7684\u6d88\u606f"
                    for m in self.b.messages_for("dev-A")
                )
            )
        )
        target = next(
            m for m in self.b.messages_for("dev-A") if m.content == "\u8981\u5220\u9664\u7684\u6d88\u606f"
        )
        self.a.delete_message("dev-B", target.id, target.sender_id)
        self.assertTrue(
            wait_until(
                lambda: all(m.id != target.id for m in self.b.messages_for("dev-A"))
            ),
            "delete must reach the other side",
        )

    def test_added_by_ip_resolves_real_id(self):
        """A contact added by IP (placeholder 'ip:...' id) must resolve to the
        member's real device id after the handshake: start_chat returns the
        real id, the session keys by it, the placeholder contact is replaced
        (dedupe by endpoint), and any queued state migrates via
        on_chat_migrated (was: everything stayed on the placeholder and the
        chat appeared disconnected)."""
        b_ip = self.b.my_peer().ip_address
        placeholder = Peer(f"ip:{b_ip}:{self.PORT}", "\u5c0fB", b_ip, self.PORT)
        # register the endpoint the placeholder key refers to BEFORE dialing
        # so the handshake can migrate the alias to the real device id
        self.a.open_chat(placeholder)
        established = []
        migrations = []
        self.a.on_session_established = established.append
        self.a.on_chat_migrated = lambda f, t: migrations.append((f, t))

        peer_id = self.a.start_chat(placeholder)
        self.assertEqual(peer_id, "dev-B", "handshake must reveal the real id")
        self.assertTrue(wait_until(lambda: self.a.is_chat_alive("dev-B")), "session under real id")
        self.assertTrue(
            wait_until(lambda: "dev-B" in established),
            "session established under the real id",
        )
        self.assertTrue(
            wait_until(
                lambda: any(
                    f == placeholder.id and t == "dev-B" for f, t in migrations
                )
            ),
            "on_chat_migrated must move the placeholder alias to the real id",
        )
        # the placeholder contact is replaced by the real one (dedupe by endpoint)
        self.assertTrue(
            wait_until(lambda: "dev-B" in [c.id for c in self.a.contacts_list()]),
            f"contacts should contain the real id: {self.a.contacts_list()}",
        )
        self.assertTrue(
            all(not c.id.startswith("ip:") for c in self.a.contacts_list()),
            "placeholder contact must be replaced",
        )
        # messages flow under the real id
        self.assertTrue(self.a.send_message("dev-B", "\u6309IP\u52a0\u7684\u4e5f\u80fd\u804a"))  # 按IP加的也能聊
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u6309IP\u52a0\u7684\u4e5f\u80fd\u804a"
                    for m in self.b.messages_for("dev-A")
                )
            ),
            "message must reach the peer",
        )

    def test_unreachable_member_fails_cleanly(self):
        ok = self.a.start_chat(Peer("dev-X", "\u4e0d\u5b58\u5728", "127.0.0.1", 1))  # 不存在
        self.assertFalse(ok)
        self.assertFalse(self.a.is_chat_alive("dev-X"))
        # nothing was added as a session
        self.assertTrue(wait_until(lambda: not self.a.is_chat_alive("dev-X")))

    def test_file_transfer_over_direct_chat(self):
        """A offers a file over the direct session; B sees the file_message and
        downloads the bytes from A's short-lived download server (the offer
        carries the per-file key that protects the raw download stream)."""
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))

        payload = b"direct-file-bytes-" * 1000
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(payload)
            path = f.name
        target = None
        try:
            sent = self.a.send_file("dev-B", path)
            self.assertIsNotNone(sent, "send_file must succeed on a live session")
            info = sent.file_info
            self.assertTrue(
                wait_until(
                    lambda: any(
                        m.id == sent.id
                        and m.file_info is not None
                        and m.content == os.path.basename(path)
                        for m in self.b.messages_for("dev-A")
                    )
                ),
                "B must receive the file_message with FileInfo",
            )
            # the offer's download host is A's LAN IP; connect over loopback
            # (the server binds 0.0.0.0) so the test is deterministic. The
            # per-file key must ride along or the download cannot open the
            # encrypted stream.
            offer = FileInfo(
                info.file_id,
                info.file_name,
                info.file_size,
                "127.0.0.1",
                info.download_port,
                info.file_key,
            )
            target = os.path.join(tempfile.gettempdir(), "direct_download.bin")
            ok, message = self.b.download_file(offer, target)
            self.assertTrue(ok, message)
            with open(target, "rb") as f:
                self.assertEqual(f.read(), payload, "downloaded bytes must match")
        finally:
            if path and os.path.exists(path):
                os.remove(path)
            if target and os.path.exists(target):
                os.remove(target)
            if target and os.path.exists(target + ".part"):
                os.remove(target + ".part")

    def test_call_signaling_rides_direct_session(self):
        """Call packets sent over the session are forwarded to on_call_signal
        on the peer; packets that do not involve the peer are dropped."""
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))
        received = []
        self.b.on_call_signal = received.append

        offer = NetworkPacket(
            type="call_offer",
            call=CallInfo(
                call_id="call-1",
                caller_id="dev-A",
                caller_name="\u5c0fA",
                callee_id="dev-B",
                media_port=12345,
            ),
        )
        self.assertTrue(self.a.send_packet("dev-B", offer))
        self.assertTrue(
            wait_until(lambda: received and received[0].call.call_id == "call-1"),
            "call_offer must ride the direct session",
        )

        # a call between two OTHER devices is not ours: dropped locally
        foreign = NetworkPacket(
            type="call_offer",
            call=CallInfo(
                call_id="call-2",
                caller_id="dev-X",
                caller_name="\u8def\u4eba",  # 路人
                callee_id="dev-Y",
                media_port=23456,
            ),
        )
        received.clear()
        self.assertTrue(self.a.send_packet("dev-B", foreign))
        time.sleep(0.3)
        self.assertEqual(received, [], "foreign call signaling must be dropped")

    def test_forged_sender_dropped_on_direct_session(self):
        """Only the linked member may speak as itself: a chat carrying a
        different senderId (or invalid content) is dropped, matching the host
        relay and the Android direct reader."""
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))

        forged = NetworkPacket(
            type="chat",
            message=network_module.ChatMessage(
                id="forged-1",
                content="\u6211\u662f\u5c0fA",  # 我是小A
                timestamp=int(time.time() * 1000),
                sender_id="dev-EVIL",
                sender_name="\u5192\u5145\u8005",  # 冒充者
            ),
        )
        self.assertTrue(self.a.send_packet("dev-B", forged))
        time.sleep(0.3)
        self.assertFalse(
            any(m.id == "forged-1" for m in self.b.messages_for("dev-A")),
            "forged senderId must be dropped",
        )

        # the real sender with a legit message still arrives
        self.assertTrue(self.a.send_message("dev-B", "\u6b63\u5e38\u6d88\u606f"))  # 正常消息
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u6b63\u5e38\u6d88\u606f"
                    for m in self.b.messages_for("dev-A")
                )
            )
        )

    def test_seed_messages_merges_live_history(self):
        """Seeding persisted history must MERGE into messages already received
        live (a session may deliver messages before the chat UI opened) -- the
        overwrite behavior wiped them (Android parity)."""
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))
        self.assertTrue(self.a.send_message("dev-B", "\u5148\u5230\u7684\u6d88\u606f"))  # 先到的消息
        self.assertTrue(
            wait_until(
                lambda: any(
                    m.content == "\u5148\u5230\u7684\u6d88\u606f"
                    for m in self.b.messages_for("dev-A")
                )
            )
        )
        old = network_module.ChatMessage(
            id="old-1",
            content="\u6628\u5929\u7684\u5386\u53f2",  # 昨天的历史
            timestamp=int(time.time() * 1000) - 60_000,
            sender_id="dev-A",
            sender_name="\u5c0fA",
            is_from_me=False,
        )
        self.b.seed_messages("dev-A", [old])
        msgs = self.b.messages_for("dev-A")
        self.assertIn("old-1", [m.id for m in msgs], "history must be seeded")
        self.assertIn(
            "\u5148\u5230\u7684\u6d88\u606f", [m.content for m in msgs],
            "live message must NOT be wiped by seeding",
        )
        timestamps = [m.timestamp for m in msgs]
        self.assertEqual(timestamps, sorted(timestamps), "merged list stays time-ordered")

    def test_overlong_direct_message_rejected(self):
        """Content above MAX_CONTENT_LENGTH is rejected locally (the Android
        reader would silently drop it) instead of showing as sent."""
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))
        self.assertFalse(
            self.a.send_message("dev-B", "x" * (MAX_CONTENT_LENGTH + 1)),
            "over-long content must be rejected",
        )

    def test_oversized_line_drops_session(self):
        """A single frame longer than MAX_LINE_LENGTH closes the link instead
        of buffering unboundedly (Android parity: bounded readLineLimited)."""
        self.assertTrue(self.a.start_chat(Peer("dev-B", "\u5c0fB", "127.0.0.1", self.PORT)))
        self.assertTrue(wait_until(lambda: self.b.is_chat_alive("dev-A")))
        sock = self.a._sessions["dev-B"]["sock"]
        try:
            sock.sendall(b"x" * (MAX_LINE_LENGTH + 100) + b"\n")
        except OSError:
            pass
        self.assertTrue(
            wait_until(lambda: not self.b.is_chat_alive("dev-A")),
            "the peer must drop an oversized line",
        )


    def test_failed_write_restores_undelivered_messages(self):
        """A send that fails AFTER the outbox flush must park the message back
        as pending (at-least-once delivery; the receiver dedups by id):
        swallowing it would show a message as delivered that never left this
        machine."""
        import queue as queue_mod

        class BoomWire:
            def send_packet(self, packet):
                raise OSError("connection reset")

        msg = network_module.ChatMessage(
            id="pending-1",
            content="\u672a\u9001\u8fbe",  # 未送达
            timestamp=int(time.time() * 1000),
            sender_id="dev-A",
            sender_name="\u5c0fA",
            is_from_me=True,
            pending=True,
        )
        self.a._outbox["dev-B"] = [msg]
        session = {
            "peer_id": "dev-B",
            "peer_name": "\u5c0fB",
            "sock": None,
            "wire": BoomWire(),
            "alive": True,
            "send_queue": queue_mod.Queue(),
        }
        self.a._flush_outbox("dev-B", session)
        self.assertEqual(self.a._outbox.get("dev-B"), [], "flush must drain the outbox")
        # run the sender loop inline: the write fails, the message must return
        self.a._send_loop(session)
        restored = self.a._outbox.get("dev-B")
        self.assertEqual(
            [m.id for m in restored],
            ["pending-1"],
            "the undelivered message must return to the outbox",
        )
        self.assertTrue(restored and restored[0].pending, "it must be pending again")


if __name__ == "__main__":
    unittest.main()
