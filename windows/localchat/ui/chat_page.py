import os
import sys

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..models import MAX_CONTENT_LENGTH, ChatMessage
from ..view_model import ChatViewModel
from .theme import (
    BUBBLE_MINE,
    BUBBLE_NAME,
    BUBBLE_OTHER,
    BUBBLE_TEXT_OTHER,
    PRIMARY,
    bubble_path,
)
from .widgets import Toast, date_header_text, format_message_time, is_same_day

MSG_ROLE = Qt.ItemDataRole.UserRole + 1
HEADER_ROLE = Qt.ItemDataRole.UserRole + 2

H_PAD = 12
V_PAD = 8
TIME_H = 14
NAME_H = 17
SIDE_MARGIN = 12
FILE_CARD_H = 66


def format_file_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def file_offer_expired(fi) -> bool:
    """A restored file offer is expired: the download address was captured in
    a previous session and the sender's server died with its process (blanked
    by the ViewModel on restore), so a download can no longer succeed."""
    return fi is None or not fi.download_host or fi.download_port <= 0


class MessageDelegate(QStyledItemDelegate):
    def __init__(self, view, on_file_click=None, file_states=None, parent=None):
        super().__init__(parent)
        self._view = view
        self.on_file_click = on_file_click
        self.file_states = file_states if file_states is not None else {}

    def _layout(self, msg: ChatMessage, max_bubble_w: int) -> tuple:
        if msg.file_info is not None:
            # fixed-size file card; width capped so the card never dominates
            return min(max_bubble_w, 320), FILE_CARD_H, None
        font = self._view.font()
        font.setPointSize(10)
        fm = QFontMetrics(font)
        limit_w = max(max_bubble_w - 2 * H_PAD, 60)
        bounding = fm.boundingRect(
            QRect(0, 0, limit_w, 10000),
            Qt.TextFlag.TextWordWrap,
            msg.content,
        )
        bubble_w = min(bounding.width(), limit_w) + 2 * H_PAD
        # the bottom-row label ("待送达 · HH:MM") shares the bubble's inner
        # width with the content: never let the bubble be narrower than the
        # label itself, or a short pending message clips it
        time_font = QFont(font)
        time_font.setPointSize(7)
        label_w = QFontMetrics(time_font).horizontalAdvance(self._time_label(msg))
        bubble_w = max(bubble_w, label_w + 2 * H_PAD)
        height = bounding.height() + 2 * V_PAD + TIME_H + 2
        if not msg.is_from_me:
            height += NAME_H
        return bubble_w, height, bounding

    def sizeHint(self, option, index) -> QSize:
        if index.data(HEADER_ROLE) is not None:
            return QSize(100, 28)
        msg = index.data(MSG_ROLE)
        if msg is None:
            return QSize(100, 40)
        view_w = self._view.viewport().width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, height, _ = self._layout(msg, max_bubble_w)
        return QSize(max_bubble_w + 2 * SIDE_MARGIN, height + 2 * V_PAD)

    def editorEvent(self, event, model, option, index):
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            msg = index.data(MSG_ROLE)
            if msg is not None and msg.file_info is not None and self.on_file_click:
                if file_offer_expired(msg.file_info):
                    return True  # expired offers are not clickable
                state = self.file_states.get(msg.id, ("idle", "", ""))[0]
                if state != "downloading":
                    self.on_file_click(msg)
                return True
        return super().editorEvent(event, model, option, index)

    def paint(self, painter: QPainter, option, index) -> None:
        header_text = index.data(HEADER_ROLE)
        if header_text is not None:
            self._paint_header(painter, option, header_text)
            return
        msg = index.data(MSG_ROLE)
        if msg is None:
            return
        if msg.file_info is not None:
            self._paint_file_message(painter, option, msg)
        else:
            self._paint_message(painter, option, msg)

    def _paint_header(self, painter: QPainter, option, text: str) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#A7A2AF"))
        font = self._view.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(
            option.rect,
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.restore()

    def _time_label(self, msg: ChatMessage) -> str:
        """Bottom-row label: a "待送达" prefix when the message is a pending
        (offline-queued) direct-chat send, so the user knows it is still
        waiting for the peer to come online (Android parity)."""
        if msg.pending:
            return "待送达 · " + format_message_time(msg.timestamp)
        return format_message_time(msg.timestamp)

    def _paint_message(self, painter: QPainter, option, msg: ChatMessage) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        view_w = rect.width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, bubble_h, _ = self._layout(msg, max_bubble_w)
        if msg.is_from_me:
            bubble_x = rect.left() + rect.width() - bubble_w - SIDE_MARGIN
        else:
            bubble_x = rect.left() + SIDE_MARGIN
        bubble_y = rect.top() + V_PAD
        bubble_rect = QRectF(bubble_x, bubble_y, bubble_w, bubble_h)

        path = bubble_path(bubble_rect.toRect(), 14, msg.is_from_me)
        painter.fillPath(path, QColor(BUBBLE_MINE if msg.is_from_me else BUBBLE_OTHER))

        inner = QRectF(bubble_rect.x() + H_PAD, bubble_rect.y() + V_PAD, bubble_rect.width() - 2 * H_PAD, bubble_rect.height() - 2 * V_PAD)
        y = inner.y()
        font = self._view.font()
        font.setPointSize(10)
        painter.setFont(font)
        if not msg.is_from_me:
            painter.setPen(QColor(BUBBLE_NAME))
            name_font = QFont(font)
            name_font.setPointSize(8)
            painter.setFont(name_font)
            painter.drawText(
                QRectF(inner.x(), y, inner.width(), NAME_H),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                msg.sender_name,
            )
            y += NAME_H
            painter.setFont(font)
        painter.setPen(QColor("#FFFFFF" if msg.is_from_me else BUBBLE_TEXT_OTHER))
        content_rect = QRectF(inner.x(), y, inner.width(), inner.height() - (y - inner.y()) - TIME_H - 2)
        painter.drawText(
            content_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            msg.content,
        )
        time_color = QColor("#FFFFFF" if msg.is_from_me else "#6B6875")
        time_color.setAlpha(200)
        painter.setPen(time_color)
        time_font = QFont(font)
        time_font.setPointSize(7)
        painter.setFont(time_font)
        painter.drawText(
            QRectF(inner.x(), inner.y() + inner.height() - TIME_H, inner.width(), TIME_H),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._time_label(msg),
        )
        painter.restore()

    def _paint_file_message(self, painter: QPainter, option, msg: ChatMessage) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        view_w = rect.width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, bubble_h, _ = self._layout(msg, max_bubble_w)
        if msg.is_from_me:
            bubble_x = rect.left() + rect.width() - bubble_w - SIDE_MARGIN
        else:
            bubble_x = rect.left() + SIDE_MARGIN
        bubble_y = rect.top() + V_PAD
        bubble_rect = QRectF(bubble_x, bubble_y, bubble_w, bubble_h)
        path = bubble_path(bubble_rect.toRect(), 14, msg.is_from_me)
        painter.fillPath(path, QColor(BUBBLE_MINE if msg.is_from_me else BUBBLE_OTHER))

        fi = msg.file_info
        state = self.file_states.get(msg.id, ("idle", "", ""))
        expired = file_offer_expired(fi)
        if expired:
            status_text = "已过期"
        else:
            status_text = {
                "idle": "点击下载",
                "downloading": "下载中...",
                "done": "已保存",
                "failed": state[2] or "下载失败",
            }.get(state[0], "点击下载")
        text_color = QColor("#FFFFFF" if msg.is_from_me else BUBBLE_TEXT_OTHER)
        subtle = QColor("#FFFFFF" if msg.is_from_me else "#6B6875")
        subtle.setAlpha(200 if msg.is_from_me else 255)
        name_font = self._view.font()
        name_font.setPointSize(10)
        fm = QFontMetrics(name_font)
        small_font = QFont(name_font)
        small_font.setPointSize(8)

        # icon (drawn with QPainter so it never depends on emoji font support)
        painter.setPen(text_color)
        icon_rect = QRectF(bubble_rect.x() + 12, bubble_rect.y() + 12, 34, 34)
        self._paint_file_icon(painter, icon_rect, text_color)

        # status on the right, vertically centered (fixed band so it never
        # overlaps the size line)
        status_fm = QFontMetrics(small_font)
        status_rect = QRectF(bubble_rect.right() - 96, bubble_rect.y(), 84, bubble_rect.height())
        painter.setFont(small_font)
        painter.setPen(text_color)
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            status_fm.elidedText(status_text, Qt.TextElideMode.ElideRight, 84),
        )

        # middle column: file name + size
        inner_x = icon_rect.right() + 10
        text_w = status_rect.left() - inner_x - 8
        file_name = fm.elidedText(fi.file_name, Qt.TextElideMode.ElideRight, max(int(text_w), 40))
        painter.setFont(name_font)
        painter.drawText(
            QRectF(inner_x, bubble_rect.y() + 12, text_w, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            file_name,
        )
        painter.setFont(small_font)
        painter.setPen(subtle)
        size_text = format_file_size(fi.file_size)
        painter.drawText(
            QRectF(inner_x, bubble_rect.y() + 32, text_w, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            size_text,
        )
        painter.restore()

    def _paint_file_icon(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """Draw a simple document glyph (body + folded corner + text lines)."""
        painter.save()
        pen = QPen(color, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        body = QRectF(rect.x() + 5, rect.y() + 4, 24, 27)
        painter.drawRoundedRect(body, 3, 3)
        fold = QPainterPath()
        fold.moveTo(body.right() - 7, body.top() + 0.5)
        fold.lineTo(body.right() - 7, body.top() + 7.5)
        fold.lineTo(body.right() - 0.5, body.top() + 7.5)
        painter.drawPath(fold)
        for dy, width in ((13, 14), (18, 14), (23, 9)):
            painter.drawLine(
                QPointF(body.x() + 5, body.y() + dy),
                QPointF(body.x() + 5 + width, body.y() + dy),
            )
        painter.restore()


class ChatInput(QTextEdit):
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


class ChatPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_back):
        super().__init__()
        self.vm = vm
        self.on_back = on_back
        self._stick_to_bottom = True
        self._building = False
        # fileId -> (status, target_path, message) for file messages
        self._file_states: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 8)
        back_btn = QPushButton("← 返回")
        back_btn.setObjectName("ghost")
        back_btn.clicked.connect(self.on_back)
        header_layout.addWidget(back_btn)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        header_layout.addWidget(self.title_label, 1)
        layout.addWidget(header)

        self.model = QStandardItemModel(self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(
            MessageDelegate(self.list_view, on_file_click=self._download_file, file_states=self._file_states, parent=self)
        )
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_message_menu)
        self.list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        layout.addWidget(self.list_view, 1)

        bottom = QFrame()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(14, 8, 14, 12)
        bottom_layout.setSpacing(2)
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
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
        self.input_edit = ChatInput(self._send_input)
        input_row.addWidget(self.input_edit, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setMinimumSize(80, 40)
        self.send_btn.clicked.connect(self._send_input)
        input_row.addWidget(self.send_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        bottom_layout.addLayout(input_row)
        self.count_label = QLabel("")
        self.count_label.setObjectName("faint")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.count_label.hide()
        bottom_layout.addWidget(self.count_label)
        layout.addWidget(bottom)

        self.input_edit.textChanged.connect(self._on_input_changed)
        self.vm.active_group_changed.connect(self._on_group_changed)
        self.vm.active_messages_changed.connect(self._on_messages_changed)
        self.vm.active_connection_lost_changed.connect(self._on_connection_state_changed)
        self.vm.file_download_finished.connect(self._on_file_download_finished)

    def _on_group_changed(self):
        self.title_label.setText(self.vm.active_group_name)
        self.model.setRowCount(0)
        self.input_edit.clear()
        self._file_states.clear()
        self._stick_to_bottom = True
        self._rebuild()

    def _on_messages_changed(self):
        self._rebuild()

    def _rebuild(self):
        self._building = True
        self.model.setRowCount(0)
        msgs = self.vm.active_messages()
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
        self._building = False
        if self._stick_to_bottom and msgs:
            self.list_view.scrollToBottom()

    def _on_scroll(self, value):
        if self._building:
            return
        bar = self.list_view.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - 40

    def _send_input(self):
        text = self.input_edit.toPlainText()
        if not text.strip():
            return
        if len(text) > MAX_CONTENT_LENGTH:
            # no silent truncation (Android parity): the Enter key can bypass
            # the disabled button, so reject over-long input here too
            self.count_label.setText(f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
            return
        if self._connection_blocked() or not self.vm.send_message(text):
            self._show_send_blocked()
            return
        self.input_edit.clear()

    def _connection_blocked(self) -> bool:
        gid = self.vm.active_group_id
        if gid is None:
            return True
        p2p = self.vm.group_p2p_map.get(gid)
        return p2p is None or self.vm.active_connection_lost()

    def _show_send_blocked(self):
        self.count_label.setText("消息未发送：已断开连接")
        self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
        self.count_label.show()

    def _on_connection_state_changed(self):
        self._update_send_ui()

    def _on_input_changed(self):
        self._update_send_ui()

    def _update_send_ui(self):
        length = len(self.input_edit.toPlainText())
        too_long = length > MAX_CONTENT_LENGTH
        blocked = self._connection_blocked()
        self.send_btn.setEnabled(
            bool(self.input_edit.toPlainText().strip()) and not too_long and not blocked
        )
        if blocked:
            self.count_label.setText("已断开连接，无法发送消息")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
        elif too_long:
            self.count_label.setText(f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
        elif length > MAX_CONTENT_LENGTH - 200:
            self.count_label.setText(f"{length}/{MAX_CONTENT_LENGTH}")
            self.count_label.setStyleSheet("font-size: 11px; color: #6B6875;")
            self.count_label.show()
        else:
            self.count_label.hide()

    def _pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self.window(), "选择要发送的文件")
        if not path:
            return
        if not self.vm.send_file(path):
            Toast(self.window()).show_message("无法发送文件：未连接或文件不可用")

    def _download_file(self, msg):
        fi = msg.file_info
        if fi is None:
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
        self.vm.download_file(msg.id, path)

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
                self._confirm_delete(msg.id)
            return
        copy_action = menu.addAction("复制")
        forward_action = menu.addAction("转发")
        delete_action = None
        if msg.is_from_me:
            menu.addSeparator()
            delete_action = menu.addAction("删除")
        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen is copy_action:
            QApplication.clipboard().setText(msg.content)
        elif chosen is forward_action:
            self._show_forward_dialog(msg.content)
        elif chosen is delete_action:
            self._confirm_delete(msg.id)

    def _show_forward_dialog(self, content: str):
        gid = self.vm.active_group_id
        targets = [g for g in self.vm.groups_list() if g.group_id != gid and g.connected]
        dialog = QDialog(self.window())
        dialog.setObjectName("confirmDialog")
        dialog.setWindowTitle("转发消息")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 12)
        preview = content if len(content) <= 20 else content[:20] + "..."
        preview_label = QLabel(f'"{preview}"')
        preview_label.setObjectName("hint")
        preview_label.setWordWrap(True)
        layout.addWidget(preview_label)
        layout.addSpacing(8)
        if not targets:
            none_label = QLabel("没有其他可转发的群组（未连接的群组无法转发）")
            none_label.setObjectName("hint")
            none_label.setWordWrap(True)
            layout.addWidget(none_label)
        else:
            for group in targets:
                btn = QPushButton(group.group_name)
                btn.setObjectName("outline")
                btn.setStyleSheet("text-align: left;")
                btn.clicked.connect(
                    lambda checked=False, gid_=group.group_id: self._forward(gid_, content, dialog)
                )
                layout.addWidget(btn)
        layout.addSpacing(8)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(dialog.accept)
        layout.addWidget(cancel_btn, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _forward(self, target_group_id: str, content: str, dialog: QDialog):
        dialog.accept()
        if not self.vm.send_message_to_group(target_group_id, content):
            Toast(self.window()).show_message("消息未发送：已断开连接")

    def _confirm_delete(self, message_id: str):
        box = QMessageBox(self.window())
        box.setWindowTitle("删除消息")
        box.setText("删除后，这条消息会从群内所有成员的聊天记录中移除，且无法恢复。")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_btn:
            self.vm.delete_message(message_id)
