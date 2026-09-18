"""Voice calls + local call log: protocol round-trip, call outcomes, storage
and the direct-chat system rows.

Chinese literals are written as unicode escapes so this file stays pure-ASCII.
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPointF, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem

import localchat.network as network_module
from localchat.models import (
    CALL_RESULT_ANSWERED,
    CALL_RESULT_CANCELLED,
    CALL_RESULT_FAILED,
    CALL_RESULT_MISSED,
    CALL_RESULT_REJECTED,
    MEDIA_AUDIO,
    CallInfo,
    NetworkPacket,
    Peer,
)
from localchat.network import P2PListener, P2PManager
from localchat.storage import ChatStore, SavedCallLog
from localchat.view_model import ChatViewModel
from tests.fake_peer import install_identity, wait_until

GROUP_NAME = "\u6d4b\u8bd5\u7fa4"  # 测试群
GROUP_PASSWORD = "123456"
HOST_NAME = "Android\u4e3b\u673a"  # Android主机

DATA_DIR = os.path.join(tempfile.gettempdir(), "kilo", "lc_call_data")


def _fresh_db(name):
    path = os.path.join(tempfile.gettempdir(), "kilo", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    return path


class Recorder(P2PListener):
    def peers_changed(self, p2p):
        pass

    def messages_changed(self, p2p):
        pass


class _Peer:
    def __init__(self, pid, name, ip):
        self.id = pid
        self.name = name
        self.ip_address = ip


class _P2PStub:
    """Just enough of P2PManager for CallManager.start_call."""

    def __init__(self, my_id, my_name):
        self.my_id = my_id
        self.my_name = my_name
        self.peers = {}
        self.sent = []

    def send_targeted(self, pid, pkt):
        self.sent.append((pid, pkt))


class CallMediaWireTest(unittest.TestCase):
    """CallInfo.media must serialize byte-exactly like kotlinx.serialization
    (the same golden strings the Android CallMediaProtocolTest asserts)."""

    def test_audio_offer_wire_matches_android(self):
        call = CallInfo("c1", "a-1", "\u5f20\u4e09", "b-2", media_port=35001,
                        media=MEDIA_AUDIO)
        wire = NetworkPacket(type="call_offer", target_id="b-2", call=call).to_json()
        self.assertEqual(
            '{"type":"call_offer","targetId":"b-2","call":{"callId":"c1",'
            '"callerId":"a-1","callerName":"\u5f20\u4e09","calleeId":"b-2",'
            '"mediaPort":35001,"media":"audio"}}',
            wire,
        )

    def test_video_offer_omits_media(self):
        call = CallInfo("c1", "a-1", "A", "b-2", media_port=35001)
        wire = NetworkPacket(type="call_offer", target_id="b-2", call=call).to_json()
        parsed = json.loads(wire)
        self.assertNotIn("media", parsed["call"], "video offers omit the field")
        self.assertEqual(
            '{"type":"call_offer","targetId":"b-2","call":{"callId":"c1",'
            '"callerId":"a-1","callerName":"A","calleeId":"b-2","mediaPort":35001}}',
            wire,
        )

    def test_android_audio_offer_decodes(self):
        payload = (
            '{"type":"call_offer","targetId":"b-2","call":{"callId":"c9",'
            '"callerId":"a-1","callerName":"\\u674e\\u56db","calleeId":"b-2",'
            '"mediaPort":42001,"media":"audio"}}'
        )
        pkt = NetworkPacket.from_json(payload)
        self.assertEqual(pkt.call.media, MEDIA_AUDIO)

    def test_android_video_offer_decodes_as_video(self):
        payload = (
            '{"type":"call_offer","targetId":"b-2","call":{"callId":"c9",'
            '"callerId":"a-1","callerName":"A","calleeId":"b-2","mediaPort":42001}}'
        )
        pkt = NetworkPacket.from_json(payload)
        self.assertEqual(pkt.call.media, "", "absent media means video")

    def test_unknown_media_kind_degrades_to_video(self):
        call = CallInfo.from_dict({
            "callId": "c1", "callerId": "a", "callerName": "A", "calleeId": "b",
            "media": "hologram",
        })
        self.assertEqual(call.media, "")


class CallOutcomeTest(unittest.TestCase):
    """Every ending path records a result in the call_finished payload."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from localchat.call import CallManager

        self.cm = CallManager()
        self.records = []
        self.cm.call_finished.connect(self.records.append)

    def tearDown(self):
        self.cm.hangup()
        self.app.processEvents()

    def _start_outgoing(self, media="video"):
        p2p = _P2PStub("me-1", "\u6211")
        p2p.peers["peer-1"] = _Peer("peer-1", "\u5bf9\u65b9", "127.0.0.1")
        self.cm.start_call(p2p, "peer-1", media=media)
        self.assertEqual(self.cm.state, "outgoing")
        return p2p, self.cm._call_id

    def _start_incoming(self, media=None):
        sent = []
        offer = NetworkPacket(
            type="call_offer",
            call=CallInfo(
                call_id="call-B",
                caller_id="caller-1",
                caller_name="\u547c\u53eb\u8005",
                callee_id="me-1",
                media_port=12345,
                media="" if media != MEDIA_AUDIO else MEDIA_AUDIO,
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

    def test_outgoing_cancel_is_cancelled(self):
        self._start_outgoing()
        self.cm.hangup()
        self.assertEqual(len(self.records), 1)
        rec = self.records[0]
        self.assertEqual(rec["result"], CALL_RESULT_CANCELLED)
        self.assertEqual(rec["direction"], "outgoing")
        self.assertEqual(rec["peer_id"], "peer-1")
        self.assertEqual(rec["media"], "video")
        self.assertEqual(rec["duration"], 0)

    def test_incoming_reject_is_rejected(self):
        self._start_incoming()
        self.cm.reject_call()
        self.assertEqual(len(self.records), 1)
        rec = self.records[0]
        self.assertEqual(rec["result"], CALL_RESULT_REJECTED)
        self.assertEqual(rec["direction"], "incoming")

    def test_incoming_peer_cancel_is_missed(self):
        self._start_incoming()
        self.cm._on_call_hangup(
            CallInfo("call-B", "caller-1", "\u547c\u53eb\u8005", "me-1")
        )
        self.assertEqual(len(self.records), 1)
        rec = self.records[0]
        self.assertEqual(rec["result"], CALL_RESULT_MISSED)
        self.assertEqual(rec["direction"], "incoming")

    def test_outgoing_reject_is_rejected(self):
        _, call_id = self._start_outgoing()
        self.cm._on_call_reject(CallInfo(call_id, "me-1", "\u6211", "peer-1"))
        self.assertEqual(self.records[0]["result"], CALL_RESULT_REJECTED)
        self.assertEqual(self.records[0]["direction"], "outgoing")

    def test_ring_timeout_is_missed(self):
        _, call_id = self._start_outgoing()
        self.cm._sig_ring_timeout.emit(call_id)
        self.app.processEvents()
        self.assertEqual(self.records[0]["result"], CALL_RESULT_MISSED)

    def test_media_failure_is_failed(self):
        _, call_id = self._start_outgoing()
        self.cm._on_call_failed(CallInfo(call_id, "me-1", "\u6211", "peer-1"))
        self.assertEqual(self.records[0]["result"], CALL_RESULT_FAILED)

    def test_audio_incoming_records_audio_media(self):
        self._start_incoming(media=MEDIA_AUDIO)
        self.assertEqual(self.cm.media, MEDIA_AUDIO)
        self.cm.reject_call()
        self.assertEqual(self.records[0]["media"], MEDIA_AUDIO)

    def test_offer_media_is_sent_only_for_audio(self):
        p2p, _ = self._start_outgoing(media=MEDIA_AUDIO)
        offer = p2p.sent[0][1]
        self.assertEqual(offer.call.media, MEDIA_AUDIO)
        self.assertIn('"media":"audio"', offer.to_json())
        self.cm.hangup()

        p2p2, _ = self._start_outgoing(media="video")
        offer2 = p2p2.sent[0][1]
        self.assertEqual(offer2.call.media, "")
        self.assertNotIn('"media"', offer2.to_json())


class CallAudioFlowTest(unittest.TestCase):
    """Two CallManagers on one machine run a full VOICE call: both sides learn
    the media kind from the offer and no camera capture starts."""

    PORT = 19221

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _wait_qt(self, cond, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            self.app.processEvents()
            time.sleep(0.01)
        return False

    def test_full_audio_call_flow(self):
        from localchat.call import (
            STATE_ACTIVE,
            STATE_IDLE,
            STATE_INCOMING,
            STATE_OUTGOING,
            CallManager,
        )

        install_identity()
        os.makedirs(DATA_DIR, exist_ok=True)
        host = P2PManager(Recorder(), port=self.PORT, password=GROUP_PASSWORD)
        host.initialize_as_host(HOST_NAME, GROUP_NAME)
        host.start_as_host()

        a = P2PManager(Recorder(), port=21131)
        a.initialize_as_client("\u547c\u53eb\u8005", GROUP_NAME, password=GROUP_PASSWORD)
        a.confirm_join("127.0.0.1", self.PORT)
        b = P2PManager(Recorder(), port=21132)
        b.initialize_as_client("\u88ab\u53eb\u8005", GROUP_NAME, password=GROUP_PASSWORD)
        b.confirm_join("127.0.0.1", self.PORT)
        self.assertTrue(wait_until(lambda: a.connection_result is not None and a.connection_result[0]))
        self.assertTrue(wait_until(lambda: b.connection_result is not None and b.connection_result[0]))
        self.assertTrue(wait_until(lambda: b.my_id in a.peers and a.my_id in b.peers))

        cm_a = CallManager()
        cm_b = CallManager()
        a.call_listener = cm_a._on_signal
        b.call_listener = cm_b._on_signal
        records_a = []
        records_b = []
        cm_a.call_finished.connect(records_a.append)
        cm_b.call_finished.connect(records_b.append)
        try:
            cm_a.start_call(a, b.my_id, media=MEDIA_AUDIO)
            self.assertTrue(self._wait_qt(lambda: cm_a.state == STATE_OUTGOING))
            self.assertTrue(self._wait_qt(lambda: cm_b.state == STATE_INCOMING))
            self.assertEqual(cm_b.media, MEDIA_AUDIO, "callee learns audio from the offer")

            cm_b.accept_call()
            self.assertTrue(
                self._wait_qt(lambda: cm_a.state == STATE_ACTIVE and cm_b.state == STATE_ACTIVE),
                "both sides must reach the active state",
            )
            time.sleep(0.4)
            self.app.processEvents()
            # voice calls never open the camera on either side
            self.assertIsNone(cm_a._capture_thread, "caller must not capture video")
            self.assertIsNone(cm_b._capture_thread, "callee must not capture video")

            cm_b.hangup()
            self.assertTrue(
                self._wait_qt(lambda: cm_a.state == STATE_IDLE and cm_b.state == STATE_IDLE)
            )
            self.assertTrue(self._wait_qt(lambda: len(records_a) == 1 and len(records_b) == 1))
            self.assertEqual(records_a[0]["result"], CALL_RESULT_ANSWERED)
            self.assertEqual(records_a[0]["direction"], "outgoing")
            self.assertEqual(records_a[0]["media"], MEDIA_AUDIO)
            self.assertEqual(records_b[0]["result"], CALL_RESULT_ANSWERED)
            self.assertEqual(records_b[0]["direction"], "incoming")
            self.assertGreaterEqual(records_a[0]["duration"], 0)
        finally:
            cm_a.hangup()
            cm_b.hangup()
            a.stop()
            b.stop()
            host.stop()


class CallLogStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = ChatStore(_fresh_db("lc_call_log.db"))

    def tearDown(self):
        self.store.close()

    def _entry(self, log_id, ts, result="answered", direction="outgoing",
               media="video", key="direct:peer-1", duration=3):
        return SavedCallLog(
            id=log_id,
            conversation_key=key,
            peer_id="peer-1",
            peer_name="\u5bf9\u65b9",
            direction=direction,
            result=result,
            media=media,
            start_time=ts,
            duration=duration,
        )

    def test_round_trip_oldest_first(self):
        self.store.add_call_log(self._entry("c2", 2000))
        self.store.add_call_log(self._entry("c1", 1000))
        logs = self.store.get_call_logs("direct:peer-1")
        self.assertEqual([r.id for r in logs], ["c1", "c2"])
        self.assertEqual(logs[0].result, "answered")
        self.assertEqual(logs[0].peer_name, "\u5bf9\u65b9")

    def test_conversations_are_isolated(self):
        self.store.add_call_log(self._entry("c1", 1000))
        self.store.add_call_log(self._entry("c2", 1500, key="direct:peer-2"))
        self.assertEqual(len(self.store.get_call_logs("direct:peer-1")), 1)
        self.assertEqual(len(self.store.get_call_logs("direct:peer-2")), 1)

    def test_cap_keeps_newest_200(self):
        for i in range(250):
            self.store.add_call_log(self._entry("c%d" % i, 1000 + i))
        logs = self.store.get_call_logs("direct:peer-1")
        self.assertEqual(len(logs), ChatStore.CALL_LOG_CAP)
        self.assertEqual(logs[-1].id, "c249")
        self.assertNotIn("c0", [r.id for r in logs])

    def test_move_and_delete(self):
        self.store.add_call_log(self._entry("c1", 1000))
        self.store.move_call_logs("direct:peer-1", "direct:real-1")
        self.assertEqual(self.store.get_call_logs("direct:peer-1"), [])
        self.assertEqual(len(self.store.get_call_logs("direct:real-1")), 1)
        self.store.delete_group("direct:real-1")
        self.assertEqual(self.store.get_call_logs("direct:real-1"), [])


class CallLogViewModelTest(unittest.TestCase):
    """The ViewModel persists finished calls and the direct-chat page renders
    them as system-style rows (and never as deletable bubbles)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        network_module.TCP_PORT = 10071
        self.vm = ChatViewModel(ChatStore(_fresh_db("lc_call_vm.db")), data_dir=DATA_DIR)
        self._vms = [self.vm]

    def tearDown(self):
        for vm in self._vms:
            vm.shutdown()
        self.app.processEvents()

    def _finish(self, peer_id="peer-1", result="missed", direction="incoming",
                media="video", duration=0, start=1700000000000):
        self.vm._on_call_finished({
            "call_id": "call-%s-%s" % (peer_id, result),
            "peer_id": peer_id,
            "peer_name": "\u5bf9\u65b9",
            "role": "callee" if direction == "incoming" else "caller",
            "direction": direction,
            "media": media,
            "result": result,
            "started_at": start,
            "connected_at": start if result == "answered" else 0,
            "ended_at": start + duration * 1000,
            "duration": duration,
        })

    def test_finished_call_is_persisted_and_signalled(self):
        seen = []
        self.vm.call_logs_changed.connect(seen.append)
        self._finish()
        self.app.processEvents()
        logs = self.vm.direct_call_logs("peer-1")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].result, "missed")
        self.assertEqual(logs[0].direction, "incoming")
        self.assertEqual(seen, ["peer-1"])

    def test_direct_page_renders_system_row_and_redials(self):
        from localchat.ui.chat_page import CallLogEntry, MessageDelegate, call_log_row
        from localchat.ui.direct_chat_page import DirectChatPage

        self._finish()
        self.app.processEvents()
        page = DirectChatPage(self.vm, lambda: None)
        page.open_chat(Peer("peer-1", "\u5bf9\u65b9", "127.0.0.1", 9))
        self.app.processEvents()

        rows = []
        for i in range(page.model.rowCount()):
            index = page.model.index(i, 0)
            data = index.data(Qt.ItemDataRole.UserRole + 1)
            if isinstance(data, CallLogEntry):
                rows.append((index, data))
        self.assertEqual(len(rows), 1, "the call log must render as a system row")
        index, entry = rows[0]
        self.assertFalse(entry.is_from_me)
        self.assertIsNone(entry.file_info, "a call row is not a message bubble")

        calls = []
        self.vm.start_direct_call = (
            lambda peer_id, media="video": calls.append((peer_id, media))
        )
        delegate = page.list_view.itemDelegate()
        option = QStyleOptionViewItem()
        option.rect = page.list_view.visualRect(index)
        event = QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(5.0, 5.0),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        handled = delegate.editorEvent(event, page.model, option, index)
        self.assertTrue(handled)
        self.assertEqual(calls, [("peer-1", "video")])
        page.deleteLater()

    def test_voice_button_starts_audio_call(self):
        from localchat.ui.direct_chat_page import DirectChatPage

        page = DirectChatPage(self.vm, lambda: None)
        page._peer_id = "peer-1"
        calls = []
        self.vm.start_direct_call = (
            lambda peer_id, media="video": calls.append((peer_id, media))
        )
        page.voice_btn.click()
        self.assertEqual(calls, [("peer-1", "audio")])
        page.deleteLater()

    def test_missed_row_text(self):
        from localchat.ui.chat_page import call_log_text, call_log_row

        self._finish(media="audio")
        self.app.processEvents()
        log = self.vm.direct_call_logs("peer-1")[0]
        text = call_log_text(call_log_row(log))
        self.assertEqual(text, "\u672a\u63a5\u6765\u7535\uff08\u8bed\u97f3\uff09")


if __name__ == "__main__":
    unittest.main()