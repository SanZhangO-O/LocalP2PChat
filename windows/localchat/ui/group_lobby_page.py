from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import network as network_module
from ..models import Peer
from ..view_model import ChatViewModel
from .theme import ERROR, PRIMARY, TEXT_SUBTLE
from .widgets import AvatarLabel, Toast


class PeerRow(QFrame):
    def __init__(self, peer: Peer, is_self: bool = False, on_call=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        layout.addWidget(AvatarLabel(peer.name, 44, 18))

        info = QVBoxLayout()
        info.setSpacing(1)
        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        name_label = QLabel(peer.name)
        name_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #1C1B1F;")
        name_label.setMaximumWidth(280)
        name_row.addWidget(name_label)
        if is_self:
            me = QLabel("我")
            me.setStyleSheet(f"font-size: 11px; color: {PRIMARY}; font-weight: 600;")
            name_row.addWidget(me)
        name_row.addStretch()
        info.addLayout(name_row)
        ip_label = QLabel(peer.ip_address)
        ip_label.setObjectName("faint")
        info.addWidget(ip_label)
        layout.addLayout(info, 1)

        if not is_self and on_call is not None:
            call_btn = QPushButton("通话")
            call_btn.setObjectName("ghost")
            call_btn.setToolTip("视频通话")
            call_btn.setFixedSize(64, 40)
            call_btn.clicked.connect(lambda checked=False, pid=peer.id: on_call(pid))
            layout.addWidget(call_btn)


class GroupLobbyPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_back, on_open_chat, on_leave):
        super().__init__()
        self.vm = vm
        self.on_back = on_back
        self.on_open_chat = on_open_chat
        self.on_leave = on_leave

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 8)
        back_btn = QPushButton("← 返回")
        back_btn.setObjectName("ghost")
        back_btn.clicked.connect(self._on_back_clicked)
        header_layout.addWidget(back_btn)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        title_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("faint")
        title_col.addWidget(self.subtitle_label)
        header_layout.addLayout(title_col, 1)

        self.chat_btn = QPushButton("进入聊天")
        self.chat_btn.clicked.connect(self.on_open_chat)
        header_layout.addWidget(self.chat_btn)
        self.leave_btn = QPushButton("退出群组")
        self.leave_btn.setObjectName("danger")
        self.leave_btn.clicked.connect(self._confirm_leave)
        header_layout.addWidget(self.leave_btn)
        layout.addWidget(header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 8, 16, 16)
        body_layout.setSpacing(8)

        self.host_card = QFrame()
        self.host_card.setObjectName("hostCard")
        host_layout = QVBoxLayout(self.host_card)
        host_layout.setContentsMargins(14, 10, 14, 10)
        host_title = QLabel("将此地址分享给其他人加入")
        host_title.setObjectName("faint")
        host_title.setStyleSheet("font-size: 12px;")
        host_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        host_layout.addWidget(host_title)
        host_row = QHBoxLayout()
        self.host_address_label = QLabel("")
        self.host_address_label.setStyleSheet("font-size: 15px; font-weight: 600; color: #1C1B1F;")
        host_row.addWidget(self.host_address_label, 1)
        host_copy_btn = QPushButton("复制")
        host_copy_btn.setObjectName("ghost")
        host_copy_btn.setStyleSheet("font-size: 12px;")
        host_copy_btn.clicked.connect(self._copy_address)
        host_row.addWidget(host_copy_btn)
        host_layout.addLayout(host_row)
        self.group_id_row_widget = QWidget()
        self.group_id_row = QHBoxLayout(self.group_id_row_widget)
        self.group_id_label = QLabel("")
        self.group_id_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #1C1B1F;")
        self.group_id_row.addWidget(self.group_id_label, 1)
        group_id_copy_btn = QPushButton("复制")
        group_id_copy_btn.setObjectName("ghost")
        group_id_copy_btn.setStyleSheet("font-size: 12px;")
        group_id_copy_btn.clicked.connect(self._copy_group_id)
        self.group_id_row.addWidget(group_id_copy_btn)
        host_layout.addWidget(self.group_id_row_widget)
        self.password_row_widget = QWidget()
        self.password_row = QHBoxLayout(self.password_row_widget)
        self.password_label = QLabel("")
        self.password_label.setStyleSheet("font-size: 14px; font-weight: 600; color: #1C1B1F;")
        self.password_row.addWidget(self.password_label, 1)
        password_copy_btn = QPushButton("复制")
        password_copy_btn.setObjectName("ghost")
        password_copy_btn.setStyleSheet("font-size: 12px;")
        password_copy_btn.clicked.connect(self._copy_password)
        self.password_row.addWidget(password_copy_btn)
        host_layout.addWidget(self.password_row_widget)
        body_layout.addWidget(self.host_card)

        self.warn_card = QFrame()
        self.warn_card.setObjectName("warnCard")
        warn_layout = QVBoxLayout(self.warn_card)
        warn_layout.setContentsMargins(14, 12, 14, 12)
        self.warn_label = QLabel("")
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet(f"font-size: 13px; color: {ERROR};")
        warn_layout.addWidget(self.warn_label)
        warn_row = QHBoxLayout()
        warn_row.addStretch()
        self.retry_host_btn = QPushButton("重试监听")
        self.retry_host_btn.setObjectName("ghost")
        self.retry_host_btn.clicked.connect(self.vm.retry_host_listening)
        warn_row.addWidget(self.retry_host_btn)
        warn_layout.addLayout(warn_row)
        body_layout.addWidget(self.warn_card)

        self.lost_card = QFrame()
        self.lost_card.setObjectName("warnCard")
        lost_layout = QHBoxLayout(self.lost_card)
        lost_layout.setContentsMargins(14, 10, 14, 10)
        lost_label = QLabel("与群组的连接已断开")
        lost_label.setStyleSheet(f"font-size: 13px; color: {ERROR};")
        lost_layout.addWidget(lost_label, 1)
        reconnect_btn = QPushButton("重连")
        reconnect_btn.setObjectName("ghost")
        reconnect_btn.clicked.connect(self.vm.reconnect_active_group)
        lost_layout.addWidget(reconnect_btn)
        body_layout.addWidget(self.lost_card)

        self.rejoin_card = QFrame()
        self.rejoin_card.setObjectName("warnCard")
        rejoin_layout = QVBoxLayout(self.rejoin_card)
        rejoin_layout.setContentsMargins(14, 12, 14, 12)
        self.rejoin_label = QLabel("")
        self.rejoin_label.setStyleSheet(f"font-size: 13px; color: {ERROR};")
        self.rejoin_label.setWordWrap(True)
        rejoin_layout.addWidget(self.rejoin_label)
        retry_btn = QPushButton("重试连接")
        retry_btn.setObjectName("ghost")
        retry_btn.clicked.connect(self.vm.reconnect_active_group)
        rejoin_layout.addWidget(retry_btn, alignment=Qt.AlignmentFlag.AlignRight)
        body_layout.addWidget(self.rejoin_card)

        self.peer_count_label = QLabel("")
        self.peer_count_label.setObjectName("faint")
        body_layout.addWidget(self.peer_count_label)

        self.peer_list = QListWidget()
        self.peer_list.setSpacing(4)
        self.peer_list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        body_layout.addWidget(self.peer_list, 1)

        self.empty_label = QLabel("")
        self.empty_label.setStyleSheet(f"font-size: 15px; color: {TEXT_SUBTLE};")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body_layout.addWidget(self.empty_label, 1)

        layout.addWidget(body, 1)

        self.vm.active_group_changed.connect(self.refresh)
        self.vm.active_peers_changed.connect(self.refresh)
        self.vm.active_connection_lost_changed.connect(self.refresh)
        self.vm.active_server_error_changed.connect(self.refresh)
        self.vm.rejoin_state_changed.connect(self.refresh)
        self.refresh()

    def _on_back_clicked(self):
        self.on_back()

    def _copy_address(self):
        ip = self.vm.local_ip
        if not ip:
            return
        QApplication.clipboard().setText(f"{ip}:{self.vm.local_port}")
        Toast(self.window()).show_message("已复制地址")

    def _copy_password(self):
        password = self.vm.active_group_password
        if not password:
            return
        QApplication.clipboard().setText(password)
        Toast(self.window()).show_message("已复制群组密码")

    def _copy_group_id(self):
        group_id = self.vm.active_group_numeric_id()
        if not group_id:
            return
        QApplication.clipboard().setText(group_id)
        Toast(self.window()).show_message("已复制数字ID")

    def _confirm_leave(self):
        box = QMessageBox(self.window())
        box.setWindowTitle("退出群组")
        box.setText("退出后群组仍会保留在列表中，可随时重新进入连接。聊天记录不会被删除。")
        leave_btn = box.addButton("退出", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is leave_btn:
            self.on_leave()

    def refresh(self):
        gid = self.vm.active_group_id
        is_host = self.vm.active_is_host
        group_name = self.vm.active_group_name
        self.title_label.setText(f"群组: {group_name}" if gid else "")
        if gid is None:
            self.subtitle_label.setText("")
        elif is_host:
            self.subtitle_label.setText(f"创建者 · {self.vm.local_ip}:{self.vm.local_port}")
        else:
            self.subtitle_label.setText("成员")

        ip = self.vm.local_ip
        self.host_address_label.setText(f"{ip}:{self.vm.local_port}" if ip else "未连接到网络")
        self.host_card.setVisible(is_host and gid is not None)
        group_id = self.vm.active_group_numeric_id()
        self.group_id_label.setText(
            f"群组数字ID: {network_module.format_numeric_group_id(group_id)}" if group_id else ""
        )
        self.group_id_row_widget.setVisible(bool(group_id))
        password = self.vm.active_group_password
        self.password_label.setText(f"群组密码: {password}" if password else "")
        self.password_row_widget.setVisible(bool(password))

        server_error = self.vm.active_server_error() if gid else None
        self.warn_card.setVisible(bool(server_error))
        self.warn_label.setText(server_error or "")
        self.retry_host_btn.setVisible(bool(server_error) and is_host)

        connection_lost = self.vm.active_connection_lost()
        self.lost_card.setVisible(connection_lost and not is_host and gid is not None)

        rejoin_in_progress = self.vm.rejoin_in_progress
        rejoin_failed = self.vm.rejoin_failed
        if rejoin_in_progress:
            self.rejoin_label.setText("正在连接...")
            self.rejoin_card.setVisible(True)
        elif rejoin_failed:
            self.rejoin_label.setText("连接失败")
            self.rejoin_card.setVisible(True)
        else:
            self.rejoin_card.setVisible(False)

        self.peer_list.clear()
        peers = self.vm.active_peers() if gid else {}
        self.peer_list.setVisible(bool(peers))

        if peers:
            self.empty_label.hide()
            count = len(peers) + 1
            self.peer_count_label.setText(f"群组成员 ({count}人)")
            self.peer_count_label.setVisible(True)
            my_peer = Peer("self", self.vm.active_my_name or "我", self.vm.local_ip, self.vm.local_port)
            self._add_peer_row(my_peer, True)
            for peer in peers.values():
                self._add_peer_row(peer, False)
            self.empty_label.hide()
        else:
            self.peer_count_label.setVisible(False)
            self.empty_label.show()
            if gid is None:
                self.empty_label.setText("")
            elif rejoin_in_progress:
                self.empty_label.setText("正在连接...")
            elif rejoin_failed and not connection_lost:
                self.empty_label.setText("连接失败")
            elif is_host:
                self.empty_label.setText("等待其他设备加入...")
            elif connection_lost:
                self.empty_label.setText("已断开连接")
            else:
                self.empty_label.setText("已连接到群组")

    def _add_peer_row(self, peer: Peer, is_self: bool):
        item = QListWidgetItem()
        row = PeerRow(peer, is_self, on_call=self._start_call if not is_self else None)
        item.setSizeHint(row.sizeHint())
        self.peer_list.addItem(item)
        self.peer_list.setItemWidget(item, row)

    def _start_call(self, peer_id: str):
        self.vm.start_call(peer_id)
