"""Video-call UI: incoming-call dialog and the in-call window.

The in-call window renders the remote video (large) and the mirrored local
preview (small corner overlay), shows call status/duration and exposes
mute-audio / mute-video / hangup controls.
"""

import time

from PyQt6.QtCore import QSize, Qt, QTimer
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .theme import ERROR, PRIMARY, SURFACE_VARIANT, TEXT_SUBTLE


class IncomingCallDialog(QDialog):
    """Ringing dialog shown when a call_offer arrives."""

    def __init__(self, caller_name: str, on_accept, on_reject, parent=None):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle("视频通话邀请")
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)

        title = QLabel("视频通话邀请")
        title.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {PRIMARY};")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        detail = QLabel(f"<b>{caller_name.toHtmlEscaped()}</b> 邀请你进行视频通话")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        hint = QLabel("接听后即可开始通话")
        hint.setObjectName("faint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        accept_btn = QPushButton("接听")
        accept_btn.setStyleSheet("background: #2E7D32; color: white;")
        accept_btn.setMinimumWidth(120)
        accept_btn.clicked.connect(self._on_accept)
        buttons.addWidget(accept_btn)
        reject_btn = QPushButton("拒绝")
        reject_btn.setObjectName("danger")
        reject_btn.setMinimumWidth(120)
        reject_btn.clicked.connect(self._on_reject)
        buttons.addWidget(reject_btn)
        layout.addLayout(buttons)

        self._on_accept_cb = on_accept
        self._on_reject_cb = on_reject
        self._done = False

    def _on_accept(self):
        if self._done:
            return
        self._done = True
        self.accept()
        self._on_accept_cb()

    def _on_reject(self):
        if self._done:
            return
        self._done = True
        self.reject()
        self._on_reject_cb()

    def closeEvent(self, event):
        # closing the dialog (e.g. Esc) counts as declining
        if not self._done:
            self._done = True
            self._on_reject_cb()
        super().closeEvent(event)


class _VideoLabel(QLabel):
    """A video surface that scales incoming QImages to fit."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            f"background: #141318; color: #8A8794; border-radius: 12px; font-size: 13px;"
        )
        self._pixmap = QPixmap()
        self.setText(placeholder)

    def show_image(self, qimage) -> None:
        pm = QPixmap.fromImage(qimage)
        self._pixmap = pm
        self.setText("")
        self._update_scaled()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scaled()

    def _update_scaled(self) -> None:
        if self._pixmap.isNull():
            return
        target = self.size() - QSize(8, 8)
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)


class CallWindow(QDialog):
    """In-call window: remote video, local preview, mute and hangup controls."""

    def __init__(self, call_manager, parent=None):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle("视频通话")
        self.setMinimumSize(680, 520)
        self.resize(820, 620)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._call_manager = call_manager
        self._audio_muted = False
        self._video_muted = False
        self._connected_at = None
        self._close_handled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.status_label = QLabel("正在连接...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(f"font-size: 14px; color: {TEXT_SUBTLE};")
        layout.addWidget(self.status_label)

        video_area = QHBoxLayout()
        video_area.setSpacing(10)
        self.remote_label = _VideoLabel("等待对方视频...")
        video_area.addWidget(self.remote_label, 1)
        self.local_label = _VideoLabel("")
        self.local_label.setFixedSize(200, 150)
        video_area.addWidget(self.local_label, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(video_area, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()
        self.audio_btn = QPushButton("静音")
        self.audio_btn.setObjectName("outline")
        self.audio_btn.setMinimumWidth(96)
        self.audio_btn.clicked.connect(self._toggle_audio)
        buttons.addWidget(self.audio_btn)
        self.video_btn = QPushButton("关闭摄像头")
        self.video_btn.setObjectName("outline")
        self.video_btn.setMinimumWidth(120)
        self.video_btn.clicked.connect(self._toggle_video)
        buttons.addWidget(self.video_btn)
        self.camera_btn = QPushButton("切换摄像头")
        self.camera_btn.setObjectName("outline")
        self.camera_btn.setMinimumWidth(120)
        self.camera_btn.setToolTip("在可用的摄像头之间切换（如笔记本内置与 USB 摄像头）")
        self.camera_btn.clicked.connect(self._toggle_camera)
        buttons.addWidget(self.camera_btn)
        self.hangup_btn = QPushButton("挂断")
        self.hangup_btn.setStyleSheet(
            f"background: {ERROR}; color: white; font-weight: 700;"
        )
        self.hangup_btn.setMinimumWidth(120)
        self.hangup_btn.clicked.connect(self._hangup)
        buttons.addWidget(self.hangup_btn)
        buttons.addStretch()
        layout.addLayout(buttons)

        self._duration_timer = QTimer(self)
        self._duration_timer.timeout.connect(self._update_status)
        self._duration_timer.start(1000)

        # media signals -> slots (queued from worker threads)
        call_manager.remote_frame.connect(self._on_remote_frame)
        call_manager.local_frame.connect(self._on_local_frame)
        call_manager.state_changed.connect(self._on_state_changed)
        call_manager.call_ended.connect(self._on_call_ended)
        call_manager.call_error.connect(self._on_call_error)

    def _on_remote_frame(self, qimage) -> None:
        self.remote_label.show_image(qimage)

    def _on_local_frame(self, qimage) -> None:
        self.local_label.show_image(qimage)

    def _on_state_changed(self, state: str, peer_name: str, detail: str) -> None:
        if state == "active":
            if self._connected_at is None:
                self._connected_at = time.monotonic()
            self.status_label.setText(f"与 {peer_name} 通话中")
        elif state in ("outgoing", "incoming"):
            self._connected_at = None
            self.status_label.setText(f"正在呼叫 {peer_name}...")
        elif state == "idle":
            self._connected_at = None

    def _update_status(self) -> None:
        if self._connected_at is None:
            return
        elapsed = int(time.monotonic() - self._connected_at)
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        if hh:
            text = f"{hh:02d}:{mm:02d}:{ss:02d}"
        else:
            text = f"{mm:02d}:{ss:02d}"
        self.status_label.setText(f"通话中 · {text}")

    def _toggle_audio(self) -> None:
        self._audio_muted = not self._audio_muted
        self._call_manager.set_audio_muted(self._audio_muted)
        self.audio_btn.setText("取消静音" if self._audio_muted else "静音")

    def _toggle_video(self) -> None:
        self._video_muted = not self._video_muted
        self._call_manager.set_video_muted(self._video_muted)
        self.video_btn.setText("开启摄像头" if self._video_muted else "关闭摄像头")
        if self._video_muted:
            self.local_label.clear()

    def _toggle_camera(self) -> None:
        self._call_manager.switch_camera()
        idx = self._call_manager.camera_index
        self.camera_btn.setText(f"切换到摄像头 {2 - idx}")

    def _hangup(self) -> None:
        self._call_manager.hangup()
        self.close()

    def _on_call_ended(self, reason: str) -> None:
        self.close()

    def _on_call_error(self, message: str) -> None:
        self.close()

    def closeEvent(self, event):
        # Closing the window (including the title-bar X) ends the call instead
        # of leaving camera/mic/media running invisibly in the background.
        if self._close_handled:
            super().closeEvent(event)
            return
        self._close_handled = True
        if self._call_manager.state != "idle":
            self._call_manager.hangup()
        self._call_manager.remote_frame.disconnect(self._on_remote_frame)
        self._call_manager.local_frame.disconnect(self._on_local_frame)
        self._call_manager.state_changed.disconnect(self._on_state_changed)
        self._call_manager.call_ended.disconnect(self._on_call_ended)
        self._call_manager.call_error.disconnect(self._on_call_error)
        self._duration_timer.stop()
        super().closeEvent(event)
