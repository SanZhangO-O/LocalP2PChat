"""Wire-protocol conformance tests against the Android (Kotlin) LocalChat.

The Android side serializes with kotlinx.serialization defaults:
- compact JSON, UTF-8, newline-delimited frames on TCP port 9999
- properties equal to their default values (all null optionals) are omitted
- ChatMessage.isFromMe / pending are @Transient and never serialized
- unknown keys in incoming JSON are ignored (Json { ignoreUnknownKeys = true })

Since the ECDH handshake change, EVERY connection starts with the secured
handshake (securewire.Handshake) and every subsequent line is AES-256-GCM
encrypted under the negotiated session key; the plaintext password field is
gone from packets. These tests drive the Python network stack through the
real handshake and assert the protocol behaves byte-exactly like Android.

Chinese literals are written as unicode escapes (\\uXXXX) so the file stays
pure-ASCII on disk but produces the correct text at runtime.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import localchat.network as network_module
from localchat.models import (
    MAX_CONTENT_LENGTH,
    MAX_LINE_LENGTH,
    CallInfo,
    ChatMessage,
    FileInfo,
    GroupInfo,
    NetworkPacket,
    Peer,
    sanitize_file_name,
)
from localchat.network import P2PListener, P2PManager
from localchat.securewire import Handshake
from tests.fake_peer import FakePeerClient, FakeHostServer, install_identity, wait_until


GROUP_NAME = "\u6d4b\u8bd5\u7fa4"  # 测试群
GROUP_PASSWORD = "123456"
HOST_NAME = "Android\u4e3b\u673a"  # Android主机
HOST_PEER = {"id": "android-host-1", "name": HOST_NAME, "ipAddress": "192.168.1.10", "port": 9999}
MEMBER_PEER = Peer("android-member-1", "\u5f20\u4e09", "192.168.1.5", 9999)  # 张三
OTHER_PEER = Peer("android-member-2", "\u674e\u56db", "192.168.1.7", 9999)  # 李四


class Recorder(P2PListener):
    def __init__(self):
        self.peer_events = []
        self.msg_events = []

    def peers_changed(self, p2p):
        self.peer_events.append(dict(p2p.peers))

    def messages_changed(self, p2p):
        self.msg_events.append(list(p2p.messages))


class ProtocolTestBase(unittest.TestCase):
    PORT = 19191

    def start_host(self, password=GROUP_PASSWORD):
        host = P2PManager(Recorder(), port=self.PORT, password=password)
        host.initialize_as_host(HOST_NAME, GROUP_NAME)
        host.set_join_id(host.numeric_group_id)
        host.start_as_host()
        return host

    def join_member(self, peer=MEMBER_PEER, password=GROUP_PASSWORD):
        c = FakePeerClient.connect("127.0.0.1", self.PORT)
        c.hs_join(GROUP_NAME, password)
        c.send(NetworkPacket(type="join", group_id=GROUP_NAME, peer=peer))
        ack = c.recv()
        assert ack is not None and ack.type == "join_ack", "join must be acked"
        return c


class AndroidClientToPythonHost(ProtocolTestBase):
    def test_query_group_parses_and_responds(self):
        host = self.start_host()
        try:
            s = FakePeerClient.connect("127.0.0.1", self.PORT)
            s.hs_query(GROUP_NAME, GROUP_PASSWORD)
            s.send(NetworkPacket(type="query", group_id=GROUP_NAME))
            resp = s.recv()
            self.assertIsNotNone(resp)
            self.assertEqual(resp.type, "group_info")
            self.assertEqual(resp.group_info.group_name, GROUP_NAME)
            self.assertEqual(resp.group_info.creator_name, HOST_NAME)
            self.assertEqual(resp.group_info.member_count, 1)
            s.close()
        finally:
            host.stop()

    def test_query_mismatch_rejected(self):
        host = self.start_host()
        try:
            s = FakePeerClient.connect("127.0.0.1", self.PORT)
            other = "\u5176\u4ed6\u7fa4"  # 其他群
            s.hs_query(other, GROUP_PASSWORD)
            s.send(NetworkPacket(type="query", group_id=other))
            self.assertEqual(s.recv().type, "join_rejected")
            s.close()
        finally:
            host.stop()

    def test_join_ack_and_members_shape(self):
        host = self.start_host()
        try:
            s = FakePeerClient.connect("127.0.0.1", self.PORT)
            s.hs_join(GROUP_NAME, GROUP_PASSWORD)
            s.send(NetworkPacket(type="join", group_id=GROUP_NAME, peer=MEMBER_PEER))
            ack = s.recv()
            self.assertEqual(ack.type, "join_ack")
            self.assertEqual(ack.group_id, host.group_id)
            members = ack.members
            self.assertEqual(len(members), 1)
            self.assertEqual(set(members[0].to_dict().keys()), {"id", "name", "ipAddress", "port"})
            self.assertEqual(members[0].name, HOST_NAME)
            self.assertEqual(members[0].port, self.PORT)
            s.close()
        finally:
            host.stop()

    def test_join_wrong_password_rejected(self):
        host = self.start_host()
        try:
            s = FakePeerClient.connect("127.0.0.1", self.PORT)
            with self.assertRaises(Exception):
                s.hs_join(GROUP_NAME, password="wrong")
            s.close()
        finally:
            host.stop()

    def test_chat_and_broadcast_roundtrip_without_is_from_me(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()  # announce when m2 joined

            hello = "\u4f60\u597d\uff0c\u6765\u81ea\u5b89\u5353"  # 你好，来自安卓
            m1.send(NetworkPacket(
                type="chat",
                message=ChatMessage("m-100", hello, 1700000000000, "android-member-1", "\u5f20\u4e09"),
            ))
            packet = m2.recv()
            self.assertEqual(packet.type, "chat")
            msg = packet.message
            self.assertNotIn("isFromMe", msg.to_dict(), "Android cannot parse isFromMe field")
            self.assertNotIn("pending", msg.to_dict(), "pending is @Transient")
            self.assertEqual(msg.content, hello)
            self.assertEqual(msg.sender_name, "\u5f20\u4e09")
            self.assertEqual(msg.timestamp, 1700000000000)
            self.assertTrue(wait_until(lambda: any(m.content == hello for m in host.messages)))

            m1.send(NetworkPacket(type="delete_message", message_id="m-100", sender_id="android-member-1"))
            broadcast = m2.recv()
            self.assertEqual(
                broadcast.to_dict(),
                NetworkPacket(type="delete_message", message_id="m-100", sender_id="android-member-1").to_dict(),
            )
            self.assertTrue(wait_until(lambda: all(m.id != "m-100" for m in host.messages)))
            self.assertIsNone(
                m1.recv(skip_heartbeat=True, timeout=0.5),
                "the deleting sender must not receive its own delete echo",
            )
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_unknown_keys_and_is_from_me_ignored(self):
        host = self.start_host()
        try:
            m = self.join_member()
            payload = NetworkPacket(type="chat", message=ChatMessage(
                "m-101", "x", 1, "android-member-1", "n",
            )).to_json()
            payload = payload.replace('"id":"m-101"', '"id":"m-101","extra":1')
            m.send_json(payload)
            self.assertTrue(wait_until(lambda: any(x.id == "m-101" for x in host.messages)))
            msg = next(x for x in host.messages if x.id == "m-101")
            self.assertFalse(msg.is_from_me)
            m.close()
        finally:
            host.stop()

    def test_peer_left_with_empty_fields(self):
        host = self.start_host()
        try:
            m = self.join_member()
            m.close()
            self.assertTrue(wait_until(lambda: not host.peers))
        finally:
            host.stop()

    def test_host_broadcasts_peer_left_packet(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.close()
            packet = m2.recv()
            self.assertEqual(packet.type, "peer_left")
            self.assertEqual(set(packet.peer.to_dict().keys()), {"id", "name", "ipAddress", "port"})
            self.assertEqual(packet.peer.port, 0)
            self.assertTrue(wait_until(lambda: len(host.peers) == 1))
            m2.close()
        finally:
            host.stop()

    def test_chat_wrong_sender_id_dropped(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.send(NetworkPacket(type="chat", message=ChatMessage(
                "m-fake", "\u4f2a\u88c5", 1, "android-member-2", "\u9ed1\u5ba2",
            )))
            self.assertIsNone(m2.recv(skip_heartbeat=True, timeout=0.4))
            time.sleep(0.3)
            self.assertFalse(any(x.id == "m-fake" for x in host.messages))
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_chat_empty_content_dropped(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.send(NetworkPacket(type="chat", message=ChatMessage("m-empty", "  ", 1, "android-member-1", "\u5f20\u4e09")))
            self.assertIsNone(m2.recv(skip_heartbeat=True, timeout=0.4))
            time.sleep(0.3)
            self.assertFalse(any(x.id == "m-empty" for x in host.messages))
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_chat_overlong_content_dropped(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.send(NetworkPacket(type="chat", message=ChatMessage(
                "m-long", "x" * (MAX_CONTENT_LENGTH + 1), 1, "android-member-1", "\u5f20\u4e09",
            )))
            self.assertIsNone(m2.recv(skip_heartbeat=True, timeout=0.4))
            time.sleep(0.3)
            self.assertFalse(any(x.id == "m-long" for x in host.messages))
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_delete_wrong_connection_identity_not_broadcast(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.send(NetworkPacket(type="chat", message=ChatMessage("m-100", "hi", 1, "android-member-1", "\u5f20\u4e09")))
            self.assertIsNotNone(m2.recv())
            m1.send(NetworkPacket(type="delete_message", message_id="m-100", sender_id="android-member-2"))
            self.assertIsNone(m2.recv(skip_heartbeat=True, timeout=0.4))
            time.sleep(0.3)
            self.assertTrue(any(x.id == "m-100" for x in host.messages))
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_delete_wrong_owner_not_broadcast(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            other_chat = "\u674e\u56db\u7684\u6d88\u606f"  # 李四的消息
            m2.send(NetworkPacket(type="chat", message=ChatMessage("m-200", other_chat, 1, "android-member-2", "\u674e\u56db")))
            self.assertIsNotNone(m1.recv())
            m1.send(NetworkPacket(type="delete_message", message_id="m-200", sender_id="android-member-1"))
            self.assertIsNone(m2.recv(skip_heartbeat=True, timeout=0.4))
            time.sleep(0.3)
            self.assertTrue(any(x.id == "m-200" for x in host.messages))
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_delete_unknown_message_not_broadcast(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.send(NetworkPacket(type="delete_message", message_id="m-ghost", sender_id="android-member-1"))
            self.assertIsNone(m2.recv(skip_heartbeat=True, timeout=0.4))
            m1.close()
            m2.close()
        finally:
            host.stop()

    def test_plaintext_packet_rejected_no_downgrade(self):
        host = self.start_host()
        try:
            s = socket.create_connection(("127.0.0.1", self.PORT), timeout=6)
            s.sendall(
                json.dumps({"type": "join", "groupId": GROUP_NAME,
                            "peer": {"id": "x", "name": "n", "ipAddress": "1.1.1.1", "port": 1}}).encode()
                + b"\n"
            )
            s.settimeout(2)
            buf = bytearray()
            try:
                while True:
                    chunk = s.recv(1)
                    if not chunk:
                        break
                    buf.extend(chunk)
            except socket.timeout:
                pass
            s.close()
            self.assertEqual(bytes(buf), b"", "legacy plaintext must be rejected without a response")
        finally:
            host.stop()

    def test_overlong_first_line_closes_connection(self):
        host = self.start_host()
        try:
            s = socket.create_connection(("127.0.0.1", self.PORT), timeout=6)
            s.sendall(b"a" * (MAX_LINE_LENGTH + 100))
            s.settimeout(2)
            self.assertEqual(
                s.makefile("r", encoding="utf-8", newline="\n").readline(),
                "",
                "overlong first line must close the connection (EOF)",
            )
            s.close()
        finally:
            host.stop()

    def test_peer_left_on_disconnect(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()
            m1.close()
            packet = m2.recv()
            self.assertEqual(packet.type, "peer_left")
            self.assertEqual(packet.peer.id, "android-member-1")
            self.assertTrue(wait_until(lambda: len(host.peers) == 1))
            m2.close()
        finally:
            host.stop()


class PythonClientToAndroidHost(ProtocolTestBase):
    PORT = 19192

    def test_client_output_is_kotlinx_compatible(self):
        install_identity()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(8)
        srv.settimeout(6)
        port = srv.getsockname()[1]

        # the serve thread only COLLECTS what arrives; every assertion runs in
        # the main thread (an assert inside a daemon thread never fails the
        # test). Raw decrypted JSON is kept so wire-level guarantees — like
        # "the password never appears in a packet" — are checked against the
        # actual bytes, not a re-serialized Python object.
        received = []

        def serve():
            try:
                from localchat.network import _read_line_bounded, make_wire

                conn, _ = srv.accept()
                conn.settimeout(6)
                reader = conn.makefile("r", encoding="utf-8", newline="\n")
                first = _read_line_bounded(reader)
                start = NetworkPacket.from_json(first)
                wire = make_wire(conn, reader)
                Handshake.accept(wire, start, lambda m, g: GROUP_PASSWORD)
                query_raw = wire.recv_packet_text()
                received.append(("query_raw", query_raw))
                received.append(("query", NetworkPacket.from_json(query_raw)))
                wire.send_packet(NetworkPacket(
                    type="group_info", group_info=GroupInfo(GROUP_NAME, HOST_NAME, "c", 1),
                ))
                conn.close()

                conn, _ = srv.accept()
                conn.settimeout(6)
                reader = conn.makefile("r", encoding="utf-8", newline="\n")
                first = _read_line_bounded(reader)
                start = NetworkPacket.from_json(first)
                wire = make_wire(conn, reader)
                Handshake.accept(wire, start, lambda m, g: GROUP_PASSWORD)
                join_raw = wire.recv_packet_text()
                received.append(("join_raw", join_raw))
                received.append(("join", NetworkPacket.from_json(join_raw)))
                wire.send_packet(NetworkPacket(
                    type="join_ack",
                    group_id="%s@androidid" % GROUP_NAME,
                    members=[Peer.from_dict(HOST_PEER)],
                ))

                chat_raw = wire.recv_packet_text()
                received.append(("chat_raw", chat_raw))
                conn.close()
            except Exception as e:  # surfaced by the main-thread asserts below
                received.append(("error", repr(e)))
            finally:
                srv.close()

        t = threading.Thread(target=serve, daemon=True)
        t.start()

        client = P2PManager(Recorder(), port=19999)
        client.initialize_as_client("\u738b\u4e94", GROUP_NAME, password=GROUP_PASSWORD)
        client.query_group("127.0.0.1", port)
        self.assertTrue(wait_until(lambda: client.queried_group_info is not None))
        client.confirm_join("127.0.0.1", port)
        self.assertTrue(wait_until(lambda: client.connection_result is not None))
        ok, _ = client.connection_result
        self.assertTrue(ok)
        self.assertEqual(client.group_id, "%s@androidid" % GROUP_NAME)
        greeting = "\u6765\u81eaWindows\u7684\u95ee\u5019"  # 来自Windows的问候
        client.send_message(greeting)
        self.assertTrue(wait_until(lambda: any(m.content == greeting for m in client.messages)))
        t.join(timeout=6)
        client.stop()

        # ---- everything the Android-style peer saw, asserted HERE ----
        tags = [tag for tag, _ in received]
        self.assertNotIn(
            "error", tags, f"serve thread failed: {[v for t_, v in received if t_ == 'error']}"
        )
        for required in ("query_raw", "join_raw", "chat_raw"):
            self.assertIn(required, tags, f"server never received the {required} packet")

        query = next(v for t_, v in received if t_ == "query")
        self.assertEqual(query.type, "query")
        self.assertEqual(query.group_id, GROUP_NAME)

        join_raw = next(v for t_, v in received if t_ == "join_raw")
        self.assertNotIn(
            '"password"', join_raw, "the password must never appear in a packet"
        )
        join = next(v for t_, v in received if t_ == "join")
        self.assertEqual(join.type, "join")
        self.assertEqual(join.group_id, GROUP_NAME)
        self.assertEqual(set(join.peer.to_dict().keys()), {"id", "name", "ipAddress", "port"})
        self.assertEqual(join.peer.port, 19999)

        chat_raw = next(v for t_, v in received if t_ == "chat_raw")
        chat = json.loads(chat_raw)
        self.assertEqual(chat["type"], "chat")
        msg = chat["message"]
        self.assertEqual(set(msg.keys()), {"id", "content", "timestamp", "senderId", "senderName"})
        self.assertNotIn("isFromMe", msg)
        self.assertNotIn("pending", msg)
        self.assertEqual(msg["senderName"], "\u738b\u4e94")  # 王五


class PythonOutputFormatTest(unittest.TestCase):
    """Byte-exact output parity with kotlinx.serialization on the Android side."""

    def test_python_json_is_compact_kotlinx_style(self):
        self.assertEqual(
            NetworkPacket(type="chat", group_id="g").to_json(),
            '{"type":"chat","groupId":"g"}',
        )
        hello = "\u4f60\u597d"  # 你好
        chat = NetworkPacket(type="chat", message=ChatMessage("m1", hello, 1700000000000, "a", "Alice"))
        self.assertNotIn(" ", chat.to_json(), "kotlinx emits no whitespace between tokens")
        self.assertNotIn("isFromMe", chat.to_json(), "isFromMe is @Transient on Android")
        self.assertNotIn("pending", chat.to_json(), "pending is @Transient on Android")
        self.assertEqual(json.loads(chat.to_json())["message"]["content"], hello)

    def test_python_decodes_kotlinx_compact_bytes(self):
        packet = NetworkPacket.from_json('{"type":"delete_message","messageId":"m-1"}')
        self.assertEqual(packet.type, "delete_message")
        self.assertEqual(packet.message_id, "m-1")

    def test_handshake_fields_roundtrip(self):
        pkt = NetworkPacket.from_json(
            '{"type":"hs_start","hsMode":"query","groupId":"g","eph":"AQID"}'
        )
        self.assertEqual(pkt.type, "hs_start")
        self.assertEqual(pkt.hs_mode, "query")
        self.assertEqual(pkt.group_id, "g")
        self.assertEqual(pkt.eph, "AQID")
        back = NetworkPacket.from_json(pkt.to_json())
        self.assertEqual(back.type, "hs_start")
        self.assertEqual(back.hs_mode, "query")
        self.assertEqual(back.group_id, "g")
        self.assertEqual(back.eph, "AQID")


class FileTransferTest(ProtocolTestBase):
    PORT = 19194

    def test_file_message_serialization_kotlinx_compatible(self):
        from localchat import crypto as crypto_mod

        key = crypto_mod.to_b64(crypto_mod.random_bytes(32))
        fi = FileInfo("f1", "\u62a5\u544a.pdf", 12345, "192.168.1.5", 42001, key)  # 报告.pdf
        msg = ChatMessage("f1", "\u62a5\u544a.pdf", 1700000000000, "a", "\u5f20\u4e09", file_info=fi)
        pkt = NetworkPacket(type="file_message", message=msg)
        wire = pkt.to_json()
        self.assertNotIn(" ", wire, "kotlinx emits no whitespace between tokens")
        self.assertNotIn("isFromMe", wire)
        parsed = NetworkPacket.from_json(wire)
        self.assertEqual(parsed.type, "file_message")
        self.assertEqual(parsed.message.file_info.file_id, "f1")
        self.assertEqual(parsed.message.file_info.download_port, 42001)
        self.assertEqual(parsed.message.file_info.file_key, key)
        self.assertEqual(parsed.message.file_info.file_name, "\u62a5\u544a.pdf")

    def test_file_offer_broadcast_and_download(self):
        host = self.start_host()
        sender = None
        fpath = None
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(OTHER_PEER)
            m1.recv()

            sender = P2PManager(Recorder(), port=19999)
            sender.initialize_as_client("\u53d1\u9001\u8005", GROUP_NAME, password=GROUP_PASSWORD)  # 发送者
            sender.confirm_join("127.0.0.1", self.PORT)
            self.assertTrue(wait_until(lambda: sender.connection_result is not None))
            self.assertTrue(sender.connection_result[0])
            m1.recv()

            with tempfile.NamedTemporaryFile(delete=False, suffix=".dat") as f:
                f.write(b"hello file transfer " * 1000)
                fpath = f.name
            msg = sender.send_file(fpath)
            self.assertIsNotNone(msg, "send_file must return the created message")

            pkt = m1.recv()
            self.assertEqual(pkt.type, "file_message")
            info = pkt.message.file_info
            self.assertEqual(info.file_name, os.path.basename(fpath))
            self.assertEqual(info.file_size, os.path.getsize(fpath))
            self.assertTrue(info.file_key, "a per-file key must travel in the offer")

            offer = FileInfo(
                info.file_id, info.file_name, info.file_size,
                "127.0.0.1", info.download_port, info.file_key,
            )
            target = fpath + ".down"
            ok, message = network_module._download_file_offer(offer, target)
            self.assertTrue(ok, message)
            with open(target, "rb") as got, open(fpath, "rb") as want:
                self.assertEqual(got.read(), want.read(), "downloaded bytes must match")
            os.remove(target)
            m1.close()
            m2.close()
        finally:
            if fpath and os.path.exists(fpath):
                os.remove(fpath)
            if sender is not None:
                sender.stop()
            host.stop()

    def test_download_wrong_file_id_rejected(self):
        host = self.start_host()
        sender = None
        fpath = None
        try:
            sender = P2PManager(Recorder(), port=19997)
            sender.initialize_as_client("\u53d1\u9001\u8005", GROUP_NAME, password=GROUP_PASSWORD)
            sender.confirm_join("127.0.0.1", self.PORT)
            self.assertTrue(wait_until(lambda: sender.connection_result is not None))
            self.assertTrue(sender.connection_result[0])
            with tempfile.NamedTemporaryFile(delete=False) as f:
                f.write(b"secret bytes")
                fpath = f.name
            msg = sender.send_file(fpath)
            self.assertIsNotNone(msg)
            fi = msg.file_info

            sock = socket.create_connection(("127.0.0.1", fi.download_port), timeout=6)
            sock.sendall(b'{"type":"file_download","fileId":"wrong-id"}\n')
            sock.settimeout(2)
            buf = bytearray()
            try:
                while True:
                    chunk = sock.recv(1)
                    if not chunk:
                        break
                    buf.extend(chunk)
            except socket.timeout:
                pass
            sock.close()
            self.assertEqual(bytes(buf), b"", "wrong fileId must be rejected without bytes")
        finally:
            if fpath and os.path.exists(fpath):
                os.remove(fpath)
            if sender is not None:
                sender.stop()
            host.stop()


class HeartbeatTest(ProtocolTestBase):
    PORT = 19195

    def _fast_heartbeat(self):
        self._old = (P2PManager.HEARTBEAT_INTERVAL, P2PManager.HEARTBEAT_TIMEOUT)
        P2PManager.HEARTBEAT_INTERVAL = 0.05
        P2PManager.HEARTBEAT_TIMEOUT = 0.4

    def _restore_heartbeat(self):
        P2PManager.HEARTBEAT_INTERVAL, P2PManager.HEARTBEAT_TIMEOUT = self._old

    def test_heartbeat_keeps_connection_alive(self):
        self._fast_heartbeat()
        try:
            host = self.start_host()
            try:
                client = P2PManager(Recorder(), port=19996)
                client.initialize_as_client("\u5fc3\u8df3\u6210\u5458", GROUP_NAME, password=GROUP_PASSWORD)  # 心跳成员
                client.confirm_join("127.0.0.1", self.PORT)
                self.assertTrue(wait_until(lambda: client.connection_result is not None))
                self.assertTrue(client.connection_result[0])
                self.assertTrue(wait_until(lambda: client.my_id in host.peers))
                time.sleep(1.2)
                self.assertFalse(client.connection_lost, "client must stay connected while heartbeats flow")
                self.assertIn(client.my_id, host.peers, "host must keep the member while heartbeats flow")
                client.stop()
            finally:
                host.stop()
        finally:
            self._restore_heartbeat()

    def test_host_detects_silent_member(self):
        self._fast_heartbeat()
        try:
            host = self.start_host()
            try:
                observer = P2PManager(Recorder(), port=19995)
                observer.initialize_as_client("\u89c2\u5bdf\u8005", GROUP_NAME, password=GROUP_PASSWORD)  # 观察者
                observer.confirm_join("127.0.0.1", self.PORT)
                self.assertTrue(wait_until(lambda: observer.connection_result is not None))
                self.assertTrue(observer.connection_result[0])
                self.assertTrue(wait_until(lambda: len(host.peers) == 1))

                s = self.join_member(MEMBER_PEER)
                self.assertTrue(wait_until(lambda: len(host.peers) == 2))
                self.assertTrue(
                    wait_until(lambda: len(host.peers) == 1 and observer.my_id in host.peers),
                    "host must drop the silent member after the heartbeat timeout while keeping the observer",
                )
                self.assertTrue(
                    wait_until(lambda: "android-member-1" not in observer.peers),
                    "observer must process the host's peer_left broadcast for the silent member",
                )
                s.close()
                observer.stop()
            finally:
                host.stop()
        finally:
            self._restore_heartbeat()

    def test_client_detects_silent_host(self):
        self._fast_heartbeat()
        try:
            srv = FakeHostServer(lambda mode, gid: GROUP_PASSWORD)
            port = srv.port
            completed = threading.Event()

            def on_secured(mode, gid, conn, wire):
                if mode == "query":
                    wire.send_packet(NetworkPacket(
                        type="group_info", group_info=GroupInfo(GROUP_NAME, "H", "c", 1),
                    ))
                    conn.close()
                    return
                # join: ack, then the host goes silent (no pings) and the
                # connection disappears — the client must mark itself
                # connection_lost via the read timeout/EOF.
                wire.send_packet(NetworkPacket(
                    type="join_ack", group_id="g",
                    members=[Peer("host", "H", "127.0.0.1", 9999)],
                ))
                # give the client a beat to install the relay, then vanish
                time.sleep(0.2)
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
                completed.set()

            t = threading.Thread(target=lambda: srv.accept_loop(on_secured), daemon=True)
            t.start()
            # give the accept loop's inner thread a moment to be listening, so a
            # burst of connections from the prior test cannot make this one race
            # its own listener startup (the listener binds in __init__, so this
            # is defensive; timeouts below absorb any residual scheduling lag)
            time.sleep(0.2)
            client = P2PManager(Recorder(), port=19994)
            client.initialize_as_client("\u63a2\u6d4b", GROUP_NAME, password=GROUP_PASSWORD)  # 探测
            client.query_group("127.0.0.1", port)
            self.assertTrue(
                wait_until(lambda: client.queried_group_info is not None, timeout=10)
            )
            client.confirm_join("127.0.0.1", port)
            self.assertTrue(
                wait_until(lambda: client.connection_result is not None, timeout=10)
            )
            self.assertTrue(client.connection_result[0])
            self.assertTrue(
                wait_until(lambda: client.connection_lost),
                "client must detect a silent host within the heartbeat timeout",
            )
            client.stop()
            completed.wait(6)
            srv.close()
        finally:
            self._restore_heartbeat()


class CallInfoSerializationTest(unittest.TestCase):
    """Wire format of the video-call packets must match kotlinx.serialization
    on the Android side (compact, camelCase, defaults omitted)."""

    def test_call_offer_kotlinx_compatible(self):
        call = CallInfo("c1", "caller-1", "\u5f20\u4e09", "callee-2", media_port=35001)
        wire = NetworkPacket(type="call_offer", target_id="callee-2", call=call).to_json()
        self.assertNotIn(" ", wire)
        self.assertNotIn("isFromMe", wire)
        parsed = json.loads(wire)
        self.assertEqual(
            parsed,
            {
                "type": "call_offer",
                "targetId": "callee-2",
                "call": {
                    "callId": "c1",
                    "callerId": "caller-1",
                    "callerName": "\u5f20\u4e09",
                    "calleeId": "callee-2",
                    "mediaPort": 35001,
                },
            },
        )
        self.assertNotIn("accepted", parsed["call"])
        self.assertNotIn("audioEnabled", parsed["call"])

    def test_call_answer_omits_media_port(self):
        call = CallInfo("c1", "caller-1", "A", "callee-2")
        wire = NetworkPacket(type="call_answer", target_id="caller-1", call=call).to_json()
        parsed = json.loads(wire)
        self.assertNotIn("mediaPort", parsed["call"], "mediaPort=0 must be omitted")
        self.assertNotIn("accepted", parsed["call"])

    def test_android_payload_decodes(self):
        payload = (
            '{"type":"call_offer","targetId":"b-2","call":{"callId":"c9",'
            '"callerId":"a-1","callerName":"\\u674e\\u56db","calleeId":"b-2",'
            '"mediaPort":42001}}'
        )
        pkt = NetworkPacket.from_json(payload)
        self.assertEqual(pkt.type, "call_offer")
        self.assertEqual(pkt.target_id, "b-2")
        self.assertEqual(pkt.call.call_id, "c9")
        self.assertEqual(pkt.call.caller_name, "\u674e\u56db")  # 李四
        self.assertEqual(pkt.call.media_port, 42001)
        self.assertTrue(pkt.call.accepted, "absent accepted means true")
        self.assertTrue(pkt.call.audio_enabled, "absent audioEnabled means true")

    def test_call_reject_carries_error_message(self):
        call = CallInfo("c1", "a-1", "A", "b-2")
        pkt = NetworkPacket(type="call_reject", target_id="a-1", call=call)
        pkt.error_message = "busy"
        parsed = json.loads(pkt.to_json())
        self.assertEqual(parsed["errorMessage"], "busy")
        self.assertEqual(parsed["call"]["callerId"], "a-1")


class CallRoutingTest(ProtocolTestBase):
    """Targeted delivery of call signaling through the host relay."""

    PORT = 19196

    def _group(self):
        host = self.start_host()
        a = P2PManager(Recorder(), port=21001)
        a.initialize_as_client("\u6210\u5458A", GROUP_NAME, password=GROUP_PASSWORD)
        a.confirm_join("127.0.0.1", self.PORT)
        b = P2PManager(Recorder(), port=21002)
        b.initialize_as_client("\u6210\u5458B", GROUP_NAME, password=GROUP_PASSWORD)
        b.confirm_join("127.0.0.1", self.PORT)
        assert wait_until(lambda: a.connection_result is not None and a.connection_result[0])
        assert wait_until(lambda: b.connection_result is not None and b.connection_result[0])
        assert wait_until(lambda: b.my_id in a.peers and a.my_id in b.peers)
        s = self.join_member(OTHER_PEER)
        assert wait_until(lambda: len(host.peers) == 3)
        return host, a, b, s

    def _offer(self, caller_id, callee_id, call_id="c-1"):
        return NetworkPacket(
            type="call_offer", target_id=callee_id,
            call=CallInfo(call_id, caller_id, "A", callee_id, media_port=35000),
        )

    def test_offer_relayed_only_to_target_member(self):
        host, a, b, raw = self._group()
        received_b = []
        received_a = []
        a.call_listener = lambda p2p, pkt: received_a.append(pkt)
        b.call_listener = lambda p2p, pkt: received_b.append(pkt)
        try:
            a.send_targeted(b.my_id, self._offer(a.my_id, b.my_id))
            self.assertTrue(wait_until(lambda: received_b and received_b[0].type == "call_offer"))
            self.assertEqual(received_b[0].call.caller_id, a.my_id)
            self.assertEqual(received_b[0].target_id, b.my_id)
            self.assertIsNone(raw.recv(skip_heartbeat=True, timeout=0.5))
            self.assertFalse(received_a, "the sender must not receive its own offer echo")
        finally:
            raw.close()
            a.stop()
            b.stop()
            host.stop()

    def test_answer_relayed_back_to_caller(self):
        host, a, b, raw = self._group()
        received_a = []
        received_b = []
        a.call_listener = lambda p2p, pkt: received_a.append(pkt)
        b.call_listener = lambda p2p, pkt: received_b.append(pkt)
        try:
            a.send_targeted(b.my_id, self._offer(a.my_id, b.my_id))
            self.assertTrue(wait_until(lambda: received_b and received_b[0].type == "call_offer"))
            answer = NetworkPacket(type="call_answer", target_id=a.my_id, call=CallInfo("c-1", a.my_id, "A", b.my_id))
            b.send_targeted(a.my_id, answer)
            self.assertTrue(
                wait_until(lambda: received_a and received_a[0].type == "call_answer"),
                "caller must receive the answer",
            )
            self.assertEqual(received_a[0].call.callee_id, b.my_id)
            self.assertIsNone(raw.recv(skip_heartbeat=True, timeout=0.5))
        finally:
            raw.close()
            a.stop()
            b.stop()
            host.stop()

    def test_host_receives_offer_addressed_to_itself(self):
        host, a, b, raw = self._group()
        received_host = []
        host.call_listener = lambda p2p, pkt: received_host.append(pkt)
        try:
            a.send_targeted(host.my_id, self._offer(a.my_id, host.my_id))
            self.assertTrue(
                wait_until(lambda: received_host and received_host[0].type == "call_offer"),
                "the host must handle offers addressed to itself",
            )
            self.assertEqual(received_host[0].call.callee_id, host.my_id)
        finally:
            raw.close()
            a.stop()
            b.stop()
            host.stop()

    def test_forged_caller_id_dropped(self):
        host, a, b, raw = self._group()
        received_b = []
        b.call_listener = lambda p2p, pkt: received_b.append(pkt)
        try:
            forged = self._offer("attacker-id", b.my_id, call_id="c-forged")
            a.send_targeted(b.my_id, forged)
            time.sleep(0.5)
            self.assertFalse(received_b, "a forged callerId must not be relayed to the target")
            self.assertIsNone(raw.recv(skip_heartbeat=True, timeout=0.3))
        finally:
            raw.close()
            a.stop()
            b.stop()
            host.stop()

    def test_unknown_target_dropped_silently(self):
        host, a, b, raw = self._group()
        try:
            a.send_targeted("no-such-member", self._offer(a.my_id, "no-such-member"))
            time.sleep(0.5)
            self.assertIsNone(raw.recv(skip_heartbeat=True, timeout=0.3))
        finally:
            raw.close()
            a.stop()
            b.stop()
            host.stop()


class SanitizeFileNameTest(unittest.TestCase):
    """Inbound file names are attacker-controlled and end up inside the
    suggested save path, so FileInfo.from_dict must sanitize them (path
    components, control characters, Windows-hostile trailing dots/spaces,
    length)."""

    def test_path_components_stripped_both_separators(self):
        self.assertEqual(sanitize_file_name("..\\..\\evil.exe"), "evil.exe")
        self.assertEqual(sanitize_file_name("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_file_name("a/b\\c.txt"), "c.txt")
        self.assertEqual(sanitize_file_name("plain.txt"), "plain.txt")

    def test_control_characters_stripped(self):
        self.assertEqual(sanitize_file_name("a\x00b\x1fc\x7fd.txt"), "abcd.txt")

    def test_trailing_dots_and_spaces_stripped(self):
        self.assertEqual(sanitize_file_name("report.pdf.  "), "report.pdf")
        self.assertEqual(sanitize_file_name("report.pdf.. .."), "report.pdf")

    def test_length_capped_at_255(self):
        out = sanitize_file_name("x" * 300 + ".txt")
        self.assertLessEqual(len(out), 255)
        self.assertTrue(out.startswith("x"))
        self.assertEqual(out, "x" * 255)

    def test_empty_and_path_only_names_fall_back(self):
        self.assertEqual(sanitize_file_name(""), "file")
        self.assertEqual(sanitize_file_name("   "), "file")
        self.assertEqual(sanitize_file_name(".."), "file")
        self.assertEqual(sanitize_file_name("\\\\\\..\\..\\"), "file")

    def test_from_dict_applies_sanitization(self):
        fi = FileInfo.from_dict({
            "fileId": "f1",
            "fileName": "..\\..\\..\\" + "e" * 300 + ".exe.",
            "fileSize": 10,
            "downloadHost": "1.2.3.4",
            "downloadPort": 8080,
        })
        self.assertEqual(fi.file_name, "e" * 255)
        fi2 = FileInfo.from_dict({"fileId": "f2", "fileName": "../..", "fileSize": 1,
                                  "downloadHost": "h", "downloadPort": 1})
        self.assertEqual(fi2.file_name, "file")


class StrictIntCoercionTest(unittest.TestCase):
    """Wire fields must be real ints (or pure digit strings): bools, floats
    and crafted strings are rejected as malformed packets, and port fields
    are range-checked with 0 meaning 'not set'."""

    PEER = {"id": "p", "name": "n", "ipAddress": "1.2.3.4", "port": 9999}

    def test_peer_port(self):
        self.assertEqual(Peer.from_dict(self.PEER).port, 9999)
        self.assertEqual(Peer.from_dict({**self.PEER, "port": "9999"}).port, 9999)
        self.assertEqual(Peer.from_dict({**self.PEER, "port": 0}).port, 0)
        for bad in (True, False, 1.5, "99x99", "", " 80", "1e3", None, [9]):
            with self.assertRaises(ValueError):
                Peer.from_dict({**self.PEER, "port": bad})
        for bad_port in (-1, 65536, "70000"):
            with self.assertRaises(ValueError):
                Peer.from_dict({**self.PEER, "port": bad_port})

    def test_file_info_fields(self):
        base = {"fileId": "f", "fileName": "a.txt", "downloadHost": "1.2.3.4",
                "downloadPort": 8080}
        self.assertEqual(FileInfo.from_dict({**base, "fileSize": "12"}).file_size, 12)
        self.assertEqual(FileInfo.from_dict({**base, "fileSize": 0}).file_size, 0)
        self.assertEqual(FileInfo.from_dict({**base, "downloadPort": "8080"}).download_port, 8080)
        self.assertEqual(FileInfo.from_dict({**base, "downloadPort": 0}).download_port, 0)
        for bad in (True, 12.0, "1e3", None, -1, "-5"):
            with self.assertRaises(ValueError):
                FileInfo.from_dict({**base, "fileSize": bad})
        for bad_port in (65536, -2, True, "8080.0"):
            with self.assertRaises(ValueError):
                FileInfo.from_dict({**base, "downloadPort": bad_port})

    def test_chat_message_timestamp(self):
        base = {"id": "m", "senderId": "s", "content": "hi"}
        self.assertEqual(ChatMessage.from_dict({**base, "timestamp": "123"}).timestamp, 123)
        for bad in (True, 1.5, "now", None):
            with self.assertRaises(ValueError):
                ChatMessage.from_dict({**base, "timestamp": bad})

    def test_call_info_media_port(self):
        base = {"callId": "c", "callerId": "a", "callerName": "A", "calleeId": "b"}
        self.assertEqual(CallInfo.from_dict({**base, "mediaPort": 0}).media_port, 0)
        self.assertEqual(CallInfo.from_dict({**base, "mediaPort": "35001"}).media_port, 35001)
        for bad in (True, 35001.0, "x", -1, 65536):
            with self.assertRaises(ValueError):
                CallInfo.from_dict({**base, "mediaPort": bad})
        # the strict bool checks on accepted/audioEnabled stay in force
        with self.assertRaises(ValueError):
            CallInfo.from_dict({**base, "accepted": 1})


class JoinIdentityTest(ProtocolTestBase):
    """_handle_join must refuse a join with an empty peer id or one that
    impersonates the host, while a same-id rejoin still replaces the old
    connection (identity-spoofing regression)."""

    PORT = 19201

    def test_join_with_empty_peer_id_rejected(self):
        host = self.start_host()
        try:
            s = FakePeerClient.connect("127.0.0.1", self.PORT)
            s.hs_join(GROUP_NAME, GROUP_PASSWORD)
            s.send(NetworkPacket(
                type="join", group_id=GROUP_NAME,
                peer=Peer("", "\u533f\u540d", "192.168.1.9", 9999),  # 匿名
            ))
            resp = s.recv(timeout=3)
            self.assertIsNotNone(resp)
            self.assertEqual(resp.type, "join_rejected")
            self.assertFalse(host.peers, "an empty id must never enter the roster")
            s.close()
        finally:
            host.stop()

    def test_join_impersonating_host_rejected(self):
        host = self.start_host()
        try:
            s = FakePeerClient.connect("127.0.0.1", self.PORT)
            s.hs_join(GROUP_NAME, GROUP_PASSWORD)
            s.send(NetworkPacket(
                type="join", group_id=GROUP_NAME,
                peer=Peer(host.my_id, "\u5047\u4e3b\u673a", "192.168.1.9", 9999),  # 假主机
            ))
            resp = s.recv(timeout=3)
            self.assertIsNotNone(resp)
            self.assertEqual(resp.type, "join_rejected")
            self.assertNotIn(host.my_id, host.peers, "the host id must not be hijackable")
            s.close()
        finally:
            host.stop()

    def test_same_id_rejoin_still_replaces_connection(self):
        host = self.start_host()
        try:
            m1 = self.join_member(MEMBER_PEER)
            m2 = self.join_member(MEMBER_PEER)
            # the fresh connection is now the registered one for the id
            # (join_ack is only sent after registration, so this is stable);
            # compare the TCP endpoints because the host holds the server
            # side of m2's connection
            registered = host._connected_clients.get(MEMBER_PEER.id, {}).get("sock")
            self.assertIsNotNone(registered)
            self.assertEqual(
                registered.getpeername(),
                m2.sock.getsockname(),
                "the rejoin must REPLACE the old connection for the same id",
            )
            # ...and the stale one was closed: no further traffic ever
            # arrives on it
            self.assertTrue(
                wait_until(lambda: m1.recv(timeout=0.3, skip_heartbeat=True) is None),
                "the stale connection must deliver nothing after replacement",
            )
            self.assertTrue(
                wait_until(lambda: MEMBER_PEER.id in host.peers),
                "the rejoining member must stay in the roster",
            )
            m2.send(NetworkPacket(type="chat", message=ChatMessage(
                "m-rejoin", "hi", 1, MEMBER_PEER.id, "\u5f20\u4e09",
            )))
            packet = m2.recv(timeout=3, skip_heartbeat=True)
            self.assertIsNone(packet, "sender must not get its own chat echo")
            self.assertTrue(
                wait_until(lambda: any(m.id == "m-rejoin" for m in host.messages)),
                "the fresh connection must be the live one",
            )
            m1.close()
            m2.close()
        finally:
            host.stop()


class MediaFramingTest(unittest.TestCase):
    def setUp(self):
        install_identity()

    def test_frame_roundtrip_and_chunking(self):
        from localchat import crypto as crypto_mod
        from localchat.call import CH_AUDIO, CH_VIDEO, FrameDecoder, build_frame

        key = crypto_mod.random_bytes(32)
        video = b"\xff\xd8fake-jpeg" * 100
        audio = b"\x00\x01" * 320
        blob_v = crypto_mod.aes_gcm_encrypt(key, video)
        blob_a = crypto_mod.aes_gcm_encrypt(key, audio)
        wire = build_frame(CH_VIDEO, blob_v) + build_frame(CH_AUDIO, blob_a)
        dec = FrameDecoder()
        for i in range(0, len(wire), 7):
            dec.feed(wire[i:i + 7])
        self.assertEqual(len(dec.frames), 2)
        self.assertEqual(dec.frames[0][0], CH_VIDEO)
        self.assertEqual(dec.frames[1][0], CH_AUDIO)
        self.assertEqual(crypto_mod.aes_gcm_decrypt(key, dec.frames[0][1]), video)
        self.assertEqual(crypto_mod.aes_gcm_decrypt(key, dec.frames[1][1]), audio)

    def test_oversized_frame_rejected(self):
        from localchat.call import MAX_FRAME_WIRE_LEN, FrameDecoder

        dec = FrameDecoder()
        huge = (MAX_FRAME_WIRE_LEN + 1).to_bytes(4, "big")
        dec.feed(b"\x00" + huge + b"x" * 16)
        self.assertEqual(dec.frames, [], "oversized frames must be discarded")

    def test_video_frame_decode_size_capped(self):
        """A video frame whose header claims dimensions over the decode cap
        is dropped BEFORE the pixel buffer is allocated (decompression
        bomb); normal frames still decode."""
        from PyQt6.QtCore import QBuffer, QIODevice
        from PyQt6.QtGui import QImage

        from localchat.call import VIDEO_MAX_DECODE_EDGE, _decode_video_frame

        def jpeg_of(width, height):
            img = QImage(width, height, QImage.Format.Format_RGB32)
            img.fill(0)
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.ReadWrite)
            if not img.save(buf, "JPG"):
                self.skipTest("Qt JPEG image plugin unavailable")
            return bytes(buf.data())

        bomb = jpeg_of(VIDEO_MAX_DECODE_EDGE + 1, 8)
        self.assertIsNone(
            _decode_video_frame(bomb),
            "an oversized frame must be dropped without decoding",
        )
        ok = _decode_video_frame(jpeg_of(32, 24))
        self.assertIsNotNone(ok)
        self.assertFalse(ok.isNull())
        self.assertEqual((ok.width(), ok.height()), (32, 24))


class CallManagerSignalingTest(unittest.TestCase):
    """End-to-end signaling + media-connection flow between two CallManagers
    on one machine (host relays; media socket is a loopback TCP pair, secured
    by the identity handshake + AES-GCM frames)."""

    PORT = 19197

    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        if cls.app is not None:
            cls.app.processEvents()

    def _wait_qt(self, cond, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            self.app.processEvents()
            time.sleep(0.01)
        return False

    def test_full_call_flow(self):
        from localchat.call import STATE_ACTIVE, STATE_IDLE, STATE_INCOMING, STATE_OUTGOING, CallManager

        install_identity()
        host = P2PManager(Recorder(), port=self.PORT, password=GROUP_PASSWORD)
        host.initialize_as_host(HOST_NAME, GROUP_NAME)
        host.start_as_host()

        a = P2PManager(Recorder(), port=21011)
        a.initialize_as_client("\u547c\u53eb\u8005", GROUP_NAME, password=GROUP_PASSWORD)  # 呼叫者
        a.confirm_join("127.0.0.1", self.PORT)
        b = P2PManager(Recorder(), port=21012)
        b.initialize_as_client("\u88ab\u53eb\u8005", GROUP_NAME, password=GROUP_PASSWORD)  # 被叫者
        b.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: a.connection_result is not None and a.connection_result[0]))
        self.assertTrue(wait_until(lambda: b.connection_result is not None and b.connection_result[0]))
        self.assertTrue(wait_until(lambda: b.my_id in a.peers and a.my_id in b.peers))

        cm_a = CallManager()
        cm_b = CallManager()
        a.call_listener = cm_a._on_signal
        b.call_listener = cm_b._on_signal
        try:
            cm_a.start_call(a, b.my_id)
            self.assertTrue(self._wait_qt(lambda: cm_a.state == STATE_OUTGOING))
            self.assertTrue(self._wait_qt(lambda: cm_b.state == STATE_INCOMING), "callee must enter incoming")
            self.assertEqual(cm_b.peer_name, "\u547c\u53eb\u8005")

            cm_b.accept_call()
            self.assertTrue(
                self._wait_qt(lambda: cm_a.state == STATE_ACTIVE and cm_b.state == STATE_ACTIVE),
                "both sides must reach the active state",
            )
            self.assertTrue(
                self._wait_qt(lambda: cm_a._media_socket is not None and cm_b._media_socket is not None)
            )
            time.sleep(0.6)
            self.app.processEvents()

            self.assertIsNotNone(cm_a._media_key, "caller must have the media session key")
            self.assertIsNotNone(cm_b._media_key, "callee must have the media session key")

            cm_b.hangup()
            self.assertTrue(
                self._wait_qt(lambda: cm_a.state == STATE_IDLE and cm_b.state == STATE_IDLE),
                "hangup must bring both sides back to idle",
            )
        finally:
            cm_a.hangup()
            cm_b.hangup()
            a.stop()
            b.stop()
            host.stop()

    def test_second_call_after_first_ends(self):
        """Regression: the media-stop event must be per-CALL, not sticky.
        CallManager is an app-wide singleton, and the teardown of the first
        call used to leave the media stop flag set forever — the SECOND
        call's read loop then exited at its first check and the call ended
        instantly with 对方已挂断, with every outgoing frame dropped."""
        from localchat.call import (
            STATE_ACTIVE,
            STATE_IDLE,
            STATE_INCOMING,
            STATE_OUTGOING,
            CallManager,
        )

        install_identity()
        host = P2PManager(Recorder(), port=19198, password=GROUP_PASSWORD)
        host.initialize_as_host(HOST_NAME, GROUP_NAME)
        host.start_as_host()

        a = P2PManager(Recorder(), port=21113)
        a.initialize_as_client("\u547c\u53eb\u8005", GROUP_NAME, password=GROUP_PASSWORD)
        a.confirm_join("127.0.0.1", 19198)
        b = P2PManager(Recorder(), port=21114)
        b.initialize_as_client("\u88ab\u53eb\u8005", GROUP_NAME, password=GROUP_PASSWORD)
        b.confirm_join("127.0.0.1", 19198)
        self.assertTrue(wait_until(lambda: a.connection_result is not None and a.connection_result[0]))
        self.assertTrue(wait_until(lambda: b.connection_result is not None and b.connection_result[0]))
        self.assertTrue(wait_until(lambda: b.my_id in a.peers and a.my_id in b.peers))

        cm_a = CallManager()
        cm_b = CallManager()
        a.call_listener = cm_a._on_signal
        b.call_listener = cm_b._on_signal
        try:
            def one_call(tag):
                cm_a.start_call(a, b.my_id)
                self.assertTrue(self._wait_qt(lambda: cm_a.state == STATE_OUTGOING), tag)
                self.assertTrue(self._wait_qt(lambda: cm_b.state == STATE_INCOMING), tag)
                cm_b.accept_call()
                self.assertTrue(
                    self._wait_qt(lambda: cm_a.state == STATE_ACTIVE and cm_b.state == STATE_ACTIVE),
                    tag,
                )
                cm_b.hangup()
                self.assertTrue(
                    self._wait_qt(lambda: cm_a.state == STATE_IDLE and cm_b.state == STATE_IDLE),
                    tag,
                )

            one_call("first call")
            # the second call must go live and STAY live: with the sticky
            # stop flag the read loop exits immediately and the call would
            # collapse back to idle within a moment
            cm_a.start_call(a, b.my_id)
            self.assertTrue(self._wait_qt(lambda: cm_a.state == STATE_OUTGOING), "second call")
            self.assertTrue(self._wait_qt(lambda: cm_b.state == STATE_INCOMING), "second call")
            cm_b.accept_call()
            self.assertTrue(
                self._wait_qt(lambda: cm_a.state == STATE_ACTIVE and cm_b.state == STATE_ACTIVE),
                "second call must reach active",
            )
            self.assertTrue(
                self._wait_qt(
                    lambda: cm_a._media_socket is not None and cm_b._media_socket is not None
                ),
                "second call must open a media socket",
            )
            time.sleep(1.0)
            self.app.processEvents()
            self.assertEqual(cm_a.state, STATE_ACTIVE, "second call must STAY active")
            self.assertEqual(cm_b.state, STATE_ACTIVE, "second call must STAY active")
        finally:
            cm_a.hangup()
            cm_b.hangup()
            a.stop()
            b.stop()
            host.stop()


if __name__ == "__main__":
    unittest.main()
