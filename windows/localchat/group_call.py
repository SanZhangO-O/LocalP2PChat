"""Group multi-party voice conference (audio only, star topology).

Topology (both platforms implement the same wire behavior):
- The initiator becomes the MEETING HOST. It binds an ephemeral media port
  (like the 1:1 caller) and sends a targeted ``group_call_invite`` to every
  online group member via the host relay.
- An invited member rings ("ring - join"): accepting sends ``group_call_join``
  to the meeting host, then dials the host's media port. The media link is a
  plain TCP connection with the SAME security chain as a 1:1 call media
  channel: identity-signed handshake, then a ``call_media_hello``
  version/participant check (CallInfo.callId carries the meetingId and
  CallInfo.meetingId mirrors it), then AES-256-GCM frames.
- Frames reuse the 1:1 media format exactly:
      [1 byte channel][4 bytes big-endian length][12B nonce || ct||tag]
  channel 1 = audio (PCM16 mono 16 kHz). Each member link is a dedicated TCP
  connection, so routing is per-socket; the meetingId binds every link and
  packet to its meeting (a second meeting can never hijack a link).
- The host MIXES: it sums its own microphone with every member's inbound
  audio and sends each member the mix MINUS that member's own audio
  (mix-minus-self, so nobody hears themselves). Members simply play the
  host's mix and send their microphone.

Signaling packets (all targeted, routed by the group host relay; the packet
actor must be the authenticated sender — see network._route_group_call_packet):
- group_call_invite {groupId, call{callId=meetingId, callerId=hostId,
  callerName, calleeId=memberId, mediaPort, meetingId}}
- group_call_join   {groupId, call{callId=meetingId, callerId=memberId,
  calleeId=hostId, meetingId}}
- group_call_leave  {groupId, call{callId=meetingId, callerId=actorId,
  calleeId=otherId, meetingId}}  (member leaves / host ends the meeting)
- group_call_sync   {groupId, call{...}, members=[participant peers]}
  (host -> members after every membership change)

Old peers ignore these unknown packet types (both platforms' group read
loops dispatch known types only), so chat / files / 1:1 calls are unaffected.

Threading mirrors call.py: media sockets live on worker threads; all state
transitions and Qt audio objects stay on the GUI thread via queued signals.
Audio capture/playback follows the same sounddevice-first / QtMultimedia-
fallback scheme as CallManager (PCM16 mono 16 kHz everywhere).
"""

import os
import queue
import socket
import threading
import time
import uuid

import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtMultimedia import (
    QAudio,
    QAudioFormat,
    QAudioSink,
    QAudioSource,
    QMediaDevices,
)

from .aec import Aec
from .call import (
    AUDIO_CHUNK_MS,
    AUDIO_SAMPLE_RATE,
    CH_AUDIO,
    CONNECT_TIMEOUT,
    MAX_FRAME_WIRE_LEN,
    MEDIA_READ_TIMEOUT,
    NonceReplayGuard,
    _read_exact,
    build_frame,
    run_catching_close,
)
from .crypto import GCM_NONCE_LEN, aes_gcm_decrypt, aes_gcm_encrypt
from .models import CallInfo, NetworkPacket, Peer
from .network import _read_raw_line, make_wire
from .securewire import DeviceIdentity, Handshake, Protocol

logger = __import__("logging").getLogger(__name__)

# One 20 ms PCM16 mono frame.
AUDIO_FRAME_BYTES = AUDIO_SAMPLE_RATE * 2 // 50  # 640
# Per-link inbound PCM buffer cap (bytes): ~400 ms; older audio is dropped so
# a burst can never inflate conference latency forever.
INBUF_MAX = AUDIO_FRAME_BYTES * 20
# A member must connect its media link within this window after its
# group_call_join (the join alone must never activate a link).
JOINER_TTL = 30.0
# An unanswered invite stops ringing after this long (member side).
RING_TIMEOUT = 45.0
# Member uplink pacing (mirrors the 1:1 call audio sender).
AUDIO_SEND_QUEUE = 64
AUDIO_KEEPALIVE = 3.0
AUDIO_PREROLL_BYTES = AUDIO_SAMPLE_RATE * 2 * 4  # ~80 ms
AUDIO_PENDING_MAX = AUDIO_SAMPLE_RATE * 2 * 2  # 2 s, drop-oldest beyond
# Smallest ciphertext frame on the wire: GCM nonce + tag.
MIN_FRAME_WIRE_LEN = GCM_NONCE_LEN + 16

# Audio endpoints that are virtual or loopback mixers and must never be used
# as the meeting's microphone/speaker (parity with CallManager).
_VIRTUAL_AUDIO_HINTS = (
    "steam", "virtual", "wave", "stereo mix", "loopback", "混音", "立体声混音",
)

# Conference states surfaced to the UI (same vocabulary as 1:1 calls).
STATE_IDLE = "idle"
STATE_OUTGOING = "outgoing"
STATE_INCOMING = "incoming"
STATE_ACTIVE = "active"


def mix_pcm16(chunks, frame_bytes: int = AUDIO_FRAME_BYTES) -> bytes:
    """Saturating sum of PCM16 mono buffers (voice-grade conference mix).
    Pure function so the mixing rule is unit-testable on both platforms
    (Android parity: GroupCallManager.mixPcm). Every chunk is padded/truncated
    to [frame_bytes]; an empty list yields silence."""
    acc = np.zeros(frame_bytes // 2, dtype=np.int32)
    for chunk in chunks:
        if not chunk:
            continue
        data = chunk[: frame_bytes // 2 * 2]
        arr = np.frombuffer(data, dtype=np.int16).astype(np.int32)
        acc[: arr.size] += arr
    return np.clip(acc, -32768, 32767).astype(np.int16).tobytes()


def _sd_pick_device(sd, want_input: bool):
    """Pick a sounddevice device index (OS default first, then the first
    non-virtual device). Mirrors CallManager._sd_pick_device."""
    key = "LOCALCHAT_AUDIO_IN" if want_input else "LOCALCHAT_AUDIO_OUT"
    override = os.environ.get(key, "")
    if override:
        try:
            idx = int(override)
            info = sd.query_devices(idx)
            channels = (
                info["max_input_channels"] if want_input else info["max_output_channels"]
            )
            if channels > 0:
                return idx
        except Exception:
            pass
    default = sd.default.device
    idx = default[0 if want_input else 1] if isinstance(default, (list, tuple)) else None
    if idx is None and isinstance(default, int):
        idx = default
    if idx is not None:
        try:
            info = sd.query_devices(idx)
            channels = (
                info["max_input_channels"] if want_input else info["max_output_channels"]
            )
            if channels > 0 and not any(
                h in info["name"].lower() for h in _VIRTUAL_AUDIO_HINTS
            ):
                return idx
        except Exception:
            pass
    try:
        devices = list(sd.query_devices())
    except Exception:
        # a dead host API / no devices must degrade to "no sounddevice
        # candidate" instead of raising out of the audio probe
        return None
    for i, info in enumerate(devices):
        channels = (
            info["max_input_channels"] if want_input else info["max_output_channels"]
        )
        if channels > 0 and not any(
            h in info["name"].lower() for h in _VIRTUAL_AUDIO_HINTS
        ):
            return i
    return None


class _Link:
    """One member media link on the meeting host (socket + inbound buffer)."""

    def __init__(self, member_id: str, member_name: str, sock, key: bytes):
        self.member_id = member_id
        self.member_name = member_name
        self.sock = sock
        self.key = key
        self.send_lock = threading.Lock()
        self.in_lock = threading.Lock()
        self.in_buf = bytearray()

    def push_pcm(self, pcm: bytes) -> None:
        with self.in_lock:
            self.in_buf.extend(pcm)
            if len(self.in_buf) > INBUF_MAX:
                del self.in_buf[: len(self.in_buf) - INBUF_MAX]

    def take_pcm(self, frame_bytes: int) -> bytes:
        """Consume exactly [frame_bytes] of inbound PCM (zero-padded when the
        member sent less — the mix clock never waits for a silent peer)."""
        with self.in_lock:
            missing = frame_bytes - len(self.in_buf)
            if missing > 0:
                self.in_buf.extend(b"\x00" * missing)
            chunk = bytes(self.in_buf[:frame_bytes])
            del self.in_buf[:frame_bytes]
        return chunk


class GroupCallManager(QObject):
    """One meeting at a time, per device. Owns conference signaling, the
    media links and the capture/mix/playback engines; created on the GUI
    thread. Deliberately separate from CallManager so the 1:1 call path is
    untouched."""

    # (state, title, detail) — state in idle/outgoing/incoming/active.
    state_changed = pyqtSignal(str, str, str)
    incoming_invite = pyqtSignal(str, str)  # (meeting_id, host_name)
    participants_changed = pyqtSignal(object)  # list of {id, name, self}
    call_ended = pyqtSignal(str)  # reason
    call_error = pyqtSignal(str)  # message

    # Cross-thread plumbing (network/media threads -> GUI thread). Every
    # meeting-scoped signal carries its meeting_id so a late event from a
    # PREVIOUS meeting is dropped instead of hitting the current one.
    _sig_packet = pyqtSignal(object, object)  # (p2p, NetworkPacket)
    _sig_link_ready = pyqtSignal(str, str, object, object)  # (meeting, member, sock, key)
    _sig_link_died = pyqtSignal(str, str)  # (meeting, member_id)
    _sig_member_connected = pyqtSignal(str, object, object)  # (meeting, sock, key|err)
    _sig_member_gone = pyqtSignal(str, str)  # (meeting, reason)
    # sounddevice probe finished off the GUI thread (see _start_sd_audio):
    _sig_sd_ready = pyqtSignal(str, object, int, int, object, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._state = STATE_IDLE
        self._role = ""  # "host" | "member"
        self._p2p = None
        self._group_id = ""
        self._my_id = ""
        self._my_name = ""
        self._meeting_id = ""
        self._host_id = ""
        self._host_name = ""
        self._host_ip = ""
        self._media_port = 0
        self._media_server = None
        # Host: expected joiners (member id -> monotonic deadline) declared by
        # group_call_join; only a hello from an expected member activates a
        # link (the media port is TCP-public, like the 1:1 caller's).
        self._expected = {}
        # Host: live member links.
        self._links = {}
        self._links_lock = threading.Lock()
        # Member: the single uplink to the meeting host + participant cache.
        self._sock = None
        self._key = None
        self._participants_cache = []
        # Audio engines (mirrors CallManager: sounddevice probe + Qt fallback,
        # GUI-thread QTimers, software AEC, pending playback buffer).
        self._muted = False
        self._audio_started = False
        self._aec = None
        self._audio_source = None
        self._audio_in_dev = None
        self._audio_sink = None
        self._audio_out_dev = None
        self._audio_timer_in = None
        self._audio_timer_out = None
        self._audio_pending = bytearray()
        self._audio_pending_lock = threading.Lock()
        self._audio_prerolled = False
        self._audio_started_at = 0.0
        self._sd_input = None
        self._sd_output = None
        self._sd_input_rate = AUDIO_SAMPLE_RATE
        self._sd_output_rate = AUDIO_SAMPLE_RATE
        self._sd_input_channels = 1
        self._sd_cb_count = 0
        # Host mixing inputs: own microphone ring buffer.
        self._mic_buf = bytearray()
        self._mic_lock = threading.Lock()
        # Shared engine stop flag (mixer + member sender + read loops).
        self._media_stop = threading.Event()
        self._mixer_thread = None
        # Member uplink: bounded queue drained by a dedicated sender thread.
        self._send_q = queue.Queue(maxsize=AUDIO_SEND_QUEUE)
        self._send_thread = None
        self._last_media_sent_at = 0.0
        # GUI-thread ring timer (member invite expiry) — stopped in teardown.
        self._ring_timer = QTimer(self)
        self._ring_timer.setSingleShot(True)
        self._ring_timer.timeout.connect(self._expire_ring)
        # Optional busy probe injected by the ViewModel: returns True while a
        # 1:1 call is active (conference and 1:1 call are mutually exclusive
        # because they share the microphone).
        self.busy_check = None

        self._sig_packet.connect(self._on_packet)
        self._sig_link_ready.connect(self._on_link_ready)
        self._sig_link_died.connect(self._on_link_died)
        self._sig_member_connected.connect(self._on_member_connected)
        self._sig_member_gone.connect(self._on_member_gone)
        self._sig_sd_ready.connect(self._on_sd_audio_ready)

    # ------------------------------------------------------------- public API

    @property
    def state(self) -> str:
        return self._state

    @property
    def meeting_id(self) -> str:
        return self._meeting_id

    @property
    def role(self) -> str:
        return self._role

    def participants(self):
        """Snapshot of the conference participants: list of dicts
        {id, name, self}. Host: live links + self. Member: last sync."""
        with self._lock:
            if self._role == "host":
                out = [{
                    "id": self._my_id,
                    "name": self._my_name,
                    "self": True,
                }]
                with self._links_lock:
                    for link in self._links.values():
                        out.append({
                            "id": link.member_id,
                            "name": link.member_name,
                            "self": False,
                        })
                return out
            return list(self._participants_cache)

    def attach(self, p2p) -> None:
        """Register the conference signaling listener on a group connection."""
        p2p.group_call_listener = self._on_signal

    def detach(self, p2p) -> None:
        if getattr(p2p, "group_call_listener", None) is self._on_signal:
            p2p.group_call_listener = None

    def _on_signal(self, p2p, packet) -> None:
        """Network-thread entry: hop to the GUI thread."""
        self._sig_packet.emit(p2p, packet)

    def start_meeting(self, p2p) -> None:
        """Start a conference in the active group: this device becomes the
        meeting host (mixer) and invites every online member."""
        with self._lock:
            if self._state != STATE_IDLE:
                self.call_error.emit("已有进行中的语音会议")
                return
            if self.busy_check is not None and self.busy_check():
                self.call_error.emit("通话进行中，无法发起语音会议")
                return
            try:
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("0.0.0.0", 0))
                server.listen(8)
            except OSError:
                self.call_error.emit("无法开启语音会议端口")
                return
            self._p2p = p2p
            self._group_id = p2p.current_group_id
            self._role = "host"
            self._my_id = p2p.my_id
            self._my_name = p2p.my_name
            self._meeting_id = str(uuid.uuid4())
            self._host_id = self._my_id
            self._host_name = self._my_name
            self._media_port = server.getsockname()[1]
            self._media_server = server
            self._expected = {}
            self._state = STATE_OUTGOING
        self.state_changed.emit(STATE_OUTGOING, self._my_name, "")
        self.participants_changed.emit(self.participants())
        for peer in list(p2p.peers.values()):
            if not peer.ip_address:
                continue
            p2p.send_targeted(
                peer.id,
                self._group_packet("group_call_invite", peer.id, self._my_id),
            )
        threading.Thread(
            target=self._accept_loop, args=(server, self._meeting_id), daemon=True
        ).start()

    def accept_invite(self) -> None:
        """Join the meeting we were invited to: announce, then dial the
        meeting host's media port (identity handshake + call_media_hello)."""
        with self._lock:
            if self._state != STATE_INCOMING:
                return
            meeting_id = self._meeting_id
            host_id = self._host_id
            host_ip = self._host_ip
            media_port = self._media_port
            my_id = self._my_id
            p2p = self._p2p
        self._ring_timer.stop()
        if p2p is not None and host_id:
            p2p.send_targeted(
                host_id, self._group_packet("group_call_join", my_id, host_id)
            )
        if not host_ip or media_port <= 0:
            self._leave_local("无法加入语音会议：地址无效")
            return

        def run():
            sock = None
            try:
                sock = socket.create_connection(
                    (host_ip, media_port), timeout=CONNECT_TIMEOUT
                )
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(10)
                wire = make_wire(sock)
                Handshake.initiate_direct(
                    wire,
                    expected_peer_id=(
                        host_id if host_id and not host_id.startswith("ip:") else None
                    ),
                    on_identity_mismatch=lambda: self.call_error.emit(
                        "安全警告：会议主持人身份验证失败"
                    ),
                )
                # Bind this TCP connection to the meeting (the media port is
                # TCP-public: only a peer that knows the meetingId — from the
                # encrypted invite — may turn this connection into a link).
                with self._lock:
                    host_name = self._host_name
                wire.send_packet(
                    NetworkPacket(
                        type="call_media_hello",
                        call=CallInfo(
                            call_id=meeting_id,
                            caller_id=host_id,
                            caller_name=host_name,
                            callee_id=my_id,
                            meeting_id=meeting_id,
                        ),
                    )
                )
            except OSError as e:
                run_catching_close(sock)
                self._sig_member_connected.emit(meeting_id, None, f"{e}")
                return
            except Exception:
                run_catching_close(sock)
                self._sig_member_connected.emit(meeting_id, None, "安全握手失败")
                return
            self._sig_member_connected.emit(meeting_id, sock, wire.session_key)

        threading.Thread(target=run, daemon=True).start()

    def decline_invite(self) -> None:
        """Decline the ringing invite (no packet: the host only activates
        links whose member announced AND connected)."""
        with self._lock:
            if self._state != STATE_INCOMING:
                return
        self._ring_timer.stop()
        self._leave_local("已拒绝语音会议邀请")

    def leave_meeting(self) -> None:
        """Leave the meeting from the UI (any state; host: ends the meeting)."""
        with self._lock:
            state = self._state
            role = self._role
            p2p = self._p2p
            meeting_id = self._meeting_id
            my_id = self._my_id
            host_id = self._host_id
            member_ids = []
            if role == "host":
                with self._links_lock:
                    member_ids = list(self._links.keys())
                member_ids.extend(
                    mid for mid in self._expected if mid not in member_ids
                )
        self._ring_timer.stop()
        if state == STATE_IDLE:
            return
        if p2p is not None and meeting_id:
            if role == "member" and host_id:
                p2p.send_targeted(
                    host_id, self._group_packet("group_call_leave", my_id, host_id)
                )
            elif role == "host":
                # the meeting is over: tell every participant (joined or still
                # ringing) so they tear down immediately
                for mid in dict.fromkeys(member_ids):
                    p2p.send_targeted(
                        mid, self._group_packet("group_call_leave", my_id, mid)
                    )
        self._leave_local("已挂断")

    # Alias so call-style UI code reads the same.
    def hangup(self) -> None:
        self.leave_meeting()

    def set_audio_muted(self, muted: bool) -> None:
        self._muted = bool(muted)

    def end_if_on(self, p2p, reason: str) -> None:
        """Tear the meeting down when it rides the given group connection
        (connection lost / leaving the group): signaling is dead, media
        follows."""
        with self._lock:
            if self._state == STATE_IDLE or self._p2p is not p2p:
                return
        self._ring_timer.stop()
        self._leave_local(reason)

    # ------------------------------------------------------- signal handlers

    def _on_packet(self, p2p, packet) -> None:
        if packet.call is None:
            return
        call = packet.call
        if packet.type == "group_call_invite":
            self._on_invite(p2p, packet, call)
        elif packet.type == "group_call_join":
            self._on_join(p2p, packet, call)
        elif packet.type == "group_call_leave":
            self._on_leave_packet(p2p, packet, call)
        elif packet.type == "group_call_sync":
            self._on_sync(p2p, packet, call)

    def _group_packet(self, pkt_type: str, actor_id: str, other_id: str) -> NetworkPacket:
        """Build a conference signaling packet. The actor (caller) is always
        the sender; the other party is the callee (the invited member for
        invites/sync, the meeting host for joins, either side for leaves)."""
        with self._lock:
            meeting_id = self._meeting_id
            media_port = self._media_port
            group_id = self._group_id
        call = CallInfo(
            call_id=meeting_id,
            caller_id=actor_id,
            caller_name=self._my_name if actor_id == self._my_id else self._host_name,
            callee_id=other_id,
            media_port=media_port if pkt_type == "group_call_invite" else 0,
            meeting_id=meeting_id,
        )
        pkt = NetworkPacket(type=pkt_type, group_id=group_id, call=call)
        if pkt_type == "group_call_sync":
            with self._links_lock:
                links = list(self._links.values())
            members = [Peer(self._my_id, self._my_name, "", 0)]
            for link in links:
                members.append(Peer(link.member_id, link.member_name, "", 0))
            pkt.members = members
        return pkt

    def _on_invite(self, p2p, packet, call) -> None:
        """An invited member: ring (busy conferences/1:1 calls never ring)."""
        if call.callee_id != self._my_id:
            return
        if not call.meeting_id or not call.caller_id or not call.media_port:
            return
        if packet.group_id and packet.group_id != p2p.current_group_id:
            return
        if self._state != STATE_IDLE:
            return
        if self.busy_check is not None and self.busy_check():
            return
        peer = p2p.peers.get(call.caller_id)
        host_ip = peer.ip_address if peer is not None else ""
        if not host_ip:
            return
        with self._lock:
            if self._state != STATE_IDLE:
                return
            self._p2p = p2p
            self._group_id = p2p.current_group_id
            self._role = "member"
            self._my_id = p2p.my_id
            self._my_name = p2p.my_name
            self._meeting_id = call.meeting_id
            self._host_id = call.caller_id
            self._host_name = call.caller_name
            self._host_ip = host_ip
            self._media_port = call.media_port
            self._state = STATE_INCOMING
            self._participants_cache = [
                {"id": call.caller_id, "name": call.caller_name, "self": False}
            ]
        self.state_changed.emit(STATE_INCOMING, call.caller_name, "")
        self.participants_changed.emit(self.participants())
        self.incoming_invite.emit(call.meeting_id, call.caller_name)
        self._ring_timer.start(int(RING_TIMEOUT * 1000))

    def _expire_ring(self) -> None:
        with self._lock:
            if self._state != STATE_INCOMING:
                return
        self._leave_local("未响应语音会议邀请")

    def _on_join(self, p2p, packet, call) -> None:
        """The meeting host records the announced member (host role only)."""
        if self._role != "host" or self._state == STATE_IDLE:
            return
        if call.callee_id != self._my_id or call.caller_id == self._my_id:
            return
        if not call.meeting_id or call.meeting_id != self._meeting_id:
            return
        if packet.group_id and packet.group_id != self._group_id:
            return
        if call.caller_id not in p2p.peers:
            return
        with self._lock:
            self._expected[call.caller_id] = time.monotonic() + JOINER_TTL

    def _on_leave_packet(self, p2p, packet, call) -> None:
        if not call.meeting_id or call.meeting_id != self._meeting_id:
            return
        if packet.group_id and self._group_id and packet.group_id != self._group_id:
            return
        if self._state == STATE_IDLE:
            return
        if self._role == "host":
            # a member left: drop its link (joiners that never connected are
            # dropped from the expected set too)
            with self._lock:
                self._expected.pop(call.caller_id, None)
            with self._links_lock:
                link = self._links.pop(call.caller_id, None)
            if link is not None:
                run_catching_close(link.sock)
                self._broadcast_sync()
                self.participants_changed.emit(self.participants())
        else:
            # only the meeting host may end the meeting for us
            if call.caller_id != self._host_id:
                return
            self._ring_timer.stop()
            self._leave_local("语音会议已结束")

    def _on_sync(self, p2p, packet, call) -> None:
        """Member: refresh the participant list from the meeting host."""
        if self._role != "member" or self._state != STATE_ACTIVE:
            return
        if call.caller_id != self._host_id:
            return
        if not packet.members:
            return
        out = []
        for p in packet.members:
            out.append({
                "id": p.id,
                "name": p.name,
                "self": p.id == self._my_id,
            })
        self._participants_cache = out
        self.participants_changed.emit(list(out))

    # ------------------------------------------------------ host media links

    def _accept_loop(self, server, meeting_id: str) -> None:
        server.settimeout(0.25)
        while True:
            with self._lock:
                alive = self._meeting_id == meeting_id and self._role == "host"
            if not alive:
                return
            try:
                sock, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._host_link_thread, args=(sock, meeting_id), daemon=True
            ).start()

    def _host_link_thread(self, sock, meeting_id: str) -> None:
        """Handshake + call_media_hello validation for ONE joining member
        (worker thread; mirrors the 1:1 accept_loop security chain)."""
        try:
            sock.settimeout(10)
            start_line = _read_raw_line(sock)
            start = None
            if start_line is not None:
                try:
                    start = NetworkPacket.from_json(start_line)
                except Exception:
                    start = None
            if start is None or start.type != Protocol.HS_START:
                raise OSError("no media handshake")
            wire = make_wire(sock)
            # The hello (below) declares which member this is, so the TOFU
            # check runs AFTER the handshake against that declared id — same
            # deferred-binding rule as the Android 1:1 accept loop. An
            # unexpected member is rejected before its identity is bound.
            secured = Handshake.accept_direct(wire, start, expected_peer_id=None)
            if secured is None:
                raise OSError("media handshake rejected")
            hello = wire.recv_packet()
            hello_call = hello.call if hello is not None else None
            if (
                hello is None
                or hello.type != "call_media_hello"
                or hello_call is None
                or hello_call.call_id != meeting_id
                or (hello_call.meeting_id and hello_call.meeting_id != meeting_id)
            ):
                raise OSError("call_media_hello mismatch")
            member_id = hello_call.callee_id
            with self._lock:
                deadline = self._expected.get(member_id, 0.0)
                valid = (
                    self._meeting_id == meeting_id
                    and self._role == "host"
                    and bool(member_id)
                    and member_id != self._my_id
                    and deadline >= time.monotonic()
                )
            if not valid:
                raise OSError("member not expected")
            if not member_id.startswith("ip:") and not DeviceIdentity.check_peer(
                member_id, secured.peer_ident
            ):
                self.call_error.emit("安全警告：会议成员身份验证失败")
                raise OSError("member identity changed")
        except Exception:
            run_catching_close(sock)
            return
        self._sig_link_ready.emit(meeting_id, member_id, sock, wire.session_key)

    def _on_link_ready(self, meeting_id: str, member_id: str, sock, key) -> None:
        if meeting_id != self._meeting_id or self._role != "host":
            run_catching_close(sock)
            return
        peer = self._p2p.peers.get(member_id) if self._p2p is not None else None
        member_name = peer.name if peer is not None else member_id
        with self._links_lock:
            old = self._links.get(member_id)
            self._links[member_id] = _Link(member_id, member_name, sock, key)
        if old is not None:
            run_catching_close(old.sock)
        with self._lock:
            self._expected.pop(member_id, None)
            was_outgoing = self._state == STATE_OUTGOING
            if self._state != STATE_ACTIVE:
                self._state = STATE_ACTIVE
        if was_outgoing:
            self.state_changed.emit(STATE_ACTIVE, self._host_name, "")
        self._ensure_engines()
        self._start_link_read_loop(meeting_id, member_id, sock, key)
        self._broadcast_sync()
        self.participants_changed.emit(self.participants())

    def _start_link_read_loop(self, meeting_id: str, member_id: str, sock, key) -> None:
        threading.Thread(
            target=self._link_read_loop,
            args=(meeting_id, member_id, sock, key),
            daemon=True,
            name="group-call-link",
        ).start()

    def _link_read_loop(self, meeting_id: str, member_id: str, sock, key) -> None:
        """Decrypt one member's uplink into its per-link buffer."""
        replay = NonceReplayGuard()
        try:
            sock.settimeout(MEDIA_READ_TIMEOUT)
            while True:
                with self._links_lock:
                    link = self._links.get(member_id)
                if link is None or link.sock is not sock:
                    return  # replaced or removed
                header = _read_exact(sock, 5)
                if header is None:
                    return
                channel = header[0]
                length = int.from_bytes(header[1:5], "big")
                if length < MIN_FRAME_WIRE_LEN or length > MAX_FRAME_WIRE_LEN:
                    return
                blob = _read_exact(sock, length)
                if blob is None:
                    return
                if replay.is_replay(blob[:GCM_NONCE_LEN]):
                    return
                try:
                    payload = aes_gcm_decrypt(key, blob)
                except Exception:
                    return
                if channel != CH_AUDIO:
                    continue  # conferences are audio-only
                link.push_pcm(bytes(payload))
        except (socket.timeout, OSError):
            pass
        finally:
            self._sig_link_died.emit(meeting_id, member_id)

    def _on_link_died(self, meeting_id: str, member_id: str) -> None:
        if meeting_id != self._meeting_id or self._role != "host":
            return
        with self._links_lock:
            link = self._links.pop(member_id, None)
        if link is None:
            return
        run_catching_close(link.sock)
        self._broadcast_sync()
        self.participants_changed.emit(self.participants())

    def _broadcast_sync(self) -> None:
        """Host: push the participant list to every member (targeted)."""
        p2p = self._p2p
        if p2p is None or self._role != "host":
            return
        with self._links_lock:
            member_ids = [link.member_id for link in self._links.values()]
        for mid in member_ids:
            p2p.send_targeted(
                mid, self._group_packet("group_call_sync", self._my_id, mid)
            )

    # ---------------------------------------------------- member media link

    def _on_member_connected(self, meeting_id: str, sock, key_or_error) -> None:
        if meeting_id != self._meeting_id or self._role != "member":
            if sock is not None:
                run_catching_close(sock)
            return
        if sock is None:
            self._leave_local(f"加入语音会议失败（{key_or_error}）")
            return
        with self._lock:
            if self._state not in (STATE_INCOMING, STATE_ACTIVE):
                run_catching_close(sock)
                return
            self._sock = sock
            self._key = key_or_error
            self._state = STATE_ACTIVE
        self.state_changed.emit(STATE_ACTIVE, self._host_name, "")
        self._ensure_engines()
        self._start_member_sender()
        self._start_member_read_loop(meeting_id, sock, key_or_error)

    def _start_member_read_loop(self, meeting_id: str, sock, key) -> None:
        def run():
            reason = "连接已断开"
            replay = NonceReplayGuard()
            try:
                sock.settimeout(MEDIA_READ_TIMEOUT)
                while True:
                    with self._lock:
                        live = self._sock is sock and self._meeting_id == meeting_id
                    if not live:
                        return
                    header = _read_exact(sock, 5)
                    if header is None:
                        return
                    channel = header[0]
                    length = int.from_bytes(header[1:5], "big")
                    if length < MIN_FRAME_WIRE_LEN or length > MAX_FRAME_WIRE_LEN:
                        return
                    blob = _read_exact(sock, length)
                    if blob is None:
                        return
                    if replay.is_replay(blob[:GCM_NONCE_LEN]):
                        return
                    try:
                        payload = aes_gcm_decrypt(key, blob)
                    except Exception:
                        return
                    if channel != CH_AUDIO:
                        continue
                    self._queue_playback(bytes(payload))
            except socket.timeout:
                reason = "连接已断开"
            except OSError:
                pass
            finally:
                self._sig_member_gone.emit(meeting_id, reason)

        threading.Thread(target=run, daemon=True, name="group-call-member").start()

    def _on_member_gone(self, meeting_id: str, reason: str) -> None:
        if meeting_id != self._meeting_id or self._role != "member":
            return
        self._ring_timer.stop()
        self._leave_local(reason or "连接已断开")

    # ------------------------------------------------------------- engines

    def _ensure_engines(self) -> None:
        """Start audio + mixer/sender exactly once per meeting (GUI thread)."""
        if self._audio_started:
            return
        self._audio_started = True
        self._media_stop.clear()
        self._aec = Aec()
        self._audio_prerolled = False
        self._audio_started_at = time.monotonic()
        if self._role == "host":
            self._mixer_thread = threading.Thread(
                target=self._mixer_loop, args=(self._meeting_id,), daemon=True,
                name="group-call-mixer",
            )
            self._mixer_thread.start()
        else:
            self._last_media_sent_at = 0.0
        threading.Thread(
            target=self._start_sd_audio, args=(self._meeting_id,), daemon=True,
            name="group-call-audio-probe",
        ).start()

    # ---- microphone (capture)

    def _on_mic_pcm(self, pcm: bytes) -> None:
        """Capture callback (PortAudio thread or Qt timer): route one chunk.
        Muted capture feeds silence so links stay warm (1:1 parity)."""
        if self._media_stop.is_set() or not self._audio_started:
            return
        if self._muted:
            pcm = b"\x00" * len(pcm)
        elif self._aec is not None:
            pcm = self._aec.process(pcm)
        if self._role == "host":
            with self._mic_lock:
                self._mic_buf.extend(pcm)
                if len(self._mic_buf) > INBUF_MAX:
                    del self._mic_buf[: len(self._mic_buf) - INBUF_MAX]
        else:
            while True:
                try:
                    self._send_q.put_nowait(pcm)
                    return
                except queue.Full:
                    try:
                        self._send_q.get_nowait()
                    except queue.Empty:
                        pass

    def _take_mic(self, frame_bytes: int) -> bytes:
        with self._mic_lock:
            missing = frame_bytes - len(self._mic_buf)
            if missing > 0:
                self._mic_buf.extend(b"\x00" * missing)
            chunk = bytes(self._mic_buf[:frame_bytes])
            del self._mic_buf[:frame_bytes]
        return chunk

    # ---- host mixer

    def _mixer_loop(self, meeting_id: str) -> None:
        """20 ms clock: build each member's mix-minus-self and the host's own
        playback sum, then write one frame per link."""
        tick = AUDIO_CHUNK_MS / 1000.0
        while not self._media_stop.is_set():
            with self._lock:
                live = self._meeting_id == meeting_id and self._role == "host"
            if not live:
                return
            started = time.monotonic()
            frame = AUDIO_FRAME_BYTES
            mic = self._take_mic(frame)
            with self._links_lock:
                links = list(self._links.values())
            chunks = {link.member_id: link.take_pcm(frame) for link in links}
            play = bytearray(frame)
            for link in links:
                own = chunks[link.member_id]
                others = [c for mid, c in chunks.items() if mid != link.member_id]
                mixed = mix_pcm16([mic] + others, frame)
                self._write_frame_link(link, mixed, meeting_id)
                play = bytearray(mix_pcm16([bytes(play), own], frame))
            if links:
                self._queue_playback(bytes(play))
            elapsed = time.monotonic() - started
            time.sleep(max(0.002, tick - elapsed))

    def _write_frame_link(self, link: "_Link", payload: bytes, meeting_id: str) -> None:
        with self._lock:
            live = self._meeting_id == meeting_id and self._role == "host"
        if not live:
            return
        try:
            blob = aes_gcm_encrypt(link.key, payload)
            with link.send_lock:
                link.sock.sendall(build_frame(CH_AUDIO, blob))
        except OSError:
            self._sig_link_died.emit(meeting_id, link.member_id)

    # ---- member uplink sender

    def _start_member_sender(self) -> None:
        if self._send_thread is not None and self._send_thread.is_alive():
            return
        meeting_id = self._meeting_id
        self._send_thread = threading.Thread(
            target=self._member_send_loop, args=(meeting_id,), daemon=True,
            name="group-call-sender",
        )
        self._send_thread.start()

    def _member_send_loop(self, meeting_id: str) -> None:
        """Single writer for the member uplink; sends a silence keepalive so
        the host's mixer clock never starves on a dead microphone."""
        while not self._media_stop.is_set():
            try:
                payload = self._send_q.get(timeout=0.1)
            except queue.Empty:
                payload = None
            if payload is not None:
                self._write_frame_member(payload, meeting_id)
                continue
            now = time.monotonic()
            if now - self._last_media_sent_at >= AUDIO_KEEPALIVE:
                self._last_media_sent_at = now
                self._write_frame_member(b"\x00" * AUDIO_FRAME_BYTES, meeting_id)

    def _write_frame_member(self, payload: bytes, meeting_id: str) -> None:
        with self._lock:
            sock = self._sock
            key = self._key
            live = self._meeting_id == meeting_id and self._role == "member"
        if sock is None or key is None or not live:
            return
        try:
            blob = aes_gcm_encrypt(key, payload)
            sock.sendall(build_frame(CH_AUDIO, blob))
            self._last_media_sent_at = time.monotonic()
        except OSError:
            pass  # the read loop reports the dead link

    # ---- playback (host and member share the 1:1 pending-buffer scheme)

    def _queue_playback(self, pcm: bytes) -> None:
        with self._audio_pending_lock:
            self._audio_pending.extend(pcm)
            if len(self._audio_pending) > AUDIO_PENDING_MAX:
                del self._audio_pending[: len(self._audio_pending) - AUDIO_PENDING_MAX]

    # ------------------------------------------------- audio devices (local)

    def _start_sd_audio(self, meeting_id: str) -> None:
        """Worker-thread sounddevice probe (CallManager parity): open
        capture/playback for whichever side it can, report via signal."""
        in_stream, in_rate, in_ch = None, AUDIO_SAMPLE_RATE, 1
        out_stream, out_rate = None, AUDIO_SAMPLE_RATE
        try:
            try:
                import sounddevice as sd
            except Exception:
                return
            if self._audio_in_dev is None and self._sd_input is None:
                dev = _sd_pick_device(sd, want_input=True)
                if dev is not None:
                    stream, rate, channels = self._sd_open(sd, dev, want_input=True)
                    if stream is not None:
                        in_stream, in_rate, in_ch = stream, rate, channels
            if self._audio_out_dev is None and self._sd_output is None:
                dev = _sd_pick_device(sd, want_input=False)
                if dev is not None:
                    stream, rate, channels = self._sd_open(sd, dev, want_input=False)
                    if stream is not None:
                        out_stream, out_rate = stream, rate
        except Exception:
            logger.warning(
                "sounddevice probe failed; Qt takes the missing side(s)",
                exc_info=True,
            )
        finally:
            self._sig_sd_ready.emit(
                meeting_id, in_stream, in_rate, in_ch, out_stream, out_rate
            )

    def _on_sd_audio_ready(self, meeting_id: str, in_stream, in_rate: int, in_ch: int,
                           out_stream, out_rate: int) -> None:
        stale = False
        with self._lock:
            if meeting_id != self._meeting_id or not self._audio_started:
                stale = True
            else:
                self._sd_input = in_stream
                self._sd_input_rate = in_rate
                self._sd_input_channels = in_ch
                self._sd_output = out_stream
                self._sd_output_rate = out_rate
        if stale:
            for stream in (in_stream, out_stream):
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
            return
        self._start_qt_audio()

    def _sd_open(self, sd, device: int, want_input: bool):
        """Open a sounddevice stream (16 kHz first, native rate fallback;
        mono inputs that never deliver frames re-open stereo + downmix)."""
        rates = [AUDIO_SAMPLE_RATE]
        try:
            native = int(sd.query_devices(device)["default_samplerate"])
            if native and native != AUDIO_SAMPLE_RATE:
                rates.append(native)
        except Exception:
            pass
        for sr in rates:
            for ch in ((1, 2) if want_input else (1,)):
                try:
                    if want_input:
                        stream = sd.InputStream(
                            device=device, samplerate=sr, channels=ch, dtype="int16",
                            blocksize=sr // 50, callback=self._sd_capture_cb,
                        )
                    else:
                        stream = sd.OutputStream(
                            device=device, samplerate=sr, channels=ch, dtype="int16",
                            blocksize=sr // 50, callback=self._sd_playback_cb,
                        )
                    stream.start()
                    if want_input and ch == 1:
                        before = self._sd_cb_count
                        time.sleep(0.6)
                        if self._sd_cb_count == before:
                            try:
                                stream.stop()
                            except Exception:
                                pass
                            try:
                                stream.close()
                            except Exception:
                                pass
                            continue
                    return stream, sr, ch
                except Exception as e:
                    logger.warning(
                        "sounddevice %s dev %d @%d ch%d failed: %s",
                        "input" if want_input else "output", device, sr, ch, e,
                    )
        return None, 0, 1

    @staticmethod
    def _resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
        if src_rate == dst_rate or not data:
            return data
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        n = max(1, int(round(len(x) * dst_rate / src_rate)))
        xp = np.linspace(0.0, 1.0, len(x), endpoint=False)
        fp = np.linspace(0.0, 1.0, n, endpoint=False)
        y = np.interp(fp, xp, x)
        return (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

    def _sd_capture_cb(self, indata, frames, time_info, status) -> None:
        try:
            if self._sd_input_channels == 2:
                arr = indata.astype(np.int32)
                mono = ((arr[:, 0] + arr[:, 1]) >> 1).astype(np.int16)
                pcm = mono.tobytes()
            else:
                pcm = indata.tobytes()
        except Exception:
            return
        self._sd_cb_count += 1
        rate = self._sd_input_rate
        if rate != AUDIO_SAMPLE_RATE:
            pcm = self._resample_pcm16(pcm, rate, AUDIO_SAMPLE_RATE)
        self._on_mic_pcm(pcm)

    def _sd_playback_cb(self, outdata, frames, time_info, status) -> None:
        rate = self._sd_output_rate
        need = int(frames * AUDIO_SAMPLE_RATE / rate)
        n = need * 2
        with self._audio_pending_lock:
            if self._audio_pending:
                chunk = bytes(self._audio_pending[:n])
                del self._audio_pending[:n]
                if len(chunk) < n:
                    chunk += b"\x00" * (n - len(chunk))
            else:
                chunk = b"\x00" * n
        if self._aec is not None:
            self._aec.add_reference(chunk)
        if rate != AUDIO_SAMPLE_RATE:
            chunk = self._resample_pcm16(chunk, AUDIO_SAMPLE_RATE, rate)
        try:
            outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)
        except Exception:
            pass

    def _start_qt_audio(self) -> None:
        """QtMultimedia per-side fallback (CallManager parity)."""
        fmt = QAudioFormat()
        fmt.setSampleRate(AUDIO_SAMPLE_RATE)
        fmt.setChannelCount(1)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)

        if self._audio_in_dev is None and self._sd_input is None:
            try:
                src_dev = QMediaDevices.defaultAudioInput()
                self._audio_source = QAudioSource(src_dev, fmt)
                self._audio_source.setBufferSize(AUDIO_SAMPLE_RATE * 2 // 50 * 4)
                self._audio_in_dev = self._audio_source.start()
                if (
                    self._audio_in_dev is None
                    or self._audio_source.error() != QAudio.Error.NoError
                ):
                    self._audio_in_dev = None
            except Exception:
                self._audio_source = None
                self._audio_in_dev = None

        if self._audio_out_dev is None and self._sd_output is None:
            try:
                out_dev = QMediaDevices.defaultAudioOutput()
                self._audio_sink = QAudioSink(out_dev, fmt)
                self._audio_sink.setBufferSize(AUDIO_SAMPLE_RATE * 2 * 2)
                self._audio_out_dev = self._audio_sink.start()
                if self._audio_out_dev is not None:
                    self._audio_prerolled = False
                    self._audio_started_at = time.monotonic()
            except Exception:
                self._audio_sink = None
                self._audio_out_dev = None

        if self._audio_in_dev is not None:
            self._audio_timer_in = QTimer(self)
            self._audio_timer_in.timeout.connect(self._poll_audio_in)
            self._audio_timer_in.start(AUDIO_CHUNK_MS)
        if self._audio_out_dev is not None:
            self._audio_timer_out = QTimer(self)
            self._audio_timer_out.timeout.connect(self._flush_audio_out)
            self._audio_timer_out.start(AUDIO_CHUNK_MS)

    def _poll_audio_in(self) -> None:
        src = self._audio_source
        dev = self._audio_in_dev
        if src is None or dev is None:
            return
        n = src.bytesAvailable()
        if n <= 0:
            return
        try:
            data = bytes(dev.read(n))
        except Exception:
            return
        if not data:
            return
        self._on_mic_pcm(data)

    def _flush_audio_out(self) -> None:
        sink = self._audio_sink
        dev = self._audio_out_dev
        if sink is None or dev is None:
            return
        try:
            free = sink.bytesFree()
            if free <= 0:
                return
            now = time.monotonic()
            if not self._audio_prerolled:
                with self._audio_pending_lock:
                    have = len(self._audio_pending)
                if have < AUDIO_PREROLL_BYTES and now - self._audio_started_at < 1.0:
                    return
                self._audio_prerolled = True
            with self._audio_pending_lock:
                if self._audio_pending:
                    chunk = bytes(self._audio_pending[:free])
                    del self._audio_pending[:free]
                else:
                    chunk = b"\x00" * min(free, AUDIO_SAMPLE_RATE * 2 // 20)
            if chunk:
                dev.write(chunk)
                if self._aec is not None:
                    self._aec.add_reference(chunk)
        except Exception:
            pass

    def _stop_audio(self) -> None:
        self._audio_started = False
        for timer in (self._audio_timer_in, self._audio_timer_out):
            if timer is not None:
                timer.stop()
        self._audio_timer_in = self._audio_timer_out = None
        if self._audio_source is not None:
            self._audio_source.stop()
        self._audio_source = None
        self._audio_in_dev = None
        if self._audio_sink is not None:
            self._audio_sink.stop()
        self._audio_sink = None
        self._audio_out_dev = None
        for stream in (self._sd_input, self._sd_output):
            if stream is not None:
                try:
                    stream.stop()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
        self._sd_input = self._sd_output = None
        self._audio_prerolled = False
        self._aec = None
        with self._audio_pending_lock:
            self._audio_pending.clear()

    # ------------------------------------------------------------ teardown

    def _leave_local(self, reason: str) -> None:
        """Tear the meeting down locally. GUI-thread only (every caller hops
        through a queued signal first); engine shutdown follows immediately."""
        with self._lock:
            if self._state == STATE_IDLE:
                return
            server = self._media_server
            sock = self._sock
            with self._links_lock:
                links = list(self._links.values())
                self._links.clear()
            self._expected = {}
            self._state = STATE_IDLE
            self._role = ""
            self._meeting_id = ""
            self._host_id = ""
            self._host_name = ""
            self._host_ip = ""
            self._media_port = 0
            self._p2p = None
            self._group_id = ""
            self._sock = None
            self._key = None
            self._participants_cache = []
        self._media_stop.set()
        run_catching_close(server)
        run_catching_close(sock)
        for link in links:
            run_catching_close(link.sock)
        with self._mic_lock:
            self._mic_buf.clear()
        try:
            while True:
                self._send_q.get_nowait()
        except queue.Empty:
            pass
        self._shutdown_engines()
        self.state_changed.emit(STATE_IDLE, "", "")
        self.participants_changed.emit([])
        self.call_ended.emit(reason)

    def _shutdown_engines(self) -> None:
        """Stop audio + mixer + sender (GUI thread)."""
        self._media_stop.set()
        self._stop_audio()
        mixer = self._mixer_thread
        if mixer is not None:
            mixer.join(timeout=0.5)
        self._mixer_thread = None
        sender = self._send_thread
        if sender is not None:
            sender.join(timeout=0.5)
        self._send_thread = None
        with self._mic_lock:
            self._mic_buf.clear()
        try:
            while True:
                self._send_q.get_nowait()
        except queue.Empty:
            pass
        self._ring_timer.stop()
