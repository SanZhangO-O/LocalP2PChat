"""Call-scope regression tests: a LATE event from a previous call must never
touch the current one (call.py scopes its private signals by call_id).

Covered without any network/P2P setup:
- late _sig_media_socket is closed instead of adopted while a new call is
  OUTGOING (previously the new call was hijacked/killed)
- late _sig_media_ended / _sig_connect_failed are ignored when the call_id
  does not match, while the matching one still works (control cases)
- the caller's accept loop exits promptly after cancel (short-timeout polling)
  instead of blocking in accept()
- the media sender thread is joined at teardown and a new call can start a
  fresh one (isAlive guard no longer starves)
- _media_port is initialized

Every socket binds an ephemeral port, so the file is repeatable.
"""

import os
import socket
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _Peer:
    def __init__(self, pid: str, name: str, ip: str):
        self.id = pid
        self.name = name
        self.ip_address = ip


class _P2PStub:
    """Just enough of P2PManager for CallManager.start_call."""

    def __init__(self, my_id: str, my_name: str):
        self.my_id = my_id
        self.my_name = my_name
        self.peers = {}
        self.sent = []

    def send_targeted(self, pid, pkt):
        self.sent.append((pid, pkt))


def _loopback_socket():
    """A connected TCP socket (the client side) plus its listener, so the
    test can observe a close via fileno() == -1."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cli.connect(srv.getsockname())
    accepted, _ = srv.accept()
    srv.close()
    return cli, accepted


class CallScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PyQt6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    @classmethod
    def tearDownClass(cls):
        if cls.app is not None:
            cls.app.processEvents()

    def setUp(self):
        from localchat.call import CallManager

        self.cm = CallManager()
        self.cleanup = []

    def tearDown(self):
        self.cm.hangup()
        for fn in self.cleanup:
            fn()
        self.app.processEvents()

    # ------------------------------------------------------------- helpers

    def _start_outgoing(self, call_peer_id="peer-1"):
        p2p = _P2PStub("me-1", "\u6211")
        p2p.peers[call_peer_id] = _Peer(call_peer_id, "\u5bf9\u65b9", "127.0.0.1")
        self.cm.start_call(p2p, call_peer_id)
        self.assertEqual(self.cm.state, "outgoing")
        return p2p

    def _start_incoming(self, call_id="call-B"):
        from localchat.models import CallInfo, NetworkPacket

        sent = []
        offer = NetworkPacket(
            type="call_offer",
            call=CallInfo(
                call_id=call_id,
                caller_id="caller-1",
                caller_name="\u547c\u53eb\u8005",
                callee_id="me-1",
                media_port=12345,
            ),
        )
        self.cm.handle_direct_signal(
            channel_send=lambda pid, pkt: sent.append((pid, pkt)),
            identity=object(),
            packet=offer,
            caller_ip="127.0.0.1",
            my_id="me-1",
            my_name="\u6211",
        )
        self.assertEqual(self.cm.state, "incoming")
        return sent

    def _accept_thread_alive(self):
        return [
            t
            for t in threading.enumerate()
            if getattr(t, "_target", None) == self.cm._accept_loop
        ]

    # ------------------------------------------------------- fix 1: scoping

    def test_late_media_socket_dropped_when_new_call_outgoing(self):
        """A media socket from a PREVIOUS call must be closed, not adopted
        into the current call (which is already past IDLE)."""
        self._start_outgoing()
        late, accepted = _loopback_socket()
        self.cleanup.append(accepted.close)
        self.assertNotEqual(late.fileno(), -1)

        self.cm._sig_media_socket.emit("stale-call-id", late, b"\x01" * 32)
        self.app.processEvents()

        self.assertEqual(late.fileno(), -1, "late media socket must be closed")
        self.assertIsNone(self.cm._media_socket, "current call must not adopt it")
        self.assertEqual(self.cm.state, "outgoing", "current call must survive")

    def test_late_media_ended_ignored_but_current_still_works(self):
        self._start_outgoing()

        self.cm._sig_media_ended.emit("stale-call-id", "\u5b89\u5168\u63e1\u624b\u5931\u8d25")
        self.app.processEvents()
        self.assertEqual(self.cm.state, "outgoing", "late failure must be ignored")

        ended = []
        self.cm.call_ended.connect(lambda r: ended.append(r))
        self.cm._sig_media_ended.emit(self.cm._call_id, "\u8fde\u63a5\u5df2\u65ad\u5f00")
        self.app.processEvents()
        self.assertEqual(self.cm.state, "idle", "current call's failure must still end it")
        self.assertTrue(ended)

    def test_late_connect_failed_ignored_but_current_still_works(self):
        sent = self._start_incoming()

        self.cm._sig_connect_failed.emit("stale-call-id", "127.0.0.1:1")
        self.app.processEvents()
        self.assertEqual(self.cm.state, "incoming", "late failure must be ignored")
        self.assertEqual(sent, [], "nothing may be sent for a stale call")

        self.cm._sig_connect_failed.emit(self.cm._call_id, "127.0.0.1:1")
        self.app.processEvents()
        self.assertEqual(self.cm.state, "idle")
        self.assertEqual([pkt.type for _, pkt in sent], ["call_failed"])

    # ------------------------------------------------ fix 3: accept cancel

    def test_accept_thread_exits_after_cancel(self):
        """After the caller cancels, the accept loop must leave promptly (short
        accept timeouts) instead of blocking in accept() for the ring timeout."""
        self._start_outgoing()
        self.assertTrue(self._wait(lambda: self._accept_thread_alive()))

        self.cm.hangup()
        self.assertEqual(self.cm.state, "idle")
        self.assertTrue(
            self._wait(lambda: not self._accept_thread_alive()),
            "accept thread must exit after cancel",
        )

    # ------------------------------------------------ fix 2: sender restart

    def test_sender_thread_joined_and_restartable_after_teardown(self):
        from localchat.call import CallManager

        self.cm._start_send_thread()
        first = self.cm._send_thread
        self.assertIsNotNone(first)
        self.assertTrue(first.is_alive())

        self.cm._shutdown_engines()
        self.assertTrue(
            self._wait(lambda: not first.is_alive()),
            "teardown must join the sender thread",
        )
        self.assertIsNone(self.cm._send_thread)

        # the next call must get a fresh sender (the old isAlive guard would
        # have refused forever while a stale thread lingered)
        self.cm._start_send_thread()
        second = self.cm._send_thread
        try:
            self.assertIsNotNone(second)
            self.assertIsNot(second, first)
        finally:
            self.cm._shutdown_engines()

    # ---------------------------------------------------------- fix 4: init

    def test_media_port_initialized(self):
        from localchat.call import CallManager

        self.assertEqual(CallManager()._media_port, 0)

    # ------------------------------------- media frame replay guard (fix 5)

    def test_media_replay_frame_terminates_connection(self):
        """A frame replayed WITHIN the same media connection must end the
        media read loop (nonce LRU guard) without delivering the frame;
        fresh frames before it must still flow."""
        from localchat.call import CH_AUDIO, build_frame
        from localchat.crypto import aes_gcm_encrypt, random_bytes

        key = random_bytes(32)
        cli, media = _loopback_socket()
        self.cleanup.append(cli.close)
        self.cleanup.append(media.close)
        self.cm._call_id = "call-R"
        self.cm._media_key = key

        audio = []
        self.cm.remote_audio.connect(lambda pcm: audio.append(bytes(pcm)))
        ended = []
        self.cm._sig_media_ended.connect(lambda cid, r: ended.append(cid))

        reader = threading.Thread(
            target=self.cm._media_read_loop, args=(media, "call-R"), daemon=True
        )
        reader.start()

        frames = [
            build_frame(CH_AUDIO, aes_gcm_encrypt(key, b"\x00" * 640))
            for _ in range(2)
        ]
        cli.sendall(frames[0])
        cli.sendall(frames[1])
        self.assertTrue(self._wait(lambda: len(audio) == 2))

        cli.sendall(frames[0])  # replay of the FIRST frame's exact bytes
        self.assertTrue(
            self._wait(lambda: bool(ended)),
            "a replayed nonce must terminate the media read loop",
        )
        self.assertFalse(reader.is_alive())
        self.assertEqual(ended, ["call-R"])
        self.assertEqual(len(audio), 2, "the replayed frame must not be delivered")

    def test_nonce_replay_guard_evicts_oldest(self):
        """The guard remembers at most [capacity] nonces and forgets the
        oldest one (LRU), so it stays bounded for long calls."""
        from localchat.call import NonceReplayGuard

        guard = NonceReplayGuard(capacity=2)
        n1, n2, n3 = b"\x01" * 12, b"\x02" * 12, b"\x03" * 12
        self.assertFalse(guard.is_replay(n1))
        self.assertFalse(guard.is_replay(n2))
        self.assertTrue(guard.is_replay(n2), "duplicate within the window")
        self.assertFalse(guard.is_replay(n3), "insert evicts the oldest (n1)")
        self.assertFalse(guard.is_replay(n1), "evicted nonce is forgotten")
        self.assertTrue(guard.is_replay(n3))

    # ------------------------------------------ audio probe failure handoff

    def test_failed_audio_probe_still_hands_over_to_qt(self):
        """_start_qt_audio() runs from the sounddevice probe's completion
        report, so a RAISING probe must still report: otherwise the call ends
        up with no audio engine at all and every call is silent."""
        import sys
        import types

        fake_sd = types.ModuleType("sounddevice")
        previous = sys.modules.get("sounddevice")
        sys.modules["sounddevice"] = fake_sd
        try:
            self.cm._call_id = "call-A"
            self.cm._audio_started = True
            self.cm._sd_pick_device = lambda sd, want_input: (_ for _ in ()).throw(
                RuntimeError("PortAudio host API is broken")
            )
            started = []
            self.cm._start_qt_audio = lambda: started.append(True)

            self.cm._start_sd_audio("call-A")

            self.assertTrue(
                self._wait(lambda: started),
                "the Qt audio fallback must start even when the probe raises",
            )
            self.assertIsNone(self.cm._sd_input)
            self.assertIsNone(self.cm._sd_output)
        finally:
            if previous is None:
                sys.modules.pop("sounddevice", None)
            else:
                sys.modules["sounddevice"] = previous

    # ------------------------------------------------------------- utility

    def _wait(self, cond, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            self.app.processEvents()
            time.sleep(0.02)
        return cond()


if __name__ == "__main__":
    unittest.main()
