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

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import MAX_CONTENT_LENGTH, Peer
from ..view_model import ChatViewModel
from .chat_page import HEADER_ROLE, MSG_ROLE, MessageDelegate, file_offer_expired
from .theme import PRIMARY, TEXT_SUBTLE
from .widgets import Toast, date_header_text, format_message_time, is_same_day


class DirectChatInput(QTextEdit):
    def __init__(self, on_send, parent=None):
        super().__init__(parent)
        self.on_send = on_send
        self.setPlaceholderText("输入消息...")
        self.setMaximumHeight(120)
        self.setAcceptRichText(False)

    def keyPressEvent(self, event):
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.on_send()
            return
        super().keyPressEvent(event)


class DirectChatPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_back):
        super().__init__()
        self.vm = vm
        self.on_back = on_back
        self._peer_id: str | None = None
        self._contact: Peer | None = None
        # fileId -> (status, target_path, message) for direct file messages
        self._file_states: dict = {}

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

        self.model = QStandardItemModel(self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(
            MessageDelegate(
                self.list_view,
                on_file_click=self._download_file,
                file_states=self._file_states,
                parent=self,
            )
        )
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
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self.call_btn = QPushButton("通话")
        self.call_btn.setObjectName("ghost")
        self.call_btn.setMinimumSize(64, 40)
        self.call_btn.setToolTip("视频通话")
        self.call_btn.clicked.connect(self._start_call)
        input_row.addWidget(self.call_btn, alignment=Qt.AlignmentFlag.AlignBottom)

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

        self.input_edit = DirectChatInput(self._send)
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

        self.input_edit.textChanged.connect(self._update_send_ui)
        self.vm.direct_messages_signal.connect(self._on_messages_changed)
        self.vm.direct_contacts_signal.connect(self._on_contacts_changed)
        self.vm.direct_session_closed.connect(self._on_session_closed)
        self.vm.file_download_finished.connect(self._on_file_download_finished)
        self.vm.direct_chat_migrated.connect(self._on_chat_migrated)

    def open_chat(self, contact: Peer) -> None:
        self._contact = contact
        self.title_label.setText(contact.name)
        self.input_edit.clear()
        self._file_states.clear()
        # use the real member id returned by the handshake so messages and the
        # session line up (a manually added contact starts with a placeholder)
        peer_id = self.vm.open_direct_chat(contact)
        self._peer_id = peer_id or contact.id
        self._refresh()

    def _on_back(self):
        # The session STAYS ALIVE after leaving the page (Android parity):
        # returning must not end a video call riding this session, and a
        # session only closes on a real disconnect. Reopening the chat reuses
        # the live session.
        self._peer_id = None
        self.on_back()

    def _on_messages_changed(self, peer_id: str) -> None:
        if peer_id == self._peer_id:
            self._refresh()

    def _on_contacts_changed(self) -> None:
        self._sync_contact_title()
        self._refresh_status()

    def _on_session_closed(self, peer_id: str) -> None:
        if peer_id == self._peer_id:
            self._refresh_status()

    def _refresh_status(self):
        peer_id = self._peer_id
        if peer_id is None:
            return
        alive = self.vm.direct_chat_alive(peer_id)
        self.status_label.setText("在线" if alive else "未连接")
        self.status_label.setStyleSheet(
            f"font-size: 11px; color: {PRIMARY if alive else '#6B6875'};"
        )
        self.banner_label.setVisible(not alive)
        self.call_btn.setEnabled(alive)

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

    def _refresh(self):
        self._refresh_status()
        peer_id = self._peer_id
        if peer_id is None:
            return
        msgs = self.vm.direct_messages(peer_id)
        self.model.setRowCount(0)
        self.list_view.setVisible(bool(msgs))
        self.empty_label.setVisible(not msgs)
        if not msgs:
            self.empty_label.setText(
                "已连接，开始聊天吧" if self.vm.direct_chat_alive(peer_id) else "点击发送即可尝试重新连接"
            )
            return
        prev_day = None
        for msg in msgs:
            if prev_day is None or not is_same_day(prev_day, msg.timestamp):
                header = QStandardItem()
                header.setData(date_header_text(msg.timestamp), HEADER_ROLE)
                self.model.appendRow(header)
                prev_day = msg.timestamp
            item = QStandardItem()
            item.setData(msg, MSG_ROLE)
            self.model.appendRow(item)
        self.list_view.scrollToBottom()

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
        if self.vm.send_direct_message(peer_id, text):
            self.input_edit.clear()
        else:
            Toast(self.window()).show_message("未连接，无法发送消息")

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

    def _pick_file(self):
        peer_id = self._peer_id
        if peer_id is None:
            return
        path, _ = QFileDialog.getOpenFileName(self.window(), "选择要发送的文件")
        if not path:
            return
        if not self.vm.send_direct_file(peer_id, path):
            Toast(self.window()).show_message("无法发送文件：未连接或文件不可用")

    def _download_file(self, msg):
        peer_id = self._peer_id
        fi = msg.file_info
        if peer_id is None or fi is None or file_offer_expired(fi):
            return
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(downloads, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self.window(), "保存文件", os.path.join(downloads, fi.file_name)
        )
        if not path:
            return
        self._file_states[msg.id] = ("downloading", path, "")
        self.list_view.viewport().update()
        self.vm.download_direct_file(peer_id, msg.id, path)

    def _on_file_download_finished(self, file_id: str, ok: bool, message: str):
        if ok:
            path = self._file_states.get(file_id, ("", "", ""))[1]
            self._file_states[file_id] = ("done", path, "")
            Toast(self.window()).show_message("文件已保存")
        else:
            self._file_states[file_id] = ("failed", "", message)
            Toast(self.window()).show_message(f"下载失败：{message}")
        self.list_view.viewport().update()

    def _show_message_menu(self, pos):
        index = self.list_view.indexAt(pos)
        msg = index.data(MSG_ROLE) if index.isValid() else None
        if msg is None:
            return
        menu = QMenu(self.list_view)
        if msg.file_info is not None:
            download_action = None
            if not file_offer_expired(msg.file_info):
                download_action = menu.addAction("下载 / 另存为")
            copy_name_action = menu.addAction("复制文件名")
            delete_action = None
            if msg.is_from_me:
                menu.addSeparator()
                delete_action = menu.addAction("删除")
            chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
            if download_action is not None and chosen is download_action:
                self._download_file(msg)
            elif chosen is copy_name_action:
                QApplication.clipboard().setText(msg.file_info.file_name)
            elif chosen is delete_action:
                self._delete(msg)
            return
        copy_action = menu.addAction("复制")
        delete_action = None
        if msg.is_from_me:
            menu.addSeparator()
            delete_action = menu.addAction("删除")
        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen is copy_action:
            QApplication.clipboard().setText(msg.content)
        elif chosen is delete_action:
            self._delete(msg)

    def _delete(self, msg):
        peer_id = self._peer_id
        if peer_id is None:
            return
        self.vm.delete_direct_message(peer_id, msg.id, msg.sender_id)
