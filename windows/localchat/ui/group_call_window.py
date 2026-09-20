"""Group voice conference UI: invite (ring) dialog and the meeting panel.

The meeting panel shows the participant list (host first), the elapsed time
and mute / hang-up controls. It follows the CallWindow lifecycle pattern:
closing the window ends the meeting, every manager signal is disconnected in
closeEvent and the duration timer is stopped (docs/LESSONS.md — pages must
stop every resource they own).
"""

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from .theme import ERROR, PRIMARY, TEXT_SUBTLE


class GroupCallInviteDialog(QDialog):
    """Ringing dialog for a group_call_invite ("ring - join")."""

    def __init__(self, host_name: str, on_accept, on_reject, parent=None):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle("语音会议邀请")
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 18)
        layout.setSpacing(10)

        title = QLabel("语音会议邀请")
        title.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {PRIMARY};")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        detail = QLabel(f"<b>{host_name.toHtmlEscaped()}</b> 邀请你加入群组语音会议")
        detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.setWordWrap(True)
        layout.addWidget(detail)

        hint = QLabel("加入后将进入多人语音")
        hint.setObjectName("faint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        accept_btn = QPushButton("加入")
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


class GroupCallWindow(QDialog):
    """In-meeting panel: participants, duration, mute and hang-up."""

    def __init__(self, group_call, parent=None):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle("语音会议")
        self.setMinimumSize(360, 420)
        self.resize(400, 480)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self._group_call = group_call
        self._audio_muted = False
        self._connected_at = None
        self._close_handled = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.status_label = QLabel("正在等待成员加入...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(f"font-size: 14px; color: {TEXT_SUBTLE};")
        layout.addWidget(self.status_label)

        list_title = QLabel("参会成员")
        list_title.setObjectName("faint")
        layout.addWidget(list_title)

        self.member_list = QListWidget()
        self.member_list.setSpacing(2)
        layout.addWidget(self.member_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()
        self.audio_btn = QPushButton("静音")
        self.audio_btn.setObjectName("outline")
        self.audio_btn.setMinimumWidth(96)
        self.audio_btn.clicked.connect(self._toggle_audio)
        buttons.addWidget(self.audio_btn)
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

        group_call.state_changed.connect(self._on_state_changed)
        group_call.participants_changed.connect(self._on_participants)
        group_call.call_ended.connect(self._on_call_ended)
        group_call.call_error.connect(self._on_call_error)
        self._on_participants(group_call.participants())

    def _on_state_changed(self, state: str, title: str, detail: str) -> None:
        if state == "active":
            if self._connected_at is None:
                self._connected_at = time.monotonic()
        elif state in ("outgoing", "incoming"):
            self._connected_at = None
            if state == "incoming":
                self.status_label.setText(f"{title} 邀请你加入语音会议")
            else:
                self.status_label.setText("正在邀请成员加入...")

    def _on_participants(self, participants) -> None:
        self.member_list.clear()
        for p in participants or []:
            name = str(p.get("name") or p.get("id") or "?")
            if p.get("self"):
                name = f"{name}（我）"
            item = QListWidgetItem(name)
            self.member_list.addItem(item)
        if participants:
            self.status_label.setText(f"语音会议中 · {len(participants)} 人")
            if self._connected_at is None:
                self._connected_at = time.monotonic()
            self._update_status()

    def _update_status(self) -> None:
        if self._connected_at is None:
            return
        elapsed = int(time.monotonic() - self._connected_at)
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        clock = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        count = self.member_list.count()
        base = f"语音会议中 · {count} 人" if count else "语音会议中"
        self.status_label.setText(f"{base} · {clock}")

    def _toggle_audio(self) -> None:
        self._audio_muted = not self._audio_muted
        self._group_call.set_audio_muted(self._audio_muted)
        self.audio_btn.setText("取消静音" if self._audio_muted else "静音")

    def _hangup(self) -> None:
        self._group_call.leave_meeting()
        self.close()

    def _on_call_ended(self, reason: str) -> None:
        self.close()

    def _on_call_error(self, message: str) -> None:
        self.close()

    def closeEvent(self, event):
        # Closing the window (including the title-bar X) ends the meeting
        # instead of leaving the mic/media running invisibly in the
        # background (CallWindow lifecycle parity).
        if self._close_handled:
            super().closeEvent(event)
            return
        self._close_handled = True
        if self._group_call.state != "idle":
            self._group_call.leave_meeting()
        self._group_call.state_changed.disconnect(self._on_state_changed)
        self._group_call.participants_changed.disconnect(self._on_participants)
        self._group_call.call_ended.disconnect(self._on_call_ended)
        self._group_call.call_error.disconnect(self._on_call_error)
        self._duration_timer.stop()
        super().closeEvent(event)
