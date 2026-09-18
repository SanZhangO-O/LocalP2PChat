"""Regression tests for the security/robustness fixes:

1. SecureWire anti-replay: a line replayed WITHIN one connection must abort
   the wire (per-session ECDH keys already make cross-session replay fail),
   and the per-direction packet sequence ("seq", stamped inside the
   GCM-protected JSON) rejects replayed/reordered/injected lines even after
   the nonce LRU evicted them; a relay hop restamps seq on its own wire.
3. file_download token: MANDATORY on every request — the downloader always
   attaches base64(HMAC-SHA256(fileKey, "lc-file-dl-v1:" + fileId)); the
   sender verifies it in constant time and serves NOTHING on mismatch or
   absence. models.py tolerates the field.
4. Download progress + cancel: progress(received, total) fires per chunk; a
   cancel Event aborts with "下载已取消" and removes the .part file.
6. numeric_group_id_of iterates UTF-16 code units (Kotlin ch.code parity):
   fixed vector for an astral-char name; BMP names unchanged.
8. accept_contact_request dials the peer immediately (no 60s sweep wait).
9. Contact requests carry the dialer's identity fingerprint (安全码).
10. Group passwords / chat bodies are encrypted at rest (secretbox).

Chinese literals are \\uXXXX escapes so the file stays pure-ASCII on disk.
"""

import hashlib
import hmac as hmac_mod
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from localchat.crypto import random_bytes, to_b64
from localchat.models import FileInfo, NetworkPacket, Peer
from localchat.network import (
    DirectChatManager,
    HostGroupServer,
    _download_file_offer,
    _serve_file_download,
    file_download_token,
    numeric_group_id_of,
)
from localchat.securewire import NONCE_CACHE_SIZE, Wire, WireException
from tests.fake_peer import install_identity

A_ID = "zzz-A"  # larger id: the deterministic dialer rule never picks it
B_ID = "aaa-B"
NAME_A = "\u5c0fA"  # 小A
NAME_B = "\u5c0fB"  # 小B


def wait_until(cond, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


class WireReplayTest(unittest.TestCase):
    KEY = b"\x07" * 32

    def _lines(self, n):
        sent = []

        def write(line):
            sent.append(line)

        sender = Wire(None, write)
        sender.activate(self.KEY)
        for i in range(n):
            sender.send_packet(NetworkPacket(type="ping"))
        return sent

    def test_replayed_line_aborts_connection(self):
        """The same encrypted line delivered twice: the first read succeeds,
        the replay must raise WireException (same failure path as decrypt
        failure) so callers drop the connection."""
        sent = self._lines(2)
        lines = [sent[0], sent[0]]  # attacker replays the FIRST line
        receiver = Wire(lambda: lines.pop(0) if lines else None, None)
        receiver.activate(self.KEY)
        self.assertIsNotNone(receiver.recv_packet())
        with self.assertRaises(WireException):
            receiver.recv_packet()

    def test_distinct_lines_accepted(self):
        """No false positives: two different lines (fresh nonces) both
        decrypt."""
        sent = self._lines(2)
        lines = list(sent)
        receiver = Wire(lambda: lines.pop(0) if lines else None, None)
        receiver.activate(self.KEY)
        self.assertIsNotNone(receiver.recv_packet())
        self.assertIsNotNone(receiver.recv_packet())

    def test_replayed_line_rejected_even_after_window(self):
        """The seq guard closes the old LRU-eviction hole: a replayed OLD
        line (nonce long evicted from the cache) is still rejected because
        its sequence number is not the exact next one."""
        old_cap = NONCE_CACHE_SIZE
        try:
            import localchat.securewire as sw

            sw.NONCE_CACHE_SIZE = 8
            sent = self._lines(12)
            # consume lines 0..9 (seq 1..10): line 0's nonce was evicted from
            # the 8-entry cache long before the replay arrives
            lines = [sent[i] for i in range(0, 10)] + [sent[0]]
            receiver = Wire(lambda: lines.pop(0) if lines else None, None)
            receiver.activate(self.KEY)
            for _ in range(10):
                self.assertIsNotNone(receiver.recv_packet())
            with self.assertRaises(WireException):
                receiver.recv_packet()
        finally:
            import localchat.securewire as sw

            sw.NONCE_CACHE_SIZE = old_cap

    def _raw_line(self, payload: str) -> str:
        from localchat.crypto import aes_gcm_encrypt

        return to_b64(aes_gcm_encrypt(self.KEY, payload.encode("utf-8")))

    def test_missing_seq_rejected(self):
        """An attacker-crafted line (valid GCM under a stolen key scenario,
        or any non-compliant peer) without the sequence field is rejected."""
        lines = [self._raw_line('{"type":"ping"}')]
        receiver = Wire(lambda: lines.pop(0) if lines else None, None)
        receiver.activate(self.KEY)
        with self.assertRaises(WireException):
            receiver.recv_packet()

    def test_out_of_order_seq_rejected(self):
        """Skipped/reordered sequences (1 then 3) never validate: only the
        exact next number is legal."""
        lines = [
            self._raw_line('{"type":"ping","seq":1}'),
            self._raw_line('{"type":"ping","seq":3}'),
        ]
        receiver = Wire(lambda: lines.pop(0) if lines else None, None)
        receiver.activate(self.KEY)
        self.assertIsNotNone(receiver.recv_packet())
        with self.assertRaises(WireException):
            receiver.recv_packet()

    def test_relay_restamps_seq_per_wire(self):
        """A host relays the SAME packet object to several member wires; each
        outgoing wire stamps its own 1,2,3,... sequence, so every receiver
        sees a strictly increasing stream (no false replay positives from
        the forwarded packet's original seq)."""
        out_a, out_b = [], []
        sender_a = Wire(None, out_a.append)
        sender_b = Wire(None, out_b.append)
        sender_a.activate(self.KEY)
        sender_b.activate(self.KEY)
        shared = NetworkPacket(type="ping")
        sender_a.send_packet(shared)  # stamped seq=1 on wire A
        sender_b.send_packet(shared)  # stamped seq=1 on wire B
        for lines in (out_a, out_b):
            receiver = Wire(lambda lines=lines: lines.pop(0) if lines else None, None)
            receiver.activate(self.KEY)
            self.assertIsNotNone(receiver.recv_packet())

    def test_raw_io_forbidden_after_activate(self):
        """send_raw/recv_raw were handshake-phase only; after activate() they
        must raise instead of silently bypassing the encryption."""
        sent = []
        wire = Wire(lambda: None, sent.append)
        wire.activate(self.KEY)
        with self.assertRaises(WireException):
            wire.send_raw(NetworkPacket(type="ping"))
        with self.assertRaises(WireException):
            wire.recv_raw()


class FileDownloadTokenTest(unittest.TestCase):
    """Real download server (a thread running _serve_file_download) + the
    real downloader (_download_file_offer) over loopback."""

    FILE_ID = "dl-token-file-1"
    PAYLOAD = b"token protected payload " * 2048  # ~48KB > one chunk

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lc_token_")
        self.src = os.path.join(self.tmp, "src.bin")
        with open(self.src, "wb") as f:
            f.write(self.PAYLOAD)
        self.file_key = random_bytes(32)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        srv.settimeout(8)
        self.srv = srv
        self.port = srv.getsockname()[1]

        def accept_loop():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                threading.Thread(
                    target=_serve_file_download,
                    args=(conn, self.FILE_ID, self.src, len(self.PAYLOAD), self.file_key),
                    daemon=True,
                ).start()

        self.accept_thread = threading.Thread(target=accept_loop, daemon=True)
        self.accept_thread.start()

    def tearDown(self):
        self.srv.close()
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _offer(self):
        return FileInfo(
            self.FILE_ID,
            "src.bin",
            len(self.PAYLOAD),
            "127.0.0.1",
            self.port,
            to_b64(self.file_key),
        )

    def _capture_request(self, srv):
        """Serve ONE connection that only records the request line."""
        conn, _ = srv.accept()
        conn.settimeout(6)
        buf = bytearray()
        try:
            while b"\n" not in buf:
                b = conn.recv(1)
                if not b:
                    break
                buf.extend(b)
        except OSError:
            pass
        conn.close()
        return json.loads(bytes(buf).decode("utf-8"))

    @staticmethod
    def _capture_server():
        """Dedicated listener for request capture: must NOT share the setUp
        socket, or the real accept_loop races this capture for the single
        incoming connection (the loser blocks until timeout)."""
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        s.settimeout(8)
        return s

    def test_models_tolerates_token_field(self):
        raw = json.dumps(
            {"type": "file_download", "fileId": "f1", "token": "abc123"}
        )
        pkt = NetworkPacket.from_json(raw)
        self.assertEqual(pkt.token, "abc123")
        self.assertEqual(json.loads(pkt.to_json())["token"], "abc123")
        legacy = NetworkPacket.from_json('{"type":"file_download","fileId":"f1"}')
        self.assertIsNone(legacy.token)
        self.assertNotIn("token", legacy.to_json())

    def test_downloader_always_sends_matching_token(self):
        """The request line must carry token = HMAC(fileKey, prefix+fileId)."""
        result = {}
        cap_srv = self._capture_server()
        offer = FileInfo(
            self.FILE_ID,
            "src.bin",
            len(self.PAYLOAD),
            "127.0.0.1",
            cap_srv.getsockname()[1],
            to_b64(self.file_key),
        )

        def run():
            result["r"] = _download_file_offer(
                offer, os.path.join(self.tmp, "out.bin")
            )

        t = threading.Thread(target=run, daemon=True)
        t.start()
        try:
            req = self._capture_request(cap_srv)
        finally:
            cap_srv.close()
        t.join(timeout=6)
        self.assertEqual(req["type"], "file_download")
        self.assertEqual(req["fileId"], self.FILE_ID)
        want = to_b64(
            hmac_mod.new(
                self.file_key,
                f"lc-file-dl-v1:{self.FILE_ID}".encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        self.assertEqual(req["token"], want)
        self.assertEqual(req["token"], file_download_token(self.file_key, self.FILE_ID))

    def test_wrong_token_gets_no_meta_no_bytes(self):
        """A token that does not verify: the server closes without sending
        the meta line or any stream byte."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=6)
        bad = NetworkPacket(
            type="file_download", file_id=self.FILE_ID, token=to_b64(b"\x00" * 32)
        )
        sock.sendall((bad.to_json() + "\n").encode("utf-8"))
        sock.settimeout(2)
        buf = bytearray()
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
        except socket.timeout:
            pass
        sock.close()
        self.assertEqual(
            bytes(buf), b"", "a bad token must be answered with silence"
        )

    def test_empty_token_rejected_like_android(self):
        """A PRESENT but empty token is a wrong token (Android checks
        `!= null`), not a legacy peer: the Windows check used truthiness and
        served the file. Parity regression."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=6)
        empty = NetworkPacket(type="file_download", file_id=self.FILE_ID, token="")
        sock.sendall((empty.to_json() + "\n").encode("utf-8"))
        sock.settimeout(2)
        buf = bytearray()
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf.extend(chunk)
        except socket.timeout:
            pass
        sock.close()
        self.assertEqual(
            bytes(buf), b"", "an empty token must be refused, not served"
        )

    def test_absent_token_rejected(self):
        """A request with NO token field is refused: the token proves the
        downloader received the ENCRYPTED offer, so a requester that knows
        only the (sniffable) request format gets nothing."""
        from localchat.network import _read_raw_line

        sock = socket.create_connection(("127.0.0.1", self.port), timeout=6)
        try:
            sock.sendall(
                (
                    NetworkPacket(type="file_download", file_id=self.FILE_ID).to_json()
                    + "\n"
                ).encode("utf-8")
            )
            sock.settimeout(2)
            buf = bytearray()
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf.extend(chunk)
            except socket.timeout:
                pass
            self.assertEqual(
                bytes(buf), b"", "a token-less request must be answered with silence"
            )
        finally:
            sock.close()

    def test_download_with_progress_and_success(self):
        """The happy path end-to-end: token accepted, file complete, progress
        callback saw monotonically growing counts up to the full size."""
        seen = []

        def progress(received, total):
            seen.append((received, total))

        target = os.path.join(self.tmp, "out.bin")
        ok, message = _download_file_offer(self._offer(), target, progress=progress)
        self.assertTrue(ok, message)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), self.PAYLOAD)
        self.assertTrue(seen, "progress must fire")
        self.assertEqual(seen[-1][0], len(self.PAYLOAD))
        self.assertGreater(seen[-1][1], 0)
        self.assertFalse(os.path.exists(target + ".part"))

    def test_cancel_before_start_aborts(self):
        cancel = threading.Event()
        cancel.set()
        target = os.path.join(self.tmp, "cancelled.bin")
        ok, message = _download_file_offer(self._offer(), target, cancel=cancel)
        self.assertFalse(ok)
        self.assertEqual(message, "\u4e0b\u8f7d\u5df2\u53d6\u6d88")  # 下载已取消
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))

    def test_cancel_after_connect_closes_the_socket(self):
        """A cancel landing between connect and the first read must not leak
        the socket: the early return now happens INSIDE the try/finally that
        closes the socket and removes the .part file."""
        import localchat.network as net

        cancel = threading.Event()
        holder = []
        srv = self._capture_server()
        real_create = net.socket.create_connection

        def fake_create(addr, timeout=None):
            s = real_create(addr, timeout=timeout)
            cancel.set()  # the cancel arrives while the connect was in flight
            return s

        net.socket.create_connection = fake_create
        try:
            ok, message = _download_file_offer(
                FileInfo(
                    self.FILE_ID,
                    "src.bin",
                    len(self.PAYLOAD),
                    "127.0.0.1",
                    srv.getsockname()[1],
                    to_b64(self.file_key),
                ),
                os.path.join(self.tmp, "never.bin"),
                cancel=cancel,
                sock_holder=holder,
            )
        finally:
            net.socket.create_connection = real_create
            srv.close()

        self.assertFalse(ok)
        self.assertEqual(message, "\u4e0b\u8f7d\u5df2\u53d6\u6d88")  # 下载已取消
        self.assertTrue(holder, "the socket must be handed to the canceller")
        for s in holder:
            with self.assertRaises(OSError):
                s.getpeername()  # closed
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "never.bin")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "never.bin.part")))

    def test_cancel_mid_download(self):
        """Cancelling after the first chunk aborts promptly and removes the
        partial .part file."""
        cancel = threading.Event()
        holder = []

        def progress(_received, _total):
            cancel.set()

        target = os.path.join(self.tmp, "mid.bin")
        ok, message = _download_file_offer(
            self._offer(), target, progress=progress, cancel=cancel, sock_holder=holder
        )
        self.assertFalse(ok)
        self.assertEqual(message, "\u4e0b\u8f7d\u5df2\u53d6\u6d88")  # 下载已取消
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".part"))


class TombstoneIntakeTest(unittest.TestCase):
    """One packet's tombstone list is deduped/blanks-dropped/capped before any
    state or database work: the deleted_messages table itself keeps at most
    ChatStore.TOMBSTONE_CAP ids per group, so a bigger list only buys the
    sender unbounded work here."""

    def test_sanitize_deleted_ids_dedupes_caps_and_drops_junk(self):
        from localchat.network import (
            MAX_DELETED_ID_LEN,
            MAX_DELETED_IDS,
            sanitize_deleted_ids,
        )
        from localchat.storage import ChatStore

        # keeping the wire cap aligned with the table cap is what makes the
        # cap a no-op for honest peers and the table bounded for everyone
        self.assertEqual(MAX_DELETED_IDS, ChatStore.TOMBSTONE_CAP)
        self.assertEqual(sanitize_deleted_ids(["a", "", "b", "a"]), ["a", "b"])
        self.assertEqual(sanitize_deleted_ids(None), [])
        self.assertEqual(sanitize_deleted_ids(["   "]), [])
        self.assertEqual(
            sanitize_deleted_ids("x" * (MAX_DELETED_ID_LEN + 1)), []
        )
        # a malformed scalar must not iterate into single-character ids
        self.assertEqual(sanitize_deleted_ids("not-a-list"), ["not-a-list"])
        many = [f"m-{i}" for i in range(1, MAX_DELETED_IDS + 26)]
        capped = sanitize_deleted_ids(many)
        self.assertEqual(len(capped), MAX_DELETED_IDS)
        self.assertEqual(capped[0], "m-1")
        self.assertEqual(capped[-1], f"m-{MAX_DELETED_IDS}")

    def test_malformed_deleted_ids_field_is_ignored(self):
        from localchat.models import NetworkPacket

        # a scalar where a list belongs parses as "no tombstones" instead of
        # exploding into per-character ids
        pkt = NetworkPacket.from_json(
            '{"type":"join_ack","groupId":"g","deletedIds":"abc"}'
        )
        self.assertIsNone(pkt.deleted_ids)


class Utf16NumericIdTest(unittest.TestCase):
    def test_astral_vector_matches_utf16_algorithm(self):
        """Vector groupName="测试😀群", fingerprint="0123456789abcdef":
        hashed over UTF-16 code units (the emoji counts as its surrogate
        pair), expected id 54153344."""
        self.assertEqual(
            numeric_group_id_of("\u6d4b\u8bd5\U0001F600\u7fa4", "0123456789abcdef"),
            "54153344",
        )

    def test_bmp_names_unchanged_vs_codepoint_iteration(self):
        """Backward compat: BMP-only names hash identically under the old
        (code point) and new (UTF-16 unit) iterations."""
        def old_codepoint(gs, fp):
            h = 0x811C9DC5
            for ch in gs + "\u0000" + fp:
                h ^= ord(ch)
                h = (h * 0x01000193) & 0xFFFFFFFF
            return str((h % 100_000_000 + 100_000_000) % 100_000_000).zfill(8)

        for name, fp in (
            ("\u4f1a\u8bae\u5ba4", "fp-1"),  # 会议室
            ("ascii-name", "0123456789abcdef"),
            ("\u6d4b\u8bd5\u7fa4", "fp-2"),  # 测试群 (no emoji)
        ):
            self.assertEqual(numeric_group_id_of(name, fp), old_codepoint(name, fp))

    def test_still_stable_and_distinct(self):
        a = numeric_group_id_of("g", "fp1")
        self.assertEqual(a, numeric_group_id_of("g", "fp1"))
        self.assertNotEqual(a, numeric_group_id_of("g", "fp2"))
        self.assertNotEqual(a, numeric_group_id_of("h", "fp1"))
        self.assertEqual(len(a), 8)
        self.assertTrue(a.isdigit())


class AcceptRequestImmediateDialTest(unittest.TestCase):
    """Item 8: accepting a parked request must DIAL immediately. A has the
    LARGER id, so the deterministic sweep rule would never let A dial — only
    the immediate acceptance dial can converge within the test window (the
    sweep is stretched to 60s so it cannot rescue the assertion)."""

    PORT_A = 19761
    PORT_B = 19762

    def setUp(self):
        install_identity()
        self._old_sweep = DirectChatManager.PRESENCE_SWEEP
        DirectChatManager.PRESENCE_SWEEP = 60.0
        self.server_a = HostGroupServer(self.PORT_A)
        self.server_a.ensure_running()
        self.a = DirectChatManager()
        self.a.configure(A_ID, NAME_A, "127.0.0.1", self.PORT_A)
        self.server_a.direct_manager = self.a
        self.server_b = HostGroupServer(self.PORT_B)
        self.server_b.ensure_running()
        self.b = DirectChatManager()
        self.b.configure(B_ID, NAME_B, "127.0.0.1", self.PORT_B)
        self.server_b.direct_manager = self.b
        self.peer_a = Peer(A_ID, NAME_A, "127.0.0.1", self.PORT_A)
        self.peer_b = Peer(B_ID, NAME_B, "127.0.0.1", self.PORT_B)

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.server_a.shutdown()
        self.server_b.shutdown()
        DirectChatManager.PRESENCE_SWEEP = self._old_sweep

    def test_accept_dials_without_waiting_for_sweep(self):
        self.b.add_contact(self.peer_a)
        result = self.b.start_chat(self.peer_a, quiet=False)
        self.assertIsNone(result, "first contact parks, no session yet")
        self.assertTrue(
            wait_until(lambda: any(r.id == B_ID for r in self.a.contact_requests())),
            "request must park in A's box",
        )
        started = time.time()
        self.a.accept_contact_request(B_ID)
        # A's sweep (deterministic rule) skips B; ONLY the immediate dial
        # opens the session — and it must be fast, not a sweep period
        self.assertTrue(
            wait_until(lambda: self.a.is_chat_alive(B_ID), timeout=5.0),
            "the accepting side must dial the peer immediately",
        )
        self.assertLess(time.time() - started, 5.0, "convergence must not wait a sweep")
        self.assertTrue(
            wait_until(lambda: self.b.is_chat_alive(A_ID), timeout=5.0),
            "B must see the accepted session",
        )
        self.assertTrue(
            any(c.id == B_ID for c in self.a.contacts_list()),
            "accepting added the member",
        )
        self.assertTrue(self.a.send_message(B_ID, "hi"))
        self.assertTrue(
            wait_until(
                lambda: any(m.content == "hi" for m in self.b.messages_for(A_ID))
            ),
            "the accepted session carries traffic",
        )


class ContactRequestFingerprintTest(unittest.TestCase):
    """Item 9: a parked request carries the dialer's 安全码 so the user can
    compare it out-of-band before accepting (first-contact MITM check)."""

    def test_record_persists_fingerprint_and_roundtrips(self):
        from localchat.models import ContactRequest

        mgr = DirectChatManager()
        try:
            mgr.record_contact_request(
                Peer(B_ID, NAME_B, "192.168.1.9", 9999),
                from_removed=False,
                peer_fingerprint="ABCDEF0123456789",
            )
            req = next(r for r in mgr.contact_requests() if r.id == B_ID)
            self.assertEqual(req.peer_fingerprint, "ABCDEF0123456789")
            # persistence roundtrip keeps the fingerprint
            restored = ContactRequest.from_dict(req.to_dict())
            self.assertEqual(restored.peer_fingerprint, "ABCDEF0123456789")
        finally:
            mgr.shutdown()

    def test_fingerprint_optional(self):
        from localchat.models import ContactRequest

        legacy = ContactRequest.from_dict(
            {"id": "x", "name": "n", "ip": "1.2.3.4", "port": 1}
        )
        self.assertEqual(legacy.peer_fingerprint, "")
        self.assertNotIn("peerFingerprint", legacy.to_dict())


class SecretAtRestTest(unittest.TestCase):
    """Item 10: group passwords and chat bodies are stored ENCRYPTED in the
    database and read back transparently."""

    def setUp(self):
        import localchat.storage as storage_module
        from localchat.storage import ChatStore

        self.storage_module = storage_module
        self.tmp = tempfile.mkdtemp(prefix="lc_secret_")
        self.store = ChatStore(os.path.join(self.tmp, "chat.db"))
        # saved_messages carries a FK onto saved_groups: create the parent
        self.store.upsert_group(
            self.storage_module.SavedGroup(
                group_id="g1", group_name="G", is_host=True, created_at=1
            )
        )

    def tearDown(self):
        self.store.close()
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def test_group_password_roundtrip_and_ciphertext_at_rest(self):
        raw = self.storage_module.secretbox  # module present
        self.store.set_group_password("g1", "s3cret-pw")
        self.assertEqual(self.store.get_group_password("g1"), "s3cret-pw")
        row = self.store._conn.execute(
            "SELECT value FROM settings WHERE key = 'group_password_g1'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertNotEqual(row["value"], "s3cret-pw")
        self.assertTrue(row["value"].startswith("enc1:"))

    def test_message_content_encrypted_at_rest(self):
        from localchat.storage import SavedMessage

        self.store.insert_messages(
            [SavedMessage("m1", "g1", "秘密消息", 1, "s", "S", True)]
        )
        msgs = self.store.get_messages_for_group("g1")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].content, "秘密消息")
        row = self.store._conn.execute(
            "SELECT content FROM saved_messages WHERE id = 'm1'"
        ).fetchone()
        self.assertNotEqual(row["content"], "秘密消息")
        self.assertTrue(row["content"].startswith("enc1:"))

    def test_plaintext_rows_still_readable(self):
        """A value written without the encryption layer (or one whose key was
        lost) must not crash readers: unprotect passes plaintext through and
        degrades undecryptable ciphertext to ''."""
        self.assertEqual(
            self.storage_module.secretbox.unprotect(self.tmp, "plain text"),
            "plain text",
        )
        self.assertEqual(
            self.storage_module.secretbox.unprotect(self.tmp, "enc1:bmd4"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
