"""Video/audio call engine for the Windows client.

Protocol (docs/video_call_protocol.md):
- Signaling rides the group message channel via targeted packets
  (call_offer / call_answer / call_reject / call_hangup / call_failed),
  routed by the group host to the addressed member.
- Media travels over a single direct TCP connection opened by the caller
  (the callee connects back). Frames:
      [1 byte channel][4 bytes big-endian length][payload]
  channel 0 = video (JPEG), channel 1 = audio (PCM16 mono 16 kHz).

Capture:
- Video: OpenCV (cv2) because PyQt6 pip wheels do not ship the Qt ffmpeg
  multimedia backend on Windows; falls back to ffmpeg/DirectShow capture and
  then to a synthetic test pattern when no camera can be opened.
- Audio: QtMultimedia QAudioSource/QAudioSink first (work without a backend
  on other machines); when QtMultimedia has no audio backend at all,
  sounddevice (PortAudio) provides capture/playback — same PCM16 mono 16 kHz
  wire format either way.

Threading: the media socket lives on plain worker threads; all state
transitions and Qt audio/video objects stay on the GUI thread, reached via
queued signals.
"""

import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid

import cv2
import numpy as np
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QImage
from PyQt6.QtMultimedia import (
    QAudio,
    QAudioFormat,
    QAudioSink,
    QAudioSource,
    QMediaDevices,
)

from .aec import Aec
from .crypto import GCM_NONCE_LEN, aes_gcm_decrypt, aes_gcm_encrypt
from .models import CallInfo, NetworkPacket
from .network import _read_raw_line, make_wire
from .securewire import Handshake, Protocol, Wire

logger = __import__("logging").getLogger(__name__)

# Frame channels on the media socket.
CH_VIDEO = 0
CH_AUDIO = 1

AUDIO_SAMPLE_RATE = 16000
AUDIO_CHUNK_MS = 20  # 640 bytes of PCM16 mono
MEDIA_READ_TIMEOUT = 15.0  # seconds without traffic -> call considered dead
RING_TIMEOUT = 45.0  # caller gives up if the callee never connects
CONNECT_TIMEOUT = 8.0  # callee -> caller media connect
MAX_FRAME_LEN = 512 * 1024
# Largest ciphertext frame on the wire: a MAX_FRAME_LEN payload plus the GCM
# nonce and tag aes_gcm_encrypt prepends/appends per frame.
MAX_FRAME_WIRE_LEN = MAX_FRAME_LEN + GCM_NONCE_LEN + 16
VIDEO_MAX_EDGE = 640
VIDEO_JPEG_QUALITY = 70
VIDEO_INTERVAL = 0.08  # ~12 fps

# Audio jitter/pacing: pre-roll before playback starts, a hard ceiling on the
# pending playback buffer (drop-oldest), and a bounded send queue so capture
# callbacks never block on the socket.
AUDIO_PREROLL_BYTES = AUDIO_SAMPLE_RATE * 2 * 4  # ~80 ms
AUDIO_PENDING_MAX = AUDIO_SAMPLE_RATE * 2 * 2  # 2 s, drop-oldest beyond
AUDIO_SEND_QUEUE = 64  # audio chunks awaiting the socket (drop-oldest)

# Devices that look like virtual cameras and should be skipped when picking an
# ffmpeg/DirectShow capture device automatically.
_VIRTUAL_CAMERA_HINTS = (
    "vtube", "obs", "splitcam", "manycam", "virtual", "camtwist",
    "streamlabs", "xsplit", "e2e", "unity", "epoccam",
)

# Audio endpoints that are virtual or loopback mixers and must never be used
# as the call's microphone/speaker (the peer would hear the PC's own audio).
_VIRTUAL_AUDIO_HINTS = (
    "steam", "virtual", "wave", "stereo mix", "loopback", "混音", "立体声混音",
)

# Call states surfaced to the UI.
STATE_IDLE = "idle"
STATE_OUTGOING = "outgoing"
STATE_INCOMING = "incoming"
STATE_ACTIVE = "active"


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def build_frame(channel: int, payload: bytes) -> bytes:
    """Encode one media frame: [1 byte channel][4 bytes big-endian length][payload]."""
    return bytes([channel]) + struct.pack(">I", len(payload)) + payload


class FrameDecoder:
    """Incremental media-frame parser (channel, payload) tuples; also used by
    the tests to verify the wire format without sockets."""

    def __init__(self):
        self._buf = bytearray()
        self.frames = []

    def feed(self, data: bytes) -> None:
        self._buf.extend(data)
        while len(self._buf) >= 5:
            channel = self._buf[0]
            length = int.from_bytes(self._buf[1:5], "big")
            if length < 0 or length > MAX_FRAME_LEN:
                self._buf.clear()
                return
            if len(self._buf) < 5 + length:
                break
            payload = bytes(self._buf[5:5 + length])
            del self._buf[:5 + length]
            self.frames.append((channel, payload))


class CallManager(QObject):
    """One call at a time. Owns signaling, the media socket and the
    capture/playback resources; created on the GUI thread."""

    # (state, peer_name, detail) — state in idle/outgoing/incoming/active.
    state_changed = pyqtSignal(str, str, str)
    incoming_call = pyqtSignal(str, str)  # (call_id, caller_name)
    remote_frame = pyqtSignal(object)  # QImage
    local_frame = pyqtSignal(object)  # QImage
    remote_audio = pyqtSignal(bytes)
    call_ended = pyqtSignal(str)  # reason
    call_error = pyqtSignal(str)  # message

    # Cross-thread plumbing (network threads -> GUI thread).
    _sig_packet = pyqtSignal(object, object)  # (p2p, NetworkPacket)
    _sig_media_socket = pyqtSignal(object, object)  # (socket, session_key)
    _sig_media_ended = pyqtSignal(str)  # read loop ended: reason
    _sig_connect_failed = pyqtSignal(str)
    _sig_ring_timeout = pyqtSignal(str)  # call_id

    def __init__(self, parent=None):
        super().__init__(parent)
        self._lock = threading.RLock()
        self._state = STATE_IDLE
        self._peer_id = ""
        self._peer_name = ""
        self._peer_ip = ""
        self._call_id = ""
        self._p2p = None
        self._role = ""  # "caller" | "callee"
        # Signaling channel of the current call: callable(peer_id, packet).
        # Group calls use the host relay (P2PManager.send_targeted); direct
        # member calls write straight to the session socket via
        # DirectChatManager.send_packet (Android parity: CallChannel).
        self._channel_send = None
        # Identity of the signaling owner (a P2PManager or a DirectChatManager);
        # end_if_on compares against it so a call only ends on the right link.
        self._identity = None
        # Our own device id/name for the current call (the group p2p and the
        # direct manager expose the same identity, but the call must remember
        # whose identity it used).
        self._my_id = ""
        self._my_name = ""
        self._media_server = None
        self._media_socket = None
        # Session key negotiated by the media handshake; every frame is
        # AES-GCM encrypted under it (nonce travels in the frame).
        self._media_key = None
        self._send_lock = threading.Lock()
        self._stop_event = threading.Event()
        # Audio-priority media sender: audio chunks before video, written by a
        # dedicated thread so capture callbacks never block on the socket.
        self._audio_send_q = queue.Queue(maxsize=AUDIO_SEND_QUEUE)
        self._video_send_q = queue.Queue(maxsize=1)  # keep only the newest frame
        self._send_thread = None
        self._send_stop = threading.Event()

        self._capture = None  # cv2.VideoCapture
        self._capture_thread = None
        self._synthetic = False
        self._ffmpeg_proc = None  # subprocess-based capture fallback
        # Hot-swappable camera state: switch_camera() closes the current
        # source and the capture loop reopens it for the new index.
        self._cv_cap = None  # current cv2.VideoCapture
        self._camera_index = 0  # which camera device is active (0/1...)
        self._ffmpeg_devices: list = []  # cached DirectShow device list
        self._audio_muted = False
        self._video_muted = False

        self._audio_source = None
        self._audio_in_dev = None
        self._audio_sink = None
        self._audio_out_dev = None
        self._audio_timer_in = None
        self._audio_timer_out = None
        self._audio_pending = bytearray()
        self._audio_pending_lock = threading.Lock()
        self._audio_started = False
        self._audio_prerolled = False
        self._audio_started_at = 0.0
        # Software acoustic echo canceller (see aec.py); created with the
        # audio engine, fed the played PCM and applied to the mic capture.
        self._aec = None
        # PortAudio (sounddevice) fallback streams, used when QtMultimedia has
        # no audio backend on this machine (see _start_sd_audio).
        self._sd_input = None
        self._sd_output = None
        self._sd_input_rate = AUDIO_SAMPLE_RATE
        self._sd_output_rate = AUDIO_SAMPLE_RATE
        self._sd_input_channels = 1
        self._sd_cb_count = 0

        self._sig_packet.connect(self._on_packet)
        self._sig_media_socket.connect(self._on_media_socket)
        self._sig_media_ended.connect(self._on_media_ended)
        self._sig_connect_failed.connect(self._on_connect_failed)
        self._sig_ring_timeout.connect(self._on_ring_timeout)
        self.remote_audio.connect(self._on_remote_audio)

    # ------------------------------------------------------------- public API

    @property
    def state(self) -> str:
        return self._state

    @property
    def peer_name(self) -> str:
        return self._peer_name

    def attach(self, p2p) -> None:
        """Register the signaling listener on a group connection. Safe to call
        for every P2PManager (the listener dispatches by packet)."""
        p2p.call_listener = self._on_signal

    def detach(self, p2p) -> None:
        if getattr(p2p, "call_listener", None) is self._on_signal:
            p2p.call_listener = None

    def _on_signal(self, p2p, packet) -> None:
        """Network-thread entry: hop to the GUI thread."""
        self._sig_packet.emit(p2p, packet)

    def start_call(self, p2p, peer_id: str) -> None:
        if self._state != STATE_IDLE:
            self.call_error.emit("已有进行中的通话")
            return
        peer = p2p.peers.get(peer_id)
        if peer is None or not peer.ip_address:
            self.call_error.emit("无法发起通话：成员不在线")
            return
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", 0))
            server.listen(1)
        except OSError:
            self.call_error.emit("无法开启通话端口")
            return
        with self._lock:
            self._p2p = p2p
            self._channel_send = lambda pid, pkt: p2p.send_targeted(pid, pkt)
            self._identity = p2p
            self._my_id = p2p.my_id
            self._my_name = p2p.my_name
            self._peer_id = peer_id
            self._peer_name = peer.name
            self._peer_ip = peer.ip_address
            self._call_id = str(uuid.uuid4())
            self._role = "caller"
            self._media_server = server
            self._state = STATE_OUTGOING
        self.state_changed.emit(STATE_OUTGOING, self._peer_name, "")
        p2p.send_targeted(
            peer_id,
            NetworkPacket(
                type="call_offer",
                call=CallInfo(
                    call_id=self._call_id,
                    caller_id=p2p.my_id,
                    caller_name=p2p.my_name,
                    callee_id=peer_id,
                    media_port=server.getsockname()[1],
                ),
            ),
        )
        threading.Thread(
            target=self._accept_loop, args=(server, self._call_id), daemon=True
        ).start()

    def start_direct_call(self, channel_send, identity, peer, my_id: str, my_name: str) -> None:
        """Start a video call over a DIRECT member session: signaling rides the
        session socket via [channel_send](peer_id, packet), media over the
        usual TCP connection. [identity] is the DirectChatManager owning the
        session (used by end_if_on)."""
        if self._state != STATE_IDLE:
            self.call_error.emit("已有进行中的通话")
            return
        if peer is None or not peer.ip_address:
            self.call_error.emit("无法发起通话：成员不在线")
            return
        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", 0))
            server.listen(1)
        except OSError:
            self.call_error.emit("无法开启通话端口")
            return
        with self._lock:
            self._p2p = None
            self._channel_send = channel_send
            self._identity = identity
            self._my_id = my_id
            self._my_name = my_name
            self._peer_id = peer.id
            self._peer_name = peer.name
            self._peer_ip = peer.ip_address
            self._call_id = str(uuid.uuid4())
            self._role = "caller"
            self._media_server = server
            self._state = STATE_OUTGOING
        self.state_changed.emit(STATE_OUTGOING, self._peer_name, "")
        channel_send(
            peer.id,
            NetworkPacket(
                type="call_offer",
                call=CallInfo(
                    call_id=self._call_id,
                    caller_id=my_id,
                    caller_name=my_name,
                    callee_id=peer.id,
                    media_port=server.getsockname()[1],
                ),
            ),
        )
        threading.Thread(
            target=self._accept_loop, args=(server, self._call_id), daemon=True
        ).start()

    def accept_call(self) -> None:
        with self._lock:
            if self._state != STATE_INCOMING:
                return
            call_id = self._call_id
            p2p = self._p2p
            ip = self._peer_ip
            port = self._media_port
            peer_id = self._peer_id
        if not ip or port <= 0:
            self._reject_flow("通话信息无效")
            return

        def run():
            sock = None
            try:
                sock = socket.create_connection((ip, port), timeout=CONNECT_TIMEOUT)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(10)
                # Identity handshake on the media socket (the callee dials the
                # caller's media port, so the callee is the handshake initiator
                # and proves its long-term key): every frame after this is
                # AES-256-GCM encrypted under the negotiated session key.
                wire = make_wire(sock)
                secured = Handshake.initiate_direct(
                    wire,
                    # an "ip:..." placeholder is not a stable device id:
                    # binding TOFU state to it would false-positive once the
                    # real id is known (the handshake still authenticates the
                    # caller's long-term key via its signature)
                    expected_peer_id=(
                        peer_id if peer_id and not peer_id.startswith("ip:") else None
                    ),
                    on_identity_mismatch=lambda: self.call_error.emit(
                        "安全警告：对方媒体身份验证失败，通话已结束"
                    ),
                )
                key = wire.session_key
            except OSError as e:
                run_catching_close(sock)
                self._sig_connect_failed.emit(f"{ip}:{port}（{e}）")
                return
            except Exception:
                run_catching_close(sock)
                self._sig_connect_failed.emit(f"{ip}:{port}（安全握手失败）")
                return
            self._sig_media_socket.emit(sock, key)

        threading.Thread(target=run, daemon=True).start()

    def reject_call(self) -> None:
        self._reject_flow("已拒绝")

    def _reject_flow(self, detail: str) -> None:
        with self._lock:
            p2p = self._p2p
            call_id = self._call_id
            if self._state not in (STATE_INCOMING, STATE_OUTGOING):
                return
            if self._state == STATE_INCOMING:
                self._send_call_packet(p2p, "call_reject", call_id)
            server = self._media_server
            self._reset()
        run_catching_close(server)
        self.state_changed.emit(STATE_IDLE, "", "")
        self.call_ended.emit(detail)

    def hangup(self) -> None:
        """End the current call from the UI (any state)."""
        with self._lock:
            p2p = self._p2p
            call_id = self._call_id
            state = self._state
        if state in (STATE_ACTIVE, STATE_OUTGOING, STATE_INCOMING):
            # notify the peer over the current call's signaling channel —
            # must NOT be gated on p2p: a direct call has no p2p, only the
            # channel (session socket)
            self._send_call_packet(p2p, "call_hangup", call_id)
        self._end_call("已挂断")

    def set_audio_muted(self, muted: bool) -> None:
        self._audio_muted = muted

    def set_video_muted(self, muted: bool) -> None:
        self._video_muted = muted

    @property
    def camera_index(self) -> int:
        """Which camera device the call is currently capturing from (0/1)."""
        return self._camera_index

    def switch_camera(self) -> None:
        """Flip to the other camera device during an active call (OpenCV path
        switches index 0 <-> 1; the ffmpeg fallback switches device names).
        Closing the current source makes the capture loop reopen the new one."""
        with self._lock:
            if self._state != STATE_ACTIVE:
                return
            self._camera_index = 1 - self._camera_index
            self._synthetic = False  # retry opening the newly selected camera
        self._close_capture()

    def end_if_on(self, p2p, reason: str, peer_id: str = None) -> None:
        """End the current call when it rides the given signaling link (a
        group connection OR a direct session — the identity may be a
        P2PManager or the DirectChatManager). [peer_id] optionally narrows the
        match to a specific session, so an unrelated direct session closing
        never kills a call on another session."""
        with self._lock:
            if self._state == STATE_IDLE or self._identity is not p2p:
                return
            if peer_id is not None and self._peer_id != peer_id:
                return
        self._end_call(reason)

    # ------------------------------------------------------- signal handlers

    def _on_packet(self, p2p, packet) -> None:
        if packet.call is None:
            return
        call = packet.call
        if packet.type == "call_offer":
            peer = p2p.peers.get(call.caller_id)
            self._on_call_offer(
                channel_send=lambda pid, pkt: p2p.send_targeted(pid, pkt),
                identity=p2p,
                call=call,
                caller_ip=peer.ip_address if peer is not None else "",
                my_id=p2p.my_id,
                my_name=p2p.my_name,
            )
        elif packet.type == "call_answer":
            self._on_call_answer(call)
        elif packet.type == "call_reject":
            self._on_call_reject(call)
        elif packet.type == "call_failed":
            self._on_call_failed(call)
        elif packet.type == "call_hangup":
            self._on_call_hangup(call)

    def handle_direct_signal(self, channel_send, identity, packet, caller_ip: str,
                             my_id: str, my_name: str) -> None:
        """Session-thread entry: a direct member chat forwarded call signaling
        here (see DirectChatManager.on_call_signal). [channel_send] delivers
        the reply over the direct session; [caller_ip] is the caller's address
        (resolved by the ViewModel from the contacts)."""
        if packet.call is None:
            return
        call = packet.call
        if packet.type == "call_offer":
            self._on_call_offer(
                channel_send=channel_send,
                identity=identity,
                call=call,
                caller_ip=caller_ip,
                my_id=my_id,
                my_name=my_name,
            )
        elif packet.type == "call_answer":
            self._on_call_answer(call)
        elif packet.type == "call_reject":
            self._on_call_reject(call)
        elif packet.type == "call_failed":
            self._on_call_failed(call)
        elif packet.type == "call_hangup":
            self._on_call_hangup(call)

    def _on_call_offer(self, channel_send, identity, call, caller_ip: str,
                       my_id: str, my_name: str) -> None:
        if call.media_port <= 0 or not call.caller_id:
            return
        with self._lock:
            if self._state != STATE_IDLE:
                # busy: reply to the offerer directly (this call is not ours)
                reply = CallInfo(
                    call_id=call.call_id,
                    caller_id=call.caller_id,
                    caller_name=call.caller_name,
                    callee_id=call.callee_id,
                )
                channel_send(call.caller_id, NetworkPacket(type="call_reject", call=reply))
                return
            # replies ride the channel, not _p2p (a direct session has no p2p)
            self._p2p = None
            self._channel_send = channel_send
            self._identity = identity
            self._my_id = my_id
            self._my_name = my_name
            self._call_id = call.call_id
            self._role = "callee"
            self._peer_id = call.caller_id
            self._peer_name = call.caller_name
            self._peer_ip = caller_ip
            self._media_port = call.media_port
            self._state = STATE_INCOMING
        self.state_changed.emit(STATE_INCOMING, call.caller_name, "")
        self.incoming_call.emit(call.call_id, call.caller_name)

    def _on_call_answer(self, call) -> None:
        # The media socket may have already activated the call (see
        # _on_media_socket); the answer is then redundant confirmation.
        with self._lock:
            if call.call_id != self._call_id:
                return
            if self._state == STATE_OUTGOING:
                self._state = STATE_ACTIVE
            elif self._state != STATE_ACTIVE:
                return
        self.state_changed.emit(STATE_ACTIVE, self._peer_name, "")
        self._start_capture()
        self._start_audio()

    def _on_call_reject(self, call) -> None:
        with self._lock:
            if self._state == STATE_OUTGOING and call.call_id == self._call_id:
                self._reset()
            else:
                return
        self.state_changed.emit(STATE_IDLE, "", "")
        self.call_ended.emit("对方拒绝了通话")

    def _on_call_failed(self, call) -> None:
        with self._lock:
            if self._state == STATE_OUTGOING and call.call_id == self._call_id:
                self._reset()
            else:
                return
        self.state_changed.emit(STATE_IDLE, "", "")
        self.call_error.emit(
            "通话建立失败：对方无法连接本机的媒体端口。\n"
            "请在 Windows 防火墙中放行本程序的入站连接（不只是 9999 端口）后重试"
        )

    def _on_call_hangup(self, call) -> None:
        with self._lock:
            if call.call_id != self._call_id or self._state == STATE_IDLE:
                return
            was_ringing = self._state in (STATE_INCOMING, STATE_OUTGOING)
            server = self._media_server
            sock = self._media_socket
            self._reset()
        run_catching_close(server)
        run_catching_close(sock)
        self._shutdown_engines()
        if was_ringing:
            self.call_ended.emit("对方取消了通话")
        else:
            self.call_ended.emit("对方已挂断")
        self.state_changed.emit(STATE_IDLE, "", "")

    def _on_media_socket(self, sock, key) -> None:
        """Media socket arrived (caller accepted / callee connected) with its
        negotiated session key.

        The callee only connects after the user accepted, so a connected media
        socket IS the answer signal: the caller goes active immediately and
        call_answer (sent by the callee) becomes redundant confirmation. This
        avoids a stuck "outgoing" state if that answer packet is ever lost."""
        with self._lock:
            role = self._role
            state = self._state
            if state == STATE_IDLE:
                run_catching_close(sock)
                return
            self._media_socket = sock
            self._media_key = key
            if role == "caller":
                activate = state == STATE_OUTGOING
                if activate:
                    self._state = STATE_ACTIVE
            else:
                # Callee: tell the caller and go live (answer is idempotent).
                p2p = self._p2p
                self._send_call_packet(p2p, "call_answer", self._call_id)
                activate = state != STATE_ACTIVE
                if activate:
                    self._state = STATE_ACTIVE
        if activate:
            self.state_changed.emit(STATE_ACTIVE, self._peer_name, "")
            self._start_capture()
            self._start_audio()
        self._start_read_loop(sock)

    def _on_media_ended(self, reason: str) -> None:
        self._end_call(reason)

    def _on_connect_failed(self, message: str) -> None:
        with self._lock:
            p2p = self._p2p
            call_id = self._call_id
            if self._state != STATE_INCOMING:
                return
            self._send_call_packet(
                p2p,
                "call_failed",
                call_id,
                error_message=f"无法连接媒体通道: {message}",
            )
            self._reset()
        self.state_changed.emit(STATE_IDLE, "", "")
        self.call_error.emit(
            f"无法连接媒体通道（{message}）。请检查对方电脑的防火墙，或确认两台设备在同一网络"
        )

    def _on_ring_timeout(self, call_id: str) -> None:
        with self._lock:
            if self._state != STATE_OUTGOING or call_id != self._call_id:
                return
            p2p = self._p2p
            self._send_call_packet(p2p, "call_hangup", call_id)
            server = self._media_server
            self._reset()
        run_catching_close(server)
        self.state_changed.emit(STATE_IDLE, "", "")
        self.call_ended.emit("对方未接听")

    # ------------------------------------------------------------- internals

    def _send_call_packet(self, p2p, pkt_type: str, call_id: str,
                          error_message: str = None) -> None:
        """Send a call packet to the other participant of the current call.
        The CallInfo always carries the original caller's identity and the
        callee's id, matching what the Android side validates on the host.
        The reply goes over the current call's signaling channel (group relay
        or direct session)."""
        channel = self._channel_send
        if channel is None:
            if p2p is None:
                return
            channel = lambda pid, pkt: p2p.send_targeted(pid, pkt)
        with self._lock:
            if self._role == "caller":
                caller_id, caller_name, callee_id = self._my_id, self._my_name, self._peer_id
            else:
                caller_id, caller_name, callee_id = self._peer_id, self._peer_name, self._my_id
            call = CallInfo(
                call_id=call_id,
                caller_id=caller_id,
                caller_name=caller_name,
                callee_id=callee_id,
            )
        packet = NetworkPacket(type=pkt_type, call=call)
        if error_message:
            packet.error_message = error_message
        channel(self._peer_id, packet)

    def _accept_loop(self, server, call_id) -> None:
        sock = None
        try:
            server.settimeout(RING_TIMEOUT)
            sock, _ = server.accept()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(10)
            # Identity handshake on the media socket (the caller listens on the
            # media port, so the caller is the handshake acceptor and the
            # callee's dial proves its long-term key): frames after this are
            # AES-256-GCM encrypted under the negotiated session key. Raw line
            # IO — a buffered reader would swallow the first frames.
            start_line = _read_raw_line(sock)
            start = None
            if start_line is not None:
                try:
                    start = NetworkPacket.from_json(start_line)
                except Exception:
                    start = None  # malformed/foreign first line -> handshake failure
            if start is None or start.type != Protocol.HS_START:
                raise OSError("no media handshake")
            wire = make_wire(sock)
            with self._lock:
                peer_id = self._peer_id
            secured = Handshake.accept_direct(
                wire,
                start,
                # an "ip:..." placeholder is not a stable device id (same rule
                # as the outgoing side): never bind TOFU state to one
                expected_peer_id=(
                    peer_id if peer_id and not peer_id.startswith("ip:") else None
                ),
                on_identity_mismatch=lambda: self.call_error.emit(
                    "安全警告：对方媒体身份验证失败，通话已结束"
                ),
            )
            if secured is None:
                raise OSError("media handshake rejected")
            key = wire.session_key
        except socket.timeout:
            run_catching_close(sock)
            self._sig_ring_timeout.emit(call_id)
            return
        except OSError:
            run_catching_close(sock)
            self._sig_media_ended.emit("安全握手失败，通话已结束")
            return
        self._sig_media_socket.emit(sock, key)

    def _start_read_loop(self, sock) -> None:
        self._start_send_thread()
        threading.Thread(target=self._media_read_loop, args=(sock,), daemon=True).start()

    def _media_read_loop(self, sock) -> None:
        reason = "对方已挂断"
        try:
            sock.settimeout(MEDIA_READ_TIMEOUT)
            while not self._stop_event.is_set():
                header = _read_exact(sock, 5)
                if header is None:
                    return
                channel = header[0]
                length = int.from_bytes(header[1:5], "big")
                # ciphertext frame: at least the GCM nonce + tag, at most the
                # plaintext cap plus that overhead
                if length < GCM_NONCE_LEN + 16 or length > MAX_FRAME_WIRE_LEN:
                    return
                blob = _read_exact(sock, length)
                if blob is None:
                    return
                key = self._media_key
                if key is None:
                    return
                try:
                    payload = aes_gcm_decrypt(key, blob)
                except Exception:
                    # tampered / wrong key — a failed authentication means the
                    # other side is not who we secured the channel with
                    return
                if channel == CH_VIDEO:
                    qimg = QImage.fromData(payload)
                    if not qimg.isNull():
                        self.remote_frame.emit(qimg)
                elif channel == CH_AUDIO:
                    self.remote_audio.emit(bytes(payload))
        except socket.timeout:
            reason = "连接已断开"
        except OSError:
            pass
        finally:
            self._sig_media_ended.emit(reason)

    def _send_media(self, channel: int, payload: bytes) -> None:
        """Queue a media frame for the background sender thread.

        Capture callbacks must never block on the socket (a stalled TCP write
        would glitch the microphone), so audio is queued with drop-oldest and
        the sender serves audio with strict priority over video."""
        if self._stop_event.is_set() or self._send_stop.is_set():
            return
        if channel == CH_AUDIO:
            q = self._audio_send_q
            while True:
                try:
                    q.put_nowait(payload)
                    return
                except queue.Full:
                    try:
                        q.get_nowait()  # drop the oldest chunk
                    except queue.Empty:
                        pass
        else:
            # video: keep only the newest frame (stale ones are dropped)
            q = self._video_send_q
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass

    def _start_send_thread(self) -> None:
        if self._send_thread is not None and self._send_thread.is_alive():
            return
        self._send_stop.clear()
        self._send_thread = threading.Thread(
            target=self._send_loop, daemon=True, name="call-media-sender"
        )
        self._send_thread.start()

    def _send_loop(self) -> None:
        """Single writer for the media socket: audio first, video in gaps.

        Audio is produced every ~20 ms for the whole call (silence frames
        included), so a blocking get() on the audio queue would starve video
        entirely (the queue is never empty). A pending video frame is sent
        right after each audio chunk instead; the video source is throttled
        (~12 fps) so this cannot flood the link."""
        while not self._send_stop.is_set():
            payload = None
            try:
                payload = self._audio_send_q.get(timeout=0.1)
            except queue.Empty:
                payload = None
            if payload is not None:
                self._write_frame(CH_AUDIO, payload)
                self._send_pending_video()
                continue
            self._send_pending_video()

    def _send_pending_video(self) -> None:
        try:
            payload = self._video_send_q.get_nowait()
        except queue.Empty:
            payload = None
        if payload is not None:
            self._write_frame(CH_VIDEO, payload)

    def _write_frame(self, channel: int, payload: bytes) -> None:
        sock = self._media_socket
        key = self._media_key
        if sock is None or key is None or self._send_stop.is_set():
            return
        try:
            blob = aes_gcm_encrypt(key, payload)
            with self._send_lock:
                if sock is not None and not self._send_stop.is_set():
                    sock.sendall(build_frame(channel, blob))
        except OSError:
            self._sig_media_ended.emit("连接已断开")

    # ------------------------------------------------------------ capture

    def _start_capture(self) -> None:
        if self._capture_thread is not None:
            return
        self._stop_event.clear()
        self._capture_thread = threading.Thread(
            target=self._video_capture_loop, daemon=True
        )
        self._capture_thread.start()

    def _close_capture(self) -> None:
        """Stop the current capture source (ffmpeg subprocess or OpenCV
        camera). Safe to call from any thread; the reader threads unblock and
        exit."""
        proc = self._ffmpeg_proc
        self._ffmpeg_proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        cap = self._cv_cap
        self._cv_cap = None
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    def _open_camera(self, cam_queue) -> bool:
        """Open the camera selected by self._camera_index: OpenCV first, then
        the ffmpeg/DirectShow fallback. Returns True when a source started."""
        index = self._camera_index
        for backend in (cv2.CAP_DSHOW, cv2.CAP_MSMF):
            try:
                c = cv2.VideoCapture(index, backend)
                if c is not None and c.isOpened():
                    self._cv_cap = c
                    threading.Thread(
                        target=self._cam_reader, args=(c, cam_queue), daemon=True
                    ).start()
                    logger.info("camera index %d opened via OpenCV", index)
                    return True
                if c is not None:
                    c.release()
            except Exception:
                continue
        if self._start_ffmpeg_capture(cam_queue, index):
            return True
        logger.warning("no capture source for camera index %d", index)
        return False

    def _cam_reader(self, camera, cam_queue) -> None:
        """Read frames from an OpenCV camera into the shared queue; exits when
        the camera is switched out (self._cv_cap replaced) or the call ends."""
        try:
            while not self._stop_event.is_set():
                if self._cv_cap is not camera:
                    break  # switched to another camera
                ok, frame = camera.read()
                if not ok:
                    time.sleep(0.25)
                    continue
                if cam_queue.full():
                    try:
                        cam_queue.get_nowait()
                    except queue.Empty:
                        pass
                cam_queue.put_nowait(frame)
        finally:
            try:
                camera.release()
            except Exception:
                pass

    def _video_capture_loop(self) -> None:
        """Send video frames as long as the call is active.

        Capture sources, tried in order:
          1. OpenCV camera (cv2.VideoCapture) for self._camera_index.
          2. ffmpeg/DirectShow subprocess — used when OpenCV's Windows camera
             backend cannot enumerate devices at all (e.g. a virtual-camera
             driver or the Microsoft Store Python runtime); the device for the
             current index is picked from the enumerated list.
          3. Synthetic test pattern — last resort when no capture works.

        switch_camera() closes the current source; this loop reopens it for
        the new index. A momentarily empty queue (camera opening / switching)
        sends a throttled black frame so the remote never sees a gap; the
        synthetic pattern is only used when no camera exists at all.
        """
        cam_queue = queue.Queue(maxsize=2)
        idx = 0
        try:
            while not self._stop_event.is_set():
                if (
                    self._cv_cap is None
                    and self._ffmpeg_proc is None
                    and not self._synthetic
                ):
                    if not self._open_camera(cam_queue):
                        self._synthetic = True
                frame = None
                try:
                    frame = cam_queue.get_nowait()
                except queue.Empty:
                    frame = None
                if frame is None:
                    if self._video_muted:
                        # keep the media socket alive even when video is off
                        # and no camera is present (audio silence may also be
                        # unavailable if the mic failed)
                        if idx % 10 == 0:
                            self._send_black_frame()
                        time.sleep(VIDEO_INTERVAL)
                        continue
                    if self._synthetic:
                        frame = self._synthetic_frame(idx)
                    else:
                        # real camera present but momentarily empty (opening,
                        # switching, or the driver stalled): a throttled black
                        # frame keeps the wire alive without test-pattern noise
                        if idx % 10 == 0:
                            self._send_black_frame()
                        time.sleep(VIDEO_INTERVAL)
                        continue
                idx += 1
                h, w = frame.shape[:2]
                scale = min(1.0, VIDEO_MAX_EDGE / max(w, h))
                if scale < 1.0:
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
                # mirrored local preview
                preview = cv2.flip(frame, 1)
                self.local_frame.emit(self._to_qimage(preview))
                if self._video_muted:
                    # send an occasional black frame so the remote side knows
                    # video is off (audio silence already keeps it alive)
                    if idx % 10 == 0:
                        self._send_black_frame()
                else:
                    ok, jpg = cv2.imencode(
                        ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, VIDEO_JPEG_QUALITY]
                    )
                    if ok:
                        self._send_media(CH_VIDEO, jpg.tobytes())
                time.sleep(VIDEO_INTERVAL)
        finally:
            self._close_capture()
            self._capture_thread = None

    # ------------------------------------------------- ffmpeg capture fallback

    def _start_ffmpeg_capture(self, cam_queue, index: int = 0) -> bool:
        """Start an ffmpeg/DirectShow capture subprocess for the camera device
        at [index] and a reader thread that decodes MJPEG frames into
        [cam_queue]. Returns True when a capture was actually started (the
        reader may still produce nothing if the device is busy)."""
        ffmpeg = None
        try:
            import imageio_ffmpeg  # bundled modern ffmpeg (preferred)

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
        if not ffmpeg:
            ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            logger.warning("no ffmpeg available for camera capture fallback")
            return False
        devices = self._ffmpeg_devices
        if not devices:
            devices = self._list_ffmpeg_devices(ffmpeg)
            self._ffmpeg_devices = devices
        if index >= len(devices):
            logger.warning(
                "ffmpeg: no DirectShow device at index %d (%d listed)",
                index, len(devices),
            )
            return False
        device = devices[index]
        try:
            proc = subprocess.Popen(
                [
                    ffmpeg, "-loglevel", "error",
                    "-f", "dshow", "-i", "video=" + device,
                    "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "7", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as e:
            logger.warning("ffmpeg capture failed to start: %s", e)
            return False
        self._ffmpeg_proc = proc
        logger.info("camera capture via ffmpeg dshow device: %s", device)
        threading.Thread(
            target=self._ffmpeg_reader, args=(proc, cam_queue), daemon=True
        ).start()
        return True

    @staticmethod
    def _list_ffmpeg_devices(ffmpeg: str) -> list:
        """Enumerate DirectShow video device names for camera switching; real
        cameras first, virtual ones (VTubeStudioCam, OBS, ...) last, so index
        0 is the best camera. Handles both the modern ffmpeg output ('"Name"
        (video)' lines, no section header) and the older output (quoted names
        after a 'DirectShow video devices' header)."""
        try:
            out = subprocess.run(
                [ffmpeg, "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                capture_output=True,
                text=True,
                timeout=15,
                errors="replace",
            )
        except Exception:
            return []
        lines = (out.stderr or "").splitlines()
        names = []
        # modern ffmpeg (>= ~4.x): every video device is printed as
        #   [dshow @ ...] "Device Name" (video)
        for l in lines:
            if "(video)" in l:
                m = re.search(r'"([^"]+)"', l)
                if m:
                    names.append(m.group(1))
        if not names:
            # older ffmpeg: quoted names following the section header
            try:
                start = next(
                    i for i, l in enumerate(lines) if "DirectShow video devices" in l
                )
            except StopIteration:
                start = -1
            if start >= 0:
                for l in lines[start + 1:]:
                    m = re.search(r'"([^"]+)"', l)
                    if not m:
                        break
                    names.append(m.group(1))
        real = [n for n in names if not any(h in n.lower() for h in _VIRTUAL_CAMERA_HINTS)]
        virtual = [n for n in names if any(h in n.lower() for h in _VIRTUAL_CAMERA_HINTS)]
        return real + virtual

    def _ffmpeg_reader(self, proc, cam_queue) -> None:
        """Read MJPEG frames from the ffmpeg stdout pipe and decode them into
        the shared queue (same format as the OpenCV camera path)."""
        buf = bytearray()
        try:
            while not self._stop_event.is_set():
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf.extend(chunk)
                while True:
                    i = buf.find(b"\xff\xd8")  # JPEG SOI
                    if i < 0:
                        # no frame start yet: keep only a bounded tail
                        if len(buf) > 128 * 1024:
                            del buf[: len(buf) - 65536]
                        break
                    if i > 0:
                        del buf[:i]
                    j = buf.find(b"\xff\xd9")  # JPEG EOI
                    if j < 0:
                        # incomplete frame: bound the buffer, wait for more
                        if len(buf) > MAX_FRAME_LEN + 256 * 1024:
                            del buf[:]
                        break
                    jpeg = bytes(buf[: j + 2])
                    del buf[: j + 2]
                    try:
                        frame = cv2.imdecode(
                            np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR
                        )
                    except Exception:
                        frame = None
                    if frame is not None:
                        if cam_queue.full():
                            try:
                                cam_queue.get_nowait()
                            except queue.Empty:
                                pass
                        cam_queue.put_nowait(frame)
        except Exception:
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2)
            except Exception:
                pass

    @staticmethod
    def _to_qimage(bgr_frame) -> QImage:
        rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
        h, w, _ = rgb.shape
        return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()

    def _send_black_frame(self) -> None:
        black = np.zeros((240, 320, 3), dtype=np.uint8)
        ok, jpg = cv2.imencode(".jpg", black, [cv2.IMWRITE_JPEG_QUALITY, 50])
        if ok:
            self._send_media(CH_VIDEO, jpg.tobytes())

    @staticmethod
    def _synthetic_frame(idx: int) -> np.ndarray:
        """Moving test pattern used when no camera is available."""
        frame = np.zeros((360, 480, 3), dtype=np.uint8)
        t = idx % 120
        x = int(40 + t * (480 - 80) / 120)
        y = int(60 + (idx // 120) % (360 - 120))
        cv2.rectangle(frame, (x - 30, y - 30), (x + 30, y + 30), (0, 180, 255), -1)
        cv2.putText(
            frame,
            "LocalChat - no camera (test pattern)",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            1,
        )
        cv2.putText(
            frame,
            "idx=%d" % idx,
            (20, 330),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (200, 200, 200),
            1,
        )
        return frame

    # ------------------------------------------------------------- audio

    def _start_audio(self) -> None:
        if self._audio_started:
            return
        self._audio_started = True
        # Echo canceller: reference = the PCM we feed the speaker, applied to
        # the mic capture (speaker -> mic echo). A no-op until the echo path
        # has been identified, so it can never make audio worse.
        self._aec = Aec()
        # Preferred path: sounddevice (PortAudio). On some machines QtMultimedia
        # has no working backend at all (QAudioSink "succeeds" but never plays,
        # QAudioSource reports OpenError), while PortAudio reliably provides
        # capture/playback; Qt is the per-side fallback when sounddevice is not
        # installed.
        self._start_sd_audio()
        self._start_qt_audio()

    def _start_qt_audio(self) -> None:
        """QtMultimedia per-side fallback (only used when sounddevice could not
        provide that side, e.g. sounddevice is not installed)."""
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
        if self._aec is not None:
            data = self._aec.process(data)
        if self._audio_muted:
            data = b"\x00" * len(data)
        self._send_media(CH_AUDIO, data)

    def _on_remote_audio(self, data: bytes) -> None:
        with self._audio_pending_lock:
            self._audio_pending.extend(data)
            # bound the pending buffer so a network burst cannot inflate call
            # latency forever (drop the oldest bytes)
            if len(self._audio_pending) > AUDIO_PENDING_MAX:
                del self._audio_pending[:-AUDIO_PENDING_MAX]

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
                # pre-roll ~80 ms so jitter does not stutter the first words;
                # give up after 1 s if the peer never sends audio
                if have < AUDIO_PREROLL_BYTES and now - self._audio_started_at < 1.0:
                    return
                self._audio_prerolled = True
            with self._audio_pending_lock:
                if self._audio_pending:
                    chunk = bytes(self._audio_pending[:free])
                    del self._audio_pending[:free]
                else:
                    # underrun: feed a short silence so the sink does not stall
                    chunk = b"\x00" * min(free, AUDIO_SAMPLE_RATE * 2 // 20)
            if chunk:
                dev.write(chunk)
                if self._aec is not None:
                    self._aec.add_reference(chunk)
        except Exception:
            pass

    # ------------------------------------------- PortAudio (sounddevice) audio

    def _start_sd_audio(self) -> None:
        """Start sounddevice (PortAudio) capture/playback for whichever side
        QtMultimedia could not provide. The wire format is the same PCM16 mono
        16 kHz used everywhere, so the chunks plug straight into the existing
        send/receive path (native-rate devices are resampled)."""
        try:
            import sounddevice as sd
        except Exception:
            return
        if self._audio_in_dev is None and self._sd_input is None:
            dev = self._sd_pick_device(sd, want_input=True)
            if dev is not None:
                stream, rate, channels = self._sd_open(sd, dev, want_input=True)
                if stream is not None:
                    self._sd_input = stream
                    self._sd_input_rate = rate
                    self._sd_input_channels = channels
        if self._audio_out_dev is None and self._sd_output is None:
            dev = self._sd_pick_device(sd, want_input=False)
            if dev is not None:
                stream, rate, channels = self._sd_open(sd, dev, want_input=False)
                if stream is not None:
                    self._sd_output = stream
                    self._sd_output_rate = rate

    def _sd_open(self, sd, device: int, want_input: bool):
        """Open a sounddevice stream for [device]: first at the 16 kHz call
        rate, then at the device's native rate (some WASAPI/WDM-KS endpoints
        only accept their native rate; the callbacks resample). Input is
        probed for actual callbacks and re-opened stereo if the endpoint never
        delivers mono (e.g. WDM-KS stereo-mix loopbacks). Returns
        (stream, rate, channels) or (None, 0, 1)."""
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
                        # probe: some endpoints accept a mono open but never
                        # deliver frames; fall back to stereo + downmix
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
    def _sd_pick_device(sd, want_input: bool):
        """Pick a sounddevice device index: the OS default endpoint when it has
        the right channel direction, otherwise the first non-virtual device
        (Steam / virtual / loopback mixers are excluded)."""
        # test/debug override: LOCALCHAT_AUDIO_IN / LOCALCHAT_AUDIO_OUT = index
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
        for i, info in enumerate(sd.query_devices()):
            channels = (
                info["max_input_channels"] if want_input else info["max_output_channels"]
            )
            if channels > 0 and not any(
                h in info["name"].lower() for h in _VIRTUAL_AUDIO_HINTS
            ):
                return i
        return None

    @staticmethod
    def _resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
        """Linear-interpolation resample of a PCM16 mono buffer (voice-grade)."""
        if src_rate == dst_rate or not data:
            return data
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        n = max(1, int(round(len(x) * dst_rate / src_rate)))
        xp = np.linspace(0.0, 1.0, len(x), endpoint=False)
        fp = np.linspace(0.0, 1.0, n, endpoint=False)
        y = np.interp(fp, xp, x)
        return (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

    def _sd_capture_cb(self, indata, frames, time_info, status) -> None:
        """PortAudio input callback: send one 20 ms PCM16 chunk on the call."""
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
        if self._aec is not None:
            pcm = self._aec.process(pcm)
        if self._audio_muted:
            pcm = b"\x00" * len(pcm)
        self._send_media(CH_AUDIO, pcm)

    def _sd_playback_cb(self, outdata, frames, time_info, status) -> None:
        """PortAudio output callback: play the received PCM16 buffer (silence
        when nothing is queued yet)."""
        rate = self._sd_output_rate
        need = int(frames * AUDIO_SAMPLE_RATE / rate)  # 16k samples == frames @ rate
        n = need * 2  # int16 mono
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

    def _reset(self) -> None:
        """Clear call state (caller: must already hold self._lock)."""
        self._state = STATE_IDLE
        self._call_id = ""
        self._peer_id = ""
        self._peer_name = ""
        self._peer_ip = ""
        self._media_port = 0
        self._role = ""
        self._p2p = None
        self._channel_send = None
        self._identity = None
        self._my_id = ""
        self._my_name = ""
        self._media_server = None
        self._media_socket = None
        self._media_key = None

    def _end_call(self, reason: str) -> None:
        """End the call from any thread. Idempotent."""
        with self._lock:
            if self._state == STATE_IDLE:
                return
            self._state = STATE_IDLE
            self._stop_event.set()
            server, sock = self._media_server, self._media_socket
            self._reset()
        run_catching_close(server)
        run_catching_close(sock)
        if threading.current_thread() is not threading.main_thread():
            QTimer.singleShot(0, self._shutdown_engines)
        else:
            self._shutdown_engines()
        self.call_ended.emit(reason)
        self.state_changed.emit(STATE_IDLE, "", "")

    def _shutdown_engines(self) -> None:
        """Stop capture/audio resources. Runs on the GUI thread (deferred via
        a single-shot timer when the call ended on a worker thread)."""
        self._stop_event.set()
        self._stop_audio()
        thread = self._capture_thread
        self._capture_thread = None
        if thread is not None:
            thread.join(timeout=1.0)
        # stop the media sender and drop queued frames
        self._send_stop.set()
        for q in (self._audio_send_q, self._video_send_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        self._send_thread = None
        self._stop_event.clear()


def run_catching_close(sock) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass
