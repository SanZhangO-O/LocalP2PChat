"""Direct 1:1 chat page — pulled up from a member, no confirmation needed.

The chat is backed by the ViewModel's direct-chat manager; history is seeded
from storage when the chat opens and the live messages flow in over the direct
TCP session. Beyond text it carries what group chats already had: file
transfer (file_message + download server) and video calls whose signaling
rides the session socket (Android parity).
"""

import os
import sys
import time

from PyQt6.QtCore import QSize, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import audio_note
from ..models import (
    MAX_CONTENT_LENGTH,
    MAX_FOLDER_FILES,
    MAX_REPLY_PREVIEW,
    FILE_KIND_AUDIO,
    MEDIA_AUDIO,
    MEDIA_KINDS,
    Peer,
)
from ..view_model import ChatViewModel
from .chat_page import (
    HEADER_ROLE,
    MSG_ROLE,
    REACTION_CHOICES,
    CallLogEntry,
    FolderGroup,
    MessageDelegate,
    _safe_save_name,
    call_log_row,
    file_offer_expired,
    format_paused_text,
    format_transfer_detail,
    iter_message_rows,
)
from .emoji_panel import EmojiPanel
from .theme import PRIMARY, TEXT_SUBTLE
from .widgets import (
    DroppableTextEdit,
    Toast,
    date_header_text,
    format_message_time,
)


class DirectChatInput(DroppableTextEdit):
    def __init__(self, on_send, on_files_dropped=None, parent=None):
        super().__init__(on_send, on_files_dropped, parent)
        self.setPlaceholderText("输入消息...")
        self.setMaximumHeight(120)
        self.setAcceptRichText(False)


class DirectChatPage(QWidget):
    # Voice playback ended (player's monitor thread -> main thread hop).
    voice_finished = pyqtSignal()

    def __init__(self, vm: ChatViewModel, on_back):
        super().__init__()
        self.vm = vm
        self.on_back = on_back
        self._peer_id: str | None = None
        self._contact: Peer | None = None
        # The message being replied to (None when not replying).
        self._reply_target = None
        # fileId -> (status, target_path, message) for direct file messages
        self._file_states: dict = {}
        # fileId -> second progress line (已传/总大小 · 速度 · 剩余时间)
        self._file_details: dict = {}
        # fileId -> (last_time, last_received, smoothed_speed) sample
        self._file_rates: dict = {}
        # folderId -> (status, detail) for folder cards
        self._folder_states: dict = {}
        # a search jump whose message is not in the list yet: retried on the
        # next refresh. Carries (peer_id, message_id) so a re-activation of
        # the SAME chat keeps the pending target.
        self._pending_reveal = None
        self._emoji_panel = None
        # folderId -> chosen destination directory (paused folder resume)
        self._folder_targets: dict = {}
        # Voice-message recorder (shown only with sounddevice available).
        self._voice_recorder = audio_note.VoiceRecorder(
            os.path.join(self.vm.data_dir, "voice")
        )
        self._voice_timer = QTimer(self)
        self._voice_timer.setInterval(250)
        self._voice_timer.timeout.connect(self._tick_voice_recording)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 8)
        back_btn = QPushButton("← 返回")
        back_btn.setObjectName("ghost")
        back_btn.clicked.connect(self._on_back)
        header_layout.addWidget(back_btn)
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        title_col.addWidget(self.title_label)
        self.status_label = QLabel("")
        self.status_label.setObjectName("faint")
        title_col.addWidget(self.status_label)
        header_layout.addLayout(title_col, 1)
        layout.addWidget(header)

        # Offline banner: shows while the peer is unreachable — messages then
        # queue as pending and deliver automatically once they come online
        # (Android parity).
        self.banner_label = QLabel("对方未在线：消息将暂存，对方上线后自动发送")
        self.banner_label.setStyleSheet(
            "font-size: 12px; color: #B3261E; padding: 4px 16px; background: #FFF3F2;"
        )
        self.banner_label.hide()
        layout.addWidget(self.banner_label)

        # Pinned-message banner: shows the newest pin of this 1:1 chat (both
        # sides may pin; without a banner a pin had no visible effect).
        self._pinned_msg_id = None
        self.pin_bar = QFrame()
        self.pin_bar.setObjectName("replyBar")
        self.pin_bar.setStyleSheet(
            "QFrame#replyBar { background-color: #F2F1F5; border-radius: 6px; }"
        )
        pin_layout = QHBoxLayout(self.pin_bar)
        pin_layout.setContentsMargins(8, 4, 4, 4)
        pin_layout.setSpacing(6)
        self.pin_label = QLabel("")
        self.pin_label.setObjectName("faint")
        self.pin_label.setWordWrap(True)
        pin_layout.addWidget(self.pin_label, 1)
        pin_jump = QPushButton("查看")
        pin_jump.setObjectName("ghost")
        pin_jump.setFixedHeight(24)
        # clicked injects a bool into the first lambda parameter: keep it
        # explicit (AGENTS.md PyQt6 trap)
        pin_jump.clicked.connect(lambda checked=False: self._jump_to_pinned())
        pin_layout.addWidget(pin_jump)
        pin_unpin = QPushButton("✕")
        pin_unpin.setObjectName("ghost")
        pin_unpin.setFixedSize(24, 24)
        pin_unpin.setToolTip("取消置顶")
        pin_unpin.clicked.connect(lambda checked=False: self._unpin_banner())
        pin_layout.addWidget(pin_unpin)
        self.pin_bar.hide()
        layout.addWidget(self.pin_bar)

        self.model = QStandardItemModel(self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        # msg_id -> ("playing"|"idle", duration text) for voice bubbles
        self._voice_states: dict = {}
        self._voice_paths: dict = {}
        # The bubble currently playing: VoicePlayer.play() replaces the clip
        # WITHOUT firing on_finished, so the page clears the previous bubble
        # itself (otherwise it stays "playing" forever).
        self._playing_id = None
        self._voice_player = audio_note.VoicePlayer(on_finished=self._on_voice_finished)
        # msg_id -> [(emoji, actor_id), …] of the open chat, refreshed once per
        # refresh: the delegate asks on every sizeHint/paint and must not hit
        # SQLite from there
        self._reaction_rows: dict = {}
        # previous sessions' recordings can never be served again: prune
        audio_note.prune_recordings(os.path.join(self.vm.data_dir, "voice"))
        self.voice_finished.connect(self._on_voice_finished_ui)
        self.delegate = MessageDelegate(
            self.list_view,
            on_file_click=self._download_file,
            file_states=self._file_states,
            file_details=self._file_details,
            media_resolver=self._media_path,
            on_media_open=self._open_media,
            folder_states=self._folder_states,
            on_folder_click=self._download_folder,
            on_folder_open=self._open_folder,
            # direct chats show 已读/未读 on own bubbles
            show_read_state=True,
            on_call_click=self._call_back,
            reactions_provider=self._reactions_of,
            voice_states=self._voice_states,
            on_voice_click=self._toggle_voice,
            parent=self,
        )
        self.list_view.setItemDelegate(self.delegate)
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.setInterval(1800)
        self._reveal_timer.timeout.connect(self._clear_highlight)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_message_menu)
        layout.addWidget(self.list_view, 1)

        self.empty_label = QLabel("")
        self.empty_label.setStyleSheet(f"font-size: 14px; color: {TEXT_SUBTLE};")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label, 1)

        bottom = QFrame()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(14, 8, 14, 12)
        bottom_layout.setSpacing(2)
        # Reply/quote bar: hidden until the user picks 回复 on a message
        self.reply_bar = QFrame()
        self.reply_bar.setObjectName("replyBar")
        self.reply_bar.setStyleSheet(
            "QFrame#replyBar { background-color: #F2F1F5; border-radius: 6px; }"
        )
        reply_layout = QHBoxLayout(self.reply_bar)
        reply_layout.setContentsMargins(8, 4, 4, 4)
        reply_layout.setSpacing(6)
        self.reply_label = QLabel("")
        self.reply_label.setObjectName("faint")
        self.reply_label.setWordWrap(True)
        reply_layout.addWidget(self.reply_label, 1)
        self.reply_cancel_btn = QPushButton("×")
        self.reply_cancel_btn.setObjectName("ghost")
        self.reply_cancel_btn.setFixedSize(24, 24)
        self.reply_cancel_btn.setToolTip("取消回复")
        # clicked injects a bool into the first lambda parameter: keep it
        # explicit (AGENTS.md PyQt6 trap) so _clear_reply never receives it
        self.reply_cancel_btn.clicked.connect(
            lambda checked=False: self._clear_reply()
        )
        reply_layout.addWidget(self.reply_cancel_btn)
        self.reply_bar.hide()
        bottom_layout.addWidget(self.reply_bar)
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.call_btn = QPushButton("通话")
        self.call_btn.setObjectName("ghost")
        self.call_btn.setMinimumSize(64, 40)
        self.call_btn.setToolTip("视频通话")
        self.call_btn.clicked.connect(self._start_call)
        input_row.addWidget(self.call_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        self.voice_btn = QPushButton("语音")
        self.voice_btn.setObjectName("ghost")
        self.voice_btn.setMinimumSize(64, 40)
        self.voice_btn.setToolTip("语音通话")
        self.voice_btn.clicked.connect(self._start_voice_call)
        input_row.addWidget(self.voice_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        self.file_btn = QPushButton()
        if getattr(sys, "_MEIPASS", None):
            icon_path = os.path.join(
                sys._MEIPASS, "localchat", "ui", "assets", "paperclip.svg"
            )
        else:
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "assets", "paperclip.svg"
            )
        self.file_btn.setIcon(QIcon(icon_path))
        self.file_btn.setIconSize(QSize(20, 20))
        self.file_btn.setObjectName("ghost")
        self.file_btn.setFixedSize(40, 40)
        self.file_btn.setToolTip("发送文件")
        self.file_btn.clicked.connect(self._pick_file)
        input_row.addWidget(self.file_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        self.emoji_btn = QPushButton("😀")
        self.emoji_btn.setObjectName("ghost")
        self.emoji_btn.setFixedSize(40, 40)
        self.emoji_btn.setToolTip("表情")
        self.emoji_btn.clicked.connect(self._toggle_emoji_panel)
        input_row.addWidget(self.emoji_btn, alignment=Qt.AlignmentFlag.AlignBottom)

        # Voice-message recorder button (hidden without sounddevice).
        self.mic_btn = QPushButton("🎤")
        self.mic_btn.setObjectName("ghost")
        self.mic_btn.setFixedSize(40, 40)
        self.mic_btn.setToolTip("录制语音消息：点击开始，再点发送")
        self.mic_btn.clicked.connect(self._toggle_voice_recording)
        if audio_note.audio_available():
            input_row.addWidget(self.mic_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        else:
            self.mic_btn.hide()

        self.input_edit = DirectChatInput(self._send, self._send_files)
        input_row.addWidget(self.input_edit, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setMinimumSize(80, 40)
        self.send_btn.clicked.connect(self._send)
        input_row.addWidget(self.send_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        bottom_layout.addLayout(input_row)
        self.count_label = QLabel("")
        self.count_label.setObjectName("faint")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.count_label.hide()
        bottom_layout.addWidget(self.count_label)
        layout.addWidget(bottom)

        self.input_edit.textChanged.connect(self._on_input_changed)
        self.vm.direct_messages_signal.connect(self._on_messages_changed)
        self.vm.direct_contacts_signal.connect(self._on_contacts_changed)
        self.vm.direct_session_closed.connect(self._on_session_closed)
        self.vm.typing_state_changed.connect(self._refresh_status)
        self.vm.file_download_finished.connect(self._on_file_download_finished)
        self.vm.media_ready.connect(self._on_media_ready)
        self.vm.file_progress.connect(self._on_file_progress)
        self.vm.direct_chat_migrated.connect(self._on_chat_migrated)
        self.vm.call_logs_changed.connect(self._on_call_logs_changed)
        self.vm.folder_progress.connect(self._on_folder_progress)

        self.vm.folder_download_finished.connect(self._on_folder_download_finished)
        self.vm.folder_send_finished.connect(self._on_folder_send_finished)
        self.vm.folder_send_truncated.connect(self._on_folder_send_truncated)
        self.vm.extras_changed.connect(self._on_extras_changed)

    def _on_extras_changed(self, conversation_key: str):
        if self._peer_id is not None and conversation_key == "direct:" + self._peer_id:
            # reactions / pins changed (locally or by the peer): the banner
            # and the bubbles must follow
            self._refresh()

    def open_chat(self, contact: Peer) -> None:
        # switching chats abandons an in-progress recording: its 60 s
        # auto-send would otherwise go to the NEW peer
        self._abort_voice_recording()
        self._contact = contact
        self.title_label.setText(contact.name)
        self.input_edit.clear()
        self._file_states.clear()
        self._file_details.clear()
        self._file_rates.clear()
        self._folder_states.clear()
        self._voice_states.clear()
        self._voice_paths.clear()
        self._clear_reply()
        self._folder_targets.clear()
        # use the real member id returned by the handshake so messages and the
        # session line up (a manually added contact starts with a placeholder)
        peer_id = self.vm.open_direct_chat(contact)
        self._peer_id = peer_id or contact.id
        # a jump target belongs to the conversation it was requested from:
        # drop it only when a DIFFERENT chat is opened
        if (
            self._pending_reveal is not None
            and self._pending_reveal[0] != self._peer_id
        ):
            self._pending_reveal = None
        self._refresh()

    def _on_back(self):
        # The session STAYS ALIVE after leaving the page (Android parity):
        # returning must not end a video call riding this session, and a
        # session only closes on a real disconnect. Reopening the chat reuses
        # the live session.
        self._abort_voice_recording()
        if self._peer_id is not None:
            self.vm.end_direct_typing(self._peer_id)
        self._peer_id = None
        self.on_back()

    def hideEvent(self, event):
        """Leaving the page (another stack page, or the window hidden to the
        tray): abort a recording and stop off-screen animations/timers."""
        self._abort_voice_recording()
        self._reveal_timer.stop()
        self.delegate.clear_movies()
        super().hideEvent(event)

    def teardown(self):
        """App close: same cleanup as hide, plus disarm the delegate so a
        queued QMovie tick can never touch a dead viewport."""
        self._abort_voice_recording()
        self._reveal_timer.stop()
        self.delegate.teardown()

    def _on_messages_changed(self, peer_id: str) -> None:
        if peer_id == self._peer_id:
            self._refresh()

    def _on_contacts_changed(self) -> None:
        self._sync_contact_title()
        self._refresh_status()

    def _on_session_closed(self, peer_id: str) -> None:
        if peer_id == self._peer_id:
            self._refresh_status()

    def _on_call_logs_changed(self, peer_id: str) -> None:
        if peer_id == self._peer_id:
            self._refresh()

    def _refresh_status(self):
        peer_id = self._peer_id
        if peer_id is None:
            return
        alive = self.vm.direct_chat_alive(peer_id)
        typing = alive and self.vm.direct_peer_typing(peer_id)
        if typing:
            self.status_label.setText("对方正在输入…")
        else:
            self.status_label.setText("在线" if alive else "未连接")
        self.status_label.setStyleSheet(
            f"font-size: 11px; color: {PRIMARY if alive else '#6B6875'};"
        )
        self.banner_label.setVisible(not alive)
        self.call_btn.setEnabled(alive)
        self.voice_btn.setEnabled(alive)

    # ------------------------------------------------------------ reply

    def _set_reply_target(self, msg) -> None:
        """Arm the reply bar for [msg]: the next send quotes it."""
        self._reply_target = msg
        preview = (msg.content or "").replace("\n", " ").strip()
        if msg.file_info is not None and not preview:
            preview = msg.file_info.file_name
        if len(preview) > MAX_REPLY_PREVIEW:
            preview = preview[:MAX_REPLY_PREVIEW] + "…"
        self.reply_label.setText(
            f"回复 {msg.sender_name}：{preview}" if preview else f"回复 {msg.sender_name}"
        )
        self.reply_bar.show()

    def _clear_reply(self) -> None:
        self._reply_target = None
        self.reply_bar.hide()

    def _reply_payload(self):
        """(reply_to, reply_preview, reply_sender) for the armed reply, or
        (None, None, None) when not replying. The preview is capped like the
        wire field (Android parity) and falls back to the file name for a
        file message (same as the reply bar shows)."""
        target = self._reply_target
        if target is None:
            return None, None, None
        preview = (target.content or "").replace("\n", " ").strip()
        if target.file_info is not None and not preview:
            preview = target.file_info.file_name
        if len(preview) > MAX_REPLY_PREVIEW:
            preview = preview[:MAX_REPLY_PREVIEW]
        return target.id, preview, target.sender_name

    def _on_chat_migrated(self, from_id: str, to_id: str) -> None:
        # a handshake revealed the real device id for this chat (a manually
        # added contact only knew an "ip:..." placeholder): re-key so messages
        # and the session line up (Android parity)
        if self._peer_id == from_id:
            self._peer_id = to_id
            self._sync_contact_title()
            self._refresh()

    def _sync_contact_title(self):
        """Re-resolve the open chat's contact by its CURRENT key: a migration
        (placeholder -> real id) can land before the contacts list contains
        the real peer, and the contacts update that follows must heal the
        title/header instead of leaving the stale placeholder name."""
        peer_id = self._peer_id
        if peer_id is None:
            return
        contact = next(
            (c for c in self.vm.direct_contacts_list() if c.id == peer_id), None
        )
        if contact is not None and (
            self._contact is None or self._contact.id != contact.id
        ):
            self._contact = contact
            self.title_label.setText(contact.name)

    def _seed_resume_states(self, msgs) -> None:
        """Render paused cards from the persisted resume state (interrupted
        download, app restart): the offer may have no address left, but the
        resume entry carries one so the card stays clickable."""
        for m in msgs:
            if m.file_info is None or m.id in self._file_states:
                continue
            info = self.vm.resume_info(m.id)
            if not info:
                continue
            percent = int(info["percent"])
            self._file_states[m.id] = (
                "paused",
                info["target"],
                str(percent) if percent else "",
            )
            self._file_details[m.id] = format_paused_text(percent)
        for kind, payload in iter_message_rows(msgs):
            if kind != "folder":
                continue
            group = payload
            if group.is_from_me or group.folder_id in self._folder_states:
                continue
            info = self.vm.folder_resume_info(group.folder_id)
            if not info:
                continue
            self._folder_states[group.folder_id] = (
                "paused",
                f"已暂停 {info['done']}/{info['total']}（点击续传）",
            )
            self._folder_targets[group.folder_id] = info["target"]

    def _refresh(self):
        self._refresh_status()
        peer_id = self._peer_id
        if peer_id is None:
            self._refresh_pin_banner()
            return
        msgs = self.vm.direct_messages(peer_id)
        logs = [call_log_row(log) for log in self.vm.direct_call_logs(peer_id)]
        # one store read per refresh feeds every sizeHint/paint of this pass
        self._reaction_rows = self.vm.reactions_for(self._conv_key())
        self._refresh_pin_banner()
        self._seed_resume_states(msgs)
        self._seed_voice_states(msgs)
        self.model.setRowCount(0)
        self.list_view.setVisible(bool(msgs) or bool(logs))
        self.empty_label.setVisible(not msgs and not logs)
        if not msgs and not logs:
            self.empty_label.setText(
                "已连接，开始聊天吧" if self.vm.direct_chat_alive(peer_id) else "点击发送即可尝试重新连接"
            )
            return
        for kind, payload in iter_message_rows(msgs, logs):
            item = QStandardItem()
            if kind == "header":
                item.setData(date_header_text(payload), HEADER_ROLE)
            else:
                item.setData(payload, MSG_ROLE)
            self.model.appendRow(item)
        if (
            self._pending_reveal is not None
            and self.reveal_message(self._pending_reveal[1])
        ):
            return
        self.list_view.scrollToBottom()

    # ------------------------------------------------- search-result reveal

    def reveal_message(self, message_id: str) -> bool:
        """Scroll to [message_id] and flash-highlight its row (search jump).
        Returns False and remembers the target while the message is not in
        the list yet, so the next refresh retries."""
        row = self._find_row(message_id)
        if row is None:
            self._pending_reveal = (self._peer_id, message_id)
            return False
        self._pending_reveal = None
        self.list_view.scrollTo(
            self.model.index(row, 0),
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )
        self.delegate.highlight_id = message_id
        self.list_view.viewport().update()
        self._reveal_timer.start()
        return True

    def _find_row(self, message_id: str):
        for row in range(self.model.rowCount()):
            payload = self.model.item(row).data(MSG_ROLE)
            if payload is None:
                continue
            if getattr(payload, "id", None) == message_id:
                return row
            entries = getattr(payload, "entries", None)
            if entries and any(getattr(e, "id", None) == message_id for e in entries):
                return row
        return None

    def _clear_highlight(self):
        if self.delegate.highlight_id is None:
            return
        self.delegate.highlight_id = None
        self.list_view.viewport().update()

    # ----------------------------------------------------------- emoji

    def _toggle_emoji_panel(self):
        if self._emoji_panel is None:
            self._emoji_panel = EmojiPanel(self.vm.store)
            self._emoji_panel.emoji_picked.connect(self._insert_emoji)
        if self._emoji_panel.isVisible():
            self._emoji_panel.hide()
            return
        panel = self._emoji_panel
        panel.adjustSize()
        top = self.emoji_btn.mapToGlobal(self.emoji_btn.rect().topLeft())
        panel.move(top.x(), max(0, top.y() - panel.height() - 4))
        panel.show()

    def _insert_emoji(self, emoji: str):
        # insertPlainText inserts at the cursor and replaces a selection, just
        # like typing (QTextEdit has no insert())
        self.input_edit.insertPlainText(emoji)
        self.input_edit.setFocus()

    def _send(self):
        peer_id = self._peer_id
        if peer_id is None:
            return
        text = self.input_edit.toPlainText()
        if not text.strip():
            return
        if len(text) > MAX_CONTENT_LENGTH:
            # no silent truncation (Android parity): show the error state
            self.count_label.setText(f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
            return
        if self.vm.send_direct_message(peer_id, text, *self._reply_payload()):
            self.input_edit.clear()
            self._clear_reply()
        else:
            Toast(self.window()).show_message("未连接，无法发送消息")

    def _on_input_changed(self):
        self._update_send_ui()
        # composing a non-empty draft refreshes our typing indicator (the
        # ViewModel throttles it); clearing the box does not re-arm it
        peer_id = self._peer_id
        if peer_id is not None and self.input_edit.toPlainText().strip():
            self.vm.notify_direct_typing(peer_id)

    def _update_send_ui(self):
        length = len(self.input_edit.toPlainText())
        too_long = length > MAX_CONTENT_LENGTH
        self.send_btn.setEnabled(
            bool(self.input_edit.toPlainText().strip()) and not too_long
        )
        if too_long:
            self.count_label.setText(f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
        elif length > MAX_CONTENT_LENGTH - 200:
            self.count_label.setText(f"{length}/{MAX_CONTENT_LENGTH}")
            self.count_label.setStyleSheet("font-size: 11px; color: #6B6875;")
            self.count_label.show()
        else:
            self.count_label.hide()

    def _start_call(self):
        peer_id = self._peer_id
        if peer_id is None:
            return
        self.vm.start_direct_call(peer_id)

    def _start_voice_call(self):
        peer_id = self._peer_id
        if peer_id is None:
            return
        self.vm.start_direct_call(peer_id, media=MEDIA_AUDIO)

    def _call_back(self, entry: CallLogEntry):
        """Clicking a call-log line redials the peer with the same media kind."""
        peer_id = self._peer_id
        if peer_id is None:
            return
        self.vm.start_direct_call(peer_id, media=entry.media)

    def _pick_file(self):
        if self._peer_id is None:
            return
        menu = QMenu(self)
        file_action = menu.addAction("发送文件")
        folder_action = menu.addAction("发送文件夹")
        chosen = menu.exec(self.file_btn.mapToGlobal(self.file_btn.rect().bottomLeft()))
        if chosen is file_action:
            path, _ = QFileDialog.getOpenFileName(self.window(), "选择要发送的文件")
            if path:
                self._send_files([path])
        elif chosen is folder_action:
            path = QFileDialog.getExistingDirectory(self.window(), "选择要发送的文件夹")
            if path:
                self._send_files([path])

    def _send_files(self, paths):
        """Offer dropped/picked files and folders over the direct session.
        Every path is tried so one unavailable entry never swallows the rest;
        a folder is offered as one file_message per contained file. Folder
        offers are built on a worker thread (folder_send_finished reports the
        outcome) so a large folder never freezes the UI; plain files stay
        synchronous so their failure toast shows right away."""
        peer_id = self._peer_id
        if peer_id is None:
            return
        failed = False
        for path in paths:
            if os.path.isdir(path):
                self.vm.send_direct_folder(peer_id, path)
            elif not self.vm.send_direct_file(peer_id, path):
                failed = True
        if failed:
            Toast(self.window()).show_message("无法发送文件：未连接或文件不可用")

    def _on_folder_send_finished(self, ok: bool):
        if not ok:
            Toast(self.window()).show_message("无法发送文件夹：未连接或文件夹不可用")

    def _on_folder_send_truncated(self, folder_name: str):
        Toast(self.window()).show_message(
            f"文件夹超过 {MAX_FOLDER_FILES} 个文件，仅发送前 {MAX_FOLDER_FILES} 个"
        )

    # ------------------------------------------------------- folder saving

    def _download_folder(self, group: FolderGroup):
        peer_id = self._peer_id
        if peer_id is None or group.is_from_me:
            return
        state = self._folder_states.get(group.folder_id, ("idle", ""))[0]
        dest = self._folder_targets.get(group.folder_id, "")
        if state == "paused" and dest and os.path.isdir(dest):
            # resume into the remembered directory without asking again
            pass
        else:
            if group.expired:
                return
            dest = QFileDialog.getExistingDirectory(self.window(), "选择保存文件夹位置")
            if not dest:
                return
            self._folder_targets[group.folder_id] = dest
        info = self.vm.folder_resume_info(group.folder_id) if state == "paused" else None
        detail = f"{info['done']}/{info['total']}" if info else f"0/{group.total}"
        self._folder_states[group.folder_id] = ("downloading", detail)
        self.list_view.viewport().update()
        self.vm.download_direct_folder(peer_id, group.folder_id, dest)

    def _open_folder(self, group: FolderGroup):
        state = self._folder_states.get(group.folder_id, ("", ""))
        path = state[1] if state[0] == "done" else ""
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_folder_progress(self, folder_id: str, done: int, total: int):
        if folder_id not in self._folder_states:
            return
        self._folder_states[folder_id] = ("downloading", f"{done}/{total}")
        self.list_view.viewport().update()

    def _on_folder_download_finished(self, folder_id: str, ok: bool, message: str):
        if folder_id not in self._folder_states:
            return
        if ok:
            self._folder_states[folder_id] = ("done", message)
            self._folder_targets.pop(folder_id, None)
            Toast(self.window()).show_message("文件夹已保存")
        else:
            info = self.vm.folder_resume_info(folder_id)
            if info is not None:
                self._folder_states[folder_id] = (
                    "paused",
                    f"已暂停 {info['done']}/{info['total']}（点击续传）",
                )
                self._folder_targets[folder_id] = info["target"]
            elif "取消" in message:
                self._folder_states[folder_id] = ("cancelled", message)
            else:
                self._folder_states[folder_id] = ("failed", message)
                Toast(self.window()).show_message(f"文件夹保存失败：{message}")
        self.list_view.viewport().update()
        self.list_view.doItemsLayout()

    def _media_path(self, msg):
        fi = msg.file_info
        if fi is None:
            return None
        return self.vm.downloaded_media_path(fi.file_id, fi.file_name)

    def _open_media(self, path: str):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            Toast(self.window()).show_message("无法打开文件")

    def _download_file(self, msg):
        peer_id = self._peer_id
        fi = msg.file_info
        if peer_id is None or fi is None:
            return
        state = self._file_states.get(msg.id, ("idle", "", ""))
        # a paused download continues into its remembered target (the ViewModel
        # re-attaches the address/key from the persisted resume entry)
        if state[0] == "paused" and state[1]:
            self._file_details.pop(msg.id, None)
            self._file_rates.pop(msg.id, None)
            self._file_states[msg.id] = ("downloading", state[1], "")
            self.list_view.viewport().update()
            self.vm.download_direct_file(peer_id, msg.id, state[1])
            return
        if file_offer_expired(fi):
            return
        if fi.kind in MEDIA_KINDS or fi.kind == FILE_KIND_AUDIO:
            # media/voice: NO save dialog — download into the app media dir so
            # the message renders/plays inline; "另存为" in the context menu
            # saves a copy
            target = self.vm.media_target_path(fi.file_id, fi.file_name)
            if os.path.isfile(target):
                self._file_states[msg.id] = ("done", target, "")
                if fi.kind == FILE_KIND_AUDIO:
                    self._toggle_voice(msg)
                else:
                    self._open_media(target)
                return
            self._file_details.pop(msg.id, None)
            self._file_rates.pop(msg.id, None)
            self._file_states[msg.id] = ("downloading", target, "")
            self.list_view.viewport().update()
            self.vm.download_direct_file(peer_id, msg.id, target)
            return
        self._save_file_as(msg)

    def _save_file_as(self, msg):
        peer_id = self._peer_id
        fi = msg.file_info
        if peer_id is None or fi is None or file_offer_expired(fi):
            return
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(downloads, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self.window(), "保存文件", os.path.join(downloads, _safe_save_name(fi.file_name))
        )
        if not path:
            return
        self._file_details.pop(msg.id, None)
        self._file_rates.pop(msg.id, None)
        self._file_states[msg.id] = ("downloading", path, "0")
        self.list_view.viewport().update()
        self.vm.download_direct_file(peer_id, msg.id, path)

    def _on_file_progress(self, file_id: str, received: int, total: int):
        state = self._file_states.get(file_id)
        if state is None or state[0] != "downloading":
            return
        now = time.monotonic()
        prev = self._file_rates.get(file_id)
        speed = 0.0
        if prev is not None:
            elapsed = now - prev[0]
            delta = received - prev[1]
            if elapsed > 0 and delta > 0:
                instant = delta / elapsed
                speed = instant if prev[2] <= 0 else prev[2] * 0.6 + instant * 0.4
        self._file_rates[file_id] = (now, received, speed)
        if total > 0:
            percent = min(99, int(received * 100 / total))
            eta = (total - received) / speed if speed > 0 and received < total else -1
            self._file_states[file_id] = ("downloading", state[1], str(percent))
        else:
            eta = -1
        self._file_details[file_id] = format_transfer_detail(
            received, total, speed, eta
        )
        self.list_view.viewport().update()

    def _cancel_download(self, msg):
        peer_id = self._peer_id
        if peer_id is None:
            return
        self.vm.cancel_download(peer_id, msg.id)
        state = self._file_states.get(msg.id)
        if state is not None and state[0] == "downloading":
            percent = int(state[2]) if str(state[2]).isdigit() else 0
            self._file_states[msg.id] = (
                "paused",
                state[1],
                str(percent) if percent else "",
            )
            self._file_details[msg.id] = format_paused_text(percent)
            self.list_view.viewport().update()

    def _on_file_download_finished(self, file_id: str, ok: bool, message: str):
        self._file_rates.pop(file_id, None)
        if ok:
            path = self._file_states.get(file_id, ("", "", ""))[1]
            self._file_states[file_id] = ("done", path, "")
            self._file_details.pop(file_id, None)
            if path.lower().endswith(".wav"):
                # a fetched voice message becomes playable inline
                seconds = audio_note.wav_duration_seconds(path)
                self._voice_states[file_id] = (
                    "idle",
                    audio_note.format_voice_duration(seconds) or "语音",
                )
                self._voice_paths[file_id] = path
            Toast(self.window()).show_message("文件已保存")
        else:
            info = self.vm.resume_info(file_id)
            if info is not None or "取消" in message:
                percent = int(info["percent"]) if info else 0
                target = self._file_states.get(file_id, ("", "", ""))[1]
                target = target or (info["target"] if info else "")
                self._file_states[file_id] = (
                    "paused",
                    target,
                    str(percent) if percent else "",
                )
                self._file_details[file_id] = format_paused_text(percent)
            else:
                self._file_states[file_id] = ("failed", "", message)
                self._file_details.pop(file_id, None)
                Toast(self.window()).show_message(f"下载失败：{message}")
        self.list_view.viewport().update()
        # a downloaded image swaps its placeholder card for a much taller
        # inline bubble: repaint alone keeps the stale row height
        self.list_view.doItemsLayout()

    def _downloadable_file(self, msg) -> bool:
        """An offer stays download-clickable when a paused resume state exists
        (a restored offer has no address, but the resume entry does)."""
        if not file_offer_expired(msg.file_info):
            return True
        return self._file_states.get(msg.id, ("idle", "", ""))[0] == "paused"

    def _on_media_ready(self):
        """An own sent image landed in the media dir: re-render so the
        sender's own bubble flips to the inline image."""
        self.list_view.viewport().update()
        self.list_view.doItemsLayout()

    def _show_message_menu(self, pos):
        index = self.list_view.indexAt(pos)
        msg = index.data(MSG_ROLE) if index.isValid() else None
        if msg is None:
            return
        if isinstance(msg, CallLogEntry):
            # call-log lines are not messages: no copy/forward/delete menu
            return
        if isinstance(msg, FolderGroup):
            self._show_folder_menu(msg, pos)
            return
        menu = QMenu(self.list_view)
        if msg.file_info is not None:
            is_media = msg.file_info.kind in MEDIA_KINDS
            is_voice = msg.file_info.kind == FILE_KIND_AUDIO
            open_action = None
            if is_media:
                local = self._media_path(msg)
                if local:
                    open_action = menu.addAction("打开")
            download_action = None
            cancel_action = None
            dl_state = self._file_states.get(msg.id, ("idle", "", ""))[0]
            if is_voice and self._voice_paths.get(msg.id):
                pass  # already playable inline; no download entry
            elif self._downloadable_file(msg):
                if dl_state == "downloading":
                    # an in-flight download offers 取消下载 instead of starting
                    # a second one
                    cancel_action = menu.addAction("取消下载")
                elif is_media or is_voice:
                    download_action = menu.addAction("另存为...")
                else:
                    download_action = menu.addAction(
                        "续传 / 另存为" if dl_state == "paused" else "下载 / 另存为"
                    )
            copy_name_action = menu.addAction("复制文件名")
            reaction_menu = menu.addMenu("回应")
            self._fill_reactions(reaction_menu, msg)
            menu.addSeparator()
            pin_action = menu.addAction(
                "取消置顶" if self._is_pinned(msg.id) else "置顶"
            )
            delete_action = None
            if msg.is_from_me:
                menu.addSeparator()
                delete_action = menu.addAction("删除")
            chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
            if open_action is not None and chosen is open_action:
                self._open_media(self._media_path(msg))
            elif download_action is not None and chosen is download_action:
                self._save_file_as(msg)
            elif cancel_action is not None and chosen is cancel_action:
                self._cancel_download(msg)
            elif chosen is copy_name_action:
                QApplication.clipboard().setText(msg.file_info.file_name)
            elif chosen is pin_action:
                self.vm.toggle_direct_pin(self._peer_id, msg.id, not self._is_pinned(msg.id))
            elif chosen is delete_action:
                self._delete(msg)
            return
        reply_action = menu.addAction("回复")
        reaction_menu = menu.addMenu("回应")
        self._fill_reactions(reaction_menu, msg)
        copy_action = menu.addAction("复制")
        edit_action = None
        if msg.is_from_me and not msg.pending and self.vm.direct_chat_alive(self._peer_id or ""):
            edit_action = menu.addAction("编辑")
        pin_action = menu.addAction(
            "取消置顶" if self._is_pinned(msg.id) else "置顶"
        )
        delete_action = None
        if msg.is_from_me:
            menu.addSeparator()
            delete_action = menu.addAction("删除")
        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen is reply_action:
            self._set_reply_target(msg)
        elif chosen is copy_action:
            QApplication.clipboard().setText(msg.content)
        elif edit_action is not None and chosen is edit_action:
            self._edit_message_dialog(msg)
        elif chosen is pin_action:
            self.vm.toggle_direct_pin(self._peer_id, msg.id, not self._is_pinned(msg.id))
        elif chosen is delete_action:
            self._delete(msg)

    # ------------------------------------------- reactions/pin/edit/voice

    def _conv_key(self) -> str:
        return "direct:" + (self._peer_id or "")

    def _reactions_of(self, msg):
        """Delegate callback: reads the per-refresh cache only (runs inside
        sizeHint/paint)."""
        data = self._reaction_rows.get(msg.id)
        if not data:
            return None
        counts: dict = {}
        order: list = []
        for emoji, _actor in data:
            if emoji not in counts:
                counts[emoji] = 0
                order.append(emoji)
            counts[emoji] += 1
        return [(emoji, counts[emoji]) for emoji in order]

    def _fill_reactions(self, menu: QMenu, msg):
        mine = set()
        for emoji, actor in self.vm.reactions_for(self._conv_key()).get(msg.id, ()):
            if actor == self.vm.my_device_id:
                mine.add(emoji)
        for emoji in REACTION_CHOICES:
            label = f"{emoji} 取消回应" if emoji in mine else emoji
            action = menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, m=msg, e=emoji, on=e not in mine: self._react(m, e, on)
            )

    def _react(self, msg, emoji: str, active: bool):
        if self._peer_id is None:
            return
        if not self.vm.toggle_direct_reaction(self._peer_id, msg.id, emoji, active):
            Toast(self.window()).show_message("回应未发送：对方未在线")

    def _is_pinned(self, message_id: str) -> bool:
        return any(
            mid == message_id
            for mid, _at, _by in self.vm.pinned_for(self._conv_key())
        )

    def _refresh_pin_banner(self):
        """Newest pin of this chat feeds the banner (mirrors the group page);
        hidden when nothing is pinned."""
        peer_id = self._peer_id
        if peer_id is None:
            self._pinned_msg_id = None
            self.pin_bar.hide()
            return
        pins = self.vm.pinned_for("direct:" + peer_id)
        if not pins:
            self._pinned_msg_id = None
            self.pin_bar.hide()
            return
        msg_id, _at, by = pins[-1]  # newest pin wins the banner slot
        preview = ""
        for m in self.vm.direct_messages(peer_id):
            if m.id == msg_id:
                preview = (m.content or "").replace("\n", " ").strip()
                if m.file_info is not None and not preview:
                    preview = m.file_info.file_name
                break
        if not preview:
            preview = "（消息不在本地记录中）"
        if len(preview) > 40:
            preview = preview[:40] + "…"
        who = f"{by} " if by else ""
        self.pin_label.setText(f"📌 {who}置顶：{preview}")
        self._pinned_msg_id = msg_id
        self.pin_bar.show()

    def _jump_to_pinned(self):
        if self._pinned_msg_id:
            self.reveal_message(self._pinned_msg_id)

    def _unpin_banner(self):
        peer_id = self._peer_id
        if peer_id is not None and self._pinned_msg_id:
            self.vm.toggle_direct_pin(peer_id, self._pinned_msg_id, False)

    def _edit_message_dialog(self, msg):
        text, ok = QInputDialog.getMultiLineText(
            self.window(), "编辑消息", "新的内容：", msg.content
        )
        if not ok:
            return
        text = text.strip()
        if not text:
            return
        if len(text) > MAX_CONTENT_LENGTH:
            Toast(self.window()).show_message(
                f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）"
            )
            return
        if not self.vm.edit_direct_message(self._peer_id, msg.id, text):
            Toast(self.window()).show_message("编辑未发送：对方未在线")

    def _toggle_voice(self, msg):
        state = self._voice_states.get(msg.id, ("idle", ""))[0]
        if state == "playing":
            # stop() suppresses on_finished (generation bump), so clear the
            # state here instead of waiting for a callback that never comes
            self._voice_player.stop()
            self._playing_id = None
            self._clear_playing_states()
            self.list_view.viewport().update()
            return
        path = self._voice_paths.get(msg.id)
        if not path:
            self._download_file(msg)
            return
        if not audio_note.audio_available():
            self._open_media(path)
            return
        if self._voice_player.play(path):
            # play() replaced the previous clip silently: clear the stale
            # "playing" bubble or clicking it would kill the audible one
            self._clear_playing_states()
            self._playing_id = msg.id
            self._voice_states[msg.id] = ("playing", self._voice_states.get(msg.id, ("idle", ""))[1])
        else:
            Toast(self.window()).show_message("无法播放语音")
            self._open_media(path)
        self.list_view.viewport().update()

    def _clear_playing_states(self):
        for msg_id, state in list(self._voice_states.items()):
            if state[0] == "playing":
                self._voice_states[msg_id] = ("idle", state[1])

    def _on_voice_finished(self):
        self.voice_finished.emit()

    def _on_voice_finished_ui(self):
        self._playing_id = None
        self._clear_playing_states()
        self.list_view.viewport().update()

    def _seed_voice_states(self, msgs):
        for m in msgs:
            if m.file_info is None or m.file_info.kind != FILE_KIND_AUDIO:
                continue
            if m.id in self._voice_states:
                continue
            path = self._media_path(m)
            if path:
                seconds = audio_note.wav_duration_seconds(path)
                self._voice_states[m.id] = ("idle", audio_note.format_voice_duration(seconds))
                self._voice_paths[m.id] = path
            else:
                self._voice_states[m.id] = ("idle", "语音")

    def _toggle_voice_recording(self):
        if self._voice_recorder.recording:
            self._stop_voice_recording()
            return
        if self._peer_id is None or not self.vm.direct_chat_alive(self._peer_id):
            Toast(self.window()).show_message("对方未在线，语音消息需在线发送")
            return
        if self._voice_recorder.start():
            self.mic_btn.setText("■")
            self._voice_timer.start()

    def _tick_voice_recording(self):
        if not self._voice_recorder.recording:
            self._voice_timer.stop()
            return
        seconds = self._voice_recorder.elapsed_seconds()
        if seconds >= 60:
            self._stop_voice_recording()
            return
        self.mic_btn.setText(f"{seconds}s")

    def _stop_voice_recording(self):
        self._voice_timer.stop()
        path = self._voice_recorder.stop()
        self.mic_btn.setText("🎤")
        if not path:
            return
        if not self.vm.send_direct_file(self._peer_id, path):
            Toast(self.window()).show_message("无法发送语音：对方未在线")
            try:
                os.remove(path)
            except OSError:
                pass

    def _abort_voice_recording(self):
        """Abandon an in-progress recording (chat switched / page hidden):
        stop the timer and DISCARD the clip instead of sending it to whatever
        peer happens to be open when the 60 s cap fires."""
        if not self._voice_timer.isActive() and not self._voice_recorder.recording:
            return
        self._voice_timer.stop()
        self._voice_recorder.cancel()
        self.mic_btn.setText("🎤")

    def _show_folder_menu(self, group: FolderGroup, pos):
        menu = QMenu(self.list_view)
        state = self._folder_states.get(group.folder_id, ("idle", ""))[0]
        save_action = None
        cancel_action = None
        if not group.is_from_me and (not group.expired or state == "paused"):
            if state == "downloading":
                cancel_action = menu.addAction("取消下载")
            else:
                save_action = menu.addAction(
                    "继续保存" if state == "paused" else "保存文件夹..."
                )
        copy_action = menu.addAction("复制文件夹名")
        delete_action = None
        if group.is_from_me:
            menu.addSeparator()
            delete_action = menu.addAction("删除")
        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen is save_action:
            self._download_folder(group)
        elif chosen is cancel_action:
            self._cancel_folder(group)
        elif chosen is copy_action:
            QApplication.clipboard().setText(group.folder_name)
        elif chosen is delete_action:
            self._confirm_delete_folder(group)

    def _cancel_folder(self, group: FolderGroup):
        peer_id = self._peer_id
        if peer_id is None:
            return
        self.vm.cancel_download(peer_id, group.folder_id)
        # the worker keeps every ".part": show the paused card immediately
        self._folder_states[group.folder_id] = ("paused", "已暂停（点击续传）")
        self.list_view.viewport().update()

    def _confirm_delete_folder(self, group: FolderGroup):
        peer_id = self._peer_id
        if peer_id is None:
            return
        box = QMessageBox(self.window())
        box.setWindowTitle("删除文件夹")
        box.setText("删除后，这个文件夹的所有消息会从双方的聊天记录中移除，且无法恢复。")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_btn:
            for entry in list(group.entries):
                self.vm.delete_direct_message(peer_id, entry.id, entry.sender_id)

    def _delete(self, msg):
        peer_id = self._peer_id
        if peer_id is None:
            return
        box = QMessageBox(self.window())
        box.setWindowTitle("删除消息")
        box.setText("删除后，这条消息会从双方的聊天记录中移除，且无法恢复。是否删除？")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_btn:
            self.vm.delete_direct_message(peer_id, msg.id, msg.sender_id)
