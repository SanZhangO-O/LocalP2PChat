from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..view_model import ChatViewModel, GroupMeta
from .theme import ERROR, PRIMARY, TEXT_SUBTLE
from .widgets import AvatarLabel, Toast, format_group_time


class GroupCard(QFrame):
    clicked = pyqtSignal()
    remove_requested = pyqtSignal()

    def __init__(self, group: GroupMeta, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        layout.addWidget(AvatarLabel(group.group_name, 48, 20))

        info = QVBoxLayout()
        info.setSpacing(2)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_label = QLabel(group.group_name)
        name_label.setStyleSheet("font-size: 15px; font-weight: 600; color: #1C1B1F;")
        name_label.setMaximumWidth(260)
        name_row.addWidget(name_label)
        if not group.connected and not group.is_host:
            offline = QLabel("未连接")
            offline.setStyleSheet(f"font-size: 11px; color: {ERROR}; font-weight: 600;")
            name_row.addWidget(offline)
        name_row.addStretch()
        info.addLayout(name_row)

        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        role = QLabel("创建者" if group.is_host else "成员")
        role.setStyleSheet(f"font-size: 12px; color: {PRIMARY}; font-weight: 600;")
        sub_row.addWidget(role)
        count = QLabel(f"{group.member_count}人")
        count.setStyleSheet(f"font-size: 12px; color: {TEXT_SUBTLE};")
        sub_row.addWidget(count)
        if group.last_message:
            last = QLabel(group.last_message)
            last.setStyleSheet(f"font-size: 12px; color: {TEXT_SUBTLE};")
            last.setMaximumWidth(280)
            sub_row.addWidget(last)
        sub_row.addStretch()
        info.addLayout(sub_row)
        layout.addLayout(info, 1)

        right = QVBoxLayout()
        right.setAlignment(Qt.AlignmentFlag.AlignTop)
        right.setSpacing(4)
        time_label = QLabel(format_group_time(group.last_message_time))
        time_label.setStyleSheet("font-size: 11px; color: #A7A2AF;")
        time_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(time_label)
        if group.unread_count > 0:
            badge = QLabel("99+" if group.unread_count > 99 else str(group.unread_count))
            badge.setFixedHeight(20)
            badge.setMinimumWidth(20)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setStyleSheet(
                f"background: {PRIMARY}; color: white; border-radius: 10px;"
                "font-size: 10px; font-weight: 700; padding: 0 5px;"
            )
            right.addWidget(badge, alignment=Qt.AlignmentFlag.AlignRight)
        else:
            right.addStretch()
        layout.addLayout(right)

        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

    def _show_menu(self, pos):
        menu = QMenu(self)
        delete_action = menu.addAction("删除群组")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen is delete_action:
            self.remove_requested.emit()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class GroupListPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_add_group, on_open_members=None):
        super().__init__()
        self.vm = vm
        self.on_add_group = on_add_group
        self.on_open_group = None
        self.on_open_settings = None
        self.on_open_members = on_open_members

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(24, 16, 24, 10)
        title = QLabel("群组")
        title.setObjectName("appTitle")
        header_layout.addWidget(title)
        header_layout.addStretch()
        self.members_btn = QPushButton("成员")
        self.members_btn.setObjectName("ghost")
        self.members_btn.setToolTip("返回成员列表（直聊首页）")
        self.members_btn.clicked.connect(self._open_members)
        header_layout.addWidget(self.members_btn)
        self.settings_btn = QPushButton("设置")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.clicked.connect(self._open_settings)
        header_layout.addWidget(self.settings_btn)
        layout.addWidget(header)

        self.list = QListWidget()
        self.list.setSpacing(6)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        layout.addWidget(self.list, 1)

        self.empty_widget = QWidget()
        empty_layout = QVBoxLayout(self.empty_widget)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_icon = QLabel("群")
        empty_icon.setStyleSheet("font-size: 48px; font-weight: 600; color: #6750A4;")
        empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_icon)
        empty_text = QLabel("暂无群组")
        empty_text.setObjectName("hint")
        empty_text.setStyleSheet("font-size: 16px;")
        empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_text)
        empty_hint = QLabel("点击右下角 + 添加群组")
        empty_hint.setObjectName("faint")
        empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_hint)
        self.empty_widget.hide()
        layout.addWidget(self.empty_widget, 1)

        self.fab = QPushButton("+")
        self.fab.setObjectName("fab")
        self.fab.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fab.setToolTip("添加群组")
        self.fab.clicked.connect(self.on_add_group)
        layout.addWidget(self.fab, alignment=Qt.AlignmentFlag.AlignRight)
        layout.setContentsMargins(0, 0, 24, 24)

        self.vm.groups_changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        self.list.clear()
        groups = self.vm.groups_list()
        for group in groups:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, group.group_id)
            card = GroupCard(group)
            card.clicked.connect(lambda gid=group.group_id: self._open_group(gid))
            card.remove_requested.connect(lambda gid=group.group_id: self._confirm_remove(gid))
            item.setSizeHint(card.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, card)
        self.list.setVisible(bool(groups))
        self.empty_widget.setVisible(not groups)

    def _open_group(self, group_id: str):
        self.vm.switch_to_group(group_id)
        if self.on_open_group is not None:
            self.on_open_group()

    def _open_settings(self):
        if self.on_open_settings is not None:
            self.on_open_settings()

    def _open_members(self):
        if self.on_open_members is not None:
            self.on_open_members()

    def _confirm_remove(self, group_id: str):
        box = QMessageBox(self.window())
        box.setWindowTitle("删除群组")
        box.setText("确定要删除该群组吗？\n该群组及全部聊天记录将被永久删除，无法恢复。\n（右键群组可触发此操作）")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_btn:
            self.vm.remove_group(group_id)
