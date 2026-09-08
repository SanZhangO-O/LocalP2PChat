"""Member list — the home screen and the first-class management unit.

Every known member (seen in a group, met through a direct chat, or added by
address) appears here; tapping one immediately pulls up a 1:1 chat, no
confirmation needed. Groups are a secondary entry in the header.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import ContactRequest, Peer
from ..view_model import MAX_NAME_LENGTH, ChatViewModel
from .theme import ERROR, PRIMARY, TEXT_SUBTLE
from .widgets import AvatarLabel, Toast, format_message_time


class RequestCard(QFrame):
    """A parked contact request: who wants in, with accept / ignore buttons."""

    def __init__(self, request: ContactRequest, on_accept=None, on_ignore=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet(
            "#card { background-color: rgba(103, 80, 164, 0.14); "
            "border: 1px solid rgba(103, 80, 164, 0.35); }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(AvatarLabel(request.name, 42, 16))
        info = QVBoxLayout()
        info.setSpacing(1)
        name_label = QLabel(request.name)
        name_label.setStyleSheet("font-size: 15px; font-weight: 600; color: #1C1B1F;")
        info.addWidget(name_label)
        ip_label = QLabel(f"{request.ip}:{request.port}")
        ip_label.setObjectName("faint")
        info.addWidget(ip_label)
        hint = QLabel(
            "已移除的成员请求重新添加" if request.from_removed else "请求添加你为成员"
        )
        hint.setStyleSheet(f"font-size: 12px; color: {ERROR};")
        info.addWidget(hint)
        top.addLayout(info, 1)
        layout.addLayout(top)

        buttons = QHBoxLayout()
        buttons.addStretch()
        ignore_btn = QPushButton("忽略")
        ignore_btn.setObjectName("ghost")
        ignore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        ignore_btn.clicked.connect(on_ignore)
        buttons.addWidget(ignore_btn)
        accept_btn = QPushButton("接受")
        accept_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        accept_btn.clicked.connect(on_accept)
        buttons.addWidget(accept_btn)
        layout.addLayout(buttons)


class ContactRow(QFrame):
    def __init__(self, contact: Peer, last_message=None, on_click=None, on_remove=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._on_click = on_click
        self._on_remove = on_remove

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)
        layout.addWidget(AvatarLabel(contact.name, 44, 18))

        info = QVBoxLayout()
        info.setSpacing(1)
        name_label = QLabel(contact.name)
        name_label.setStyleSheet("font-size: 15px; font-weight: 600; color: #1C1B1F;")
        info.addWidget(name_label)
        if last_message is not None:
            preview = QLabel(last_message.content)
            preview.setObjectName("faint")
            preview.setMaximumWidth(300)
            info.addWidget(preview)
        else:
            ip_label = QLabel(f"{contact.ip_address}:{contact.port}")
            ip_label.setObjectName("faint")
            info.addWidget(ip_label)
        layout.addLayout(info, 1)

        if last_message is not None:
            time_label = QLabel(format_message_time(last_message.timestamp))
            time_label.setStyleSheet("font-size: 11px; color: #A7A2AF;")
            layout.addWidget(time_label, alignment=Qt.AlignmentFlag.AlignTop)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def _show_menu(self, pos):
        menu = QMenu(self)
        remove_action = menu.addAction("移除成员")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is remove_action:
            self._on_remove()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._on_click()
        super().mouseReleaseEvent(event)


class MemberListPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_open_groups, on_open_settings, on_open_chat):
        super().__init__()
        self.vm = vm
        self.on_open_groups = on_open_groups
        self.on_open_settings = on_open_settings
        self.on_open_chat = on_open_chat

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 8)
        title = QLabel("成员")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        header_layout.addWidget(title)
        header_layout.addStretch()
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.clicked.connect(self.on_open_settings)
        header_layout.addWidget(self.settings_btn)
        self.groups_btn = QPushButton("群组")
        self.groups_btn.setObjectName("ghost")
        self.groups_btn.setToolTip("群聊（多人群组）")
        self.groups_btn.clicked.connect(self.on_open_groups)
        header_layout.addWidget(self.groups_btn)
        self.add_btn = QPushButton("添加成员")
        self.add_btn.setObjectName("outline")
        self.add_btn.clicked.connect(self._show_add_dialog)
        header_layout.addWidget(self.add_btn)
        layout.addWidget(header)

        self.list = QListWidget()
        self.list.setSpacing(6)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.list, 1)

        self.empty_label = QLabel(
            "暂无成员\n群组成员会自动出现在这里；也可点击右上角“添加成员”按 IP:端口 添加"
        )
        self.empty_label.setStyleSheet(f"font-size: 14px; color: {TEXT_SUBTLE};")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label, 1)

        self.vm.direct_contacts_signal.connect(self.refresh)
        self.vm.direct_messages_signal.connect(self._on_direct_messages_changed)
        self.vm.direct_requests_signal.connect(self.refresh)
        self.refresh()

    def _on_direct_messages_changed(self, peer_id: str):
        # a new message may change the conversation preview on the home page
        self.refresh()

    def refresh(self):
        requests = self.vm.direct_requests_list()
        contacts = self.vm.direct_contacts_list()
        self.list.clear()
        empty = not requests and not contacts
        self.list.setVisible(not empty)
        self.empty_label.setVisible(empty)
        if requests:
            header = QLabel("联系人请求（{}）\n等待你确认的添加请求".format(len(requests)))
            header.setObjectName("faint")
            header.setStyleSheet(
                f"font-size: 13px; font-weight: 600; color: {PRIMARY}; "
                "padding: 4px 2px 2px 2px;"
            )
            head_item = QListWidgetItem()
            head_item.setFlags(Qt.ItemFlag.NoItemFlags)
            head_item.setSizeHint(header.sizeHint())
            self.list.addItem(head_item)
            self.list.setItemWidget(head_item, header)
            for request in requests:
                item = QListWidgetItem()
                card = RequestCard(
                    request,
                    on_accept=lambda rid=request.id: self.vm.accept_contact_request(rid),
                    on_ignore=lambda rid=request.id: self.vm.ignore_contact_request(rid),
                )
                item.setSizeHint(card.sizeHint())
                self.list.addItem(item)
                self.list.setItemWidget(item, card)
        if contacts:
            header = QLabel("成员")
            header.setObjectName("faint")
            header.setStyleSheet(
                f"font-size: 13px; font-weight: 600; color: {TEXT_SUBTLE}; "
                f"padding: {'12px 2px 2px 2px' if requests else '4px 2px 2px 2px'};"
            )
            head_item = QListWidgetItem()
            head_item.setFlags(Qt.ItemFlag.NoItemFlags)
            head_item.setSizeHint(header.sizeHint())
            self.list.addItem(head_item)
            self.list.setItemWidget(head_item, header)
        for contact in contacts:
            item = QListWidgetItem()
            row = ContactRow(
                contact,
                last_message=self.vm.direct_last_message(contact.id),
                on_click=lambda c=contact: self.on_open_chat(c),
                on_remove=lambda c=contact: self._confirm_remove(c),
            )
            item.setSizeHint(row.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, row)

    def _confirm_remove(self, contact: Peer):
        box = QMessageBox(self.window())
        box.setWindowTitle("移除成员")
        box.setText(f"确定要移除成员 {contact.name} 吗？\n聊天记录不会删除，成员仍可通过地址重新添加。")
        remove_btn = box.addButton("移除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is remove_btn:
            self.vm.remove_direct_contact(contact.id)

    def _show_add_dialog(self):
        dialog = QDialog(self.window())
        dialog.setObjectName("confirmDialog")
        dialog.setWindowTitle("添加成员")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 12)
        hint = QLabel("输入对方的“IP:端口”（默认端口可省略），例如 192.168.1.100 或 192.168.1.100:9999")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        ip_edit = QLineEdit()
        ip_edit.setPlaceholderText("IP:端口")
        ip_edit.setMinimumHeight(38)
        layout.addWidget(ip_edit)
        name_edit = QLineEdit()
        name_edit.setPlaceholderText("备注名（可选）")
        name_edit.setMaxLength(MAX_NAME_LENGTH)
        name_edit.setMinimumHeight(38)
        layout.addWidget(name_edit)
        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(dialog.reject)
        buttons.addWidget(cancel_btn)
        ok_btn = QPushButton("添加")
        buttons.addWidget(ok_btn)
        layout.addLayout(buttons)

        def on_accept():
            if not self.vm.add_direct_contact(ip_edit.text().strip(), name_edit.text().strip()):
                Toast(self.window()).show_message("地址无效")
                return
            dialog.accept()

        ok_btn.clicked.connect(on_accept)
        dialog.exec()
