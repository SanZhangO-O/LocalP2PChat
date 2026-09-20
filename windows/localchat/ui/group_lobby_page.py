from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import network as network_module
from ..models import MEDIA_AUDIO, Peer
from ..view_model import MAX_NAME_LENGTH, ChatViewModel
from .qr_dialogs import GroupInviteQrDialog
from .theme import ERROR, PRIMARY, TEXT_SUBTLE
from .widgets import AvatarLabel, Toast


class GroupSettingsDialog(QDialog):
    """Owner-only editor for the group name and announcement. Saving calls
    ChatViewModel.update_group_info, which broadcasts a group_update."""

    def __init__(self, vm: ChatViewModel, parent=None):
        super().__init__(parent)
        self.vm = vm
        self.setWindowTitle("群设置")
        self.setObjectName("confirmDialog")
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(8)

        name_label = QLabel("群名称")
        name_label.setObjectName("faint")
        layout.addWidget(name_label)
        self.name_edit = QLineEdit(vm.active_group_name)
        self.name_edit.setMaxLength(MAX_NAME_LENGTH)
        layout.addWidget(self.name_edit)

        ann_label = QLabel("群公告")
        ann_label.setObjectName("faint")
        layout.addWidget(ann_label)
        self.announcement_edit = QPlainTextEdit(vm.active_group_announcement())
        self.announcement_edit.setPlaceholderText("输入群公告（留空则清除）")
        self.announcement_edit.setFixedHeight(110)
        layout.addWidget(self.announcement_edit)

        invite_row = QHBoxLayout()
        invite_hint = QLabel("邀请其他人加入本群（不含群密码）")
        invite_hint.setObjectName("faint")
        invite_row.addWidget(invite_hint, 1)
        invite_btn = QPushButton("群邀请二维码")
        invite_btn.setObjectName("outline")
        invite_btn.clicked.connect(self._show_invite_qr)
        invite_row.addWidget(invite_btn)
        layout.addLayout(invite_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        save_btn.setText("保存")
        cancel_btn.setText("取消")
        save_btn.clicked.connect(self._on_save)
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

    def _show_invite_qr(self):
        group_id = self.vm.active_group_numeric_id()
        if not group_id:
            return
        GroupInviteQrDialog(self.vm, group_id, self.vm.active_group_name, self.window()).exec()

    def _on_save(self):
        name = self.name_edit.text().strip()
        if not name:
            Toast(self.window()).show_message("群名称不能为空")
            return
        announcement = self.announcement_edit.toPlainText().strip()
        if not self.vm.update_group_info(name, announcement):
            Toast(self.window()).show_message("群信息更新失败")
            return
        self.accept()


class PeerRow(QFrame):
    def __init__(
        self,
        peer: Peer,
        is_self: bool = False,
        on_call=None,
        on_kick=None,
        on_voice_call=None,
        verified: bool = False,
        on_fingerprint=None,
        parent=None,
    ):
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
        elif verified:
            # TOFU badge: this member's device identity is signature-bound in
            # this group (groupauth); the 安全码 dialog allows an out-of-band
            # comparison against the member's own settings screen
            badge = QLabel("已验证")
            badge.setStyleSheet(
                "font-size: 11px; color: #1B5E20; background: #C8E6C9;"
                "border-radius: 6px; padding: 1px 6px; font-weight: 600;"
            )
            badge.setToolTip("该成员已通过设备签名身份验证")
            name_row.addWidget(badge)
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

        if not is_self and on_voice_call is not None:
            voice_btn = QPushButton("语音")
            voice_btn.setObjectName("ghost")
            voice_btn.setToolTip("语音通话")
            voice_btn.setFixedSize(64, 40)
            voice_btn.clicked.connect(lambda checked=False, pid=peer.id: on_voice_call(pid))
            layout.addWidget(voice_btn)

        if not is_self and on_fingerprint is not None:
            fp_btn = QPushButton("安全码")
            fp_btn.setObjectName("ghost")
            fp_btn.setToolTip("查看该成员的设备身份安全码")
            fp_btn.setFixedSize(64, 40)
            fp_btn.clicked.connect(lambda checked=False, pid=peer.id: on_fingerprint(pid))
            layout.addWidget(fp_btn)

        if not is_self and on_kick is not None:
            kick_btn = QPushButton("移出")
            kick_btn.setObjectName("danger")
            kick_btn.setToolTip("将该成员移出群组")
            kick_btn.setFixedSize(64, 40)
            kick_btn.clicked.connect(lambda checked=False, pid=peer.id: on_kick(pid))
            layout.addWidget(kick_btn)


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
        self.conference_btn = QPushButton("语音会议")
        self.conference_btn.setObjectName("ghost")
        self.conference_btn.setToolTip("发起多人语音会议（仅音频）")
        self.conference_btn.clicked.connect(self._start_group_call)
        header_layout.addWidget(self.conference_btn)
        self.settings_btn = QPushButton("群设置")
        self.settings_btn.setObjectName("ghost")
        self.settings_btn.setToolTip("修改群名称与群公告")
        self.settings_btn.clicked.connect(self._open_group_settings)
        header_layout.addWidget(self.settings_btn)
        self.leave_btn = QPushButton("退出群组")
        self.leave_btn.setObjectName("danger")
        self.leave_btn.clicked.connect(self._confirm_leave)
        header_layout.addWidget(self.leave_btn)
        layout.addWidget(header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 8, 16, 16)
        body_layout.setSpacing(8)

        self.announce_card = QFrame()
        self.announce_card.setObjectName("infoCard")
        announce_layout = QVBoxLayout(self.announce_card)
        announce_layout.setContentsMargins(14, 10, 14, 10)
        announce_title = QLabel("群公告")
        announce_title.setStyleSheet(f"font-size: 12px; color: {PRIMARY}; font-weight: 700;")
        announce_layout.addWidget(announce_title)
        self.announce_label = QLabel("")
        self.announce_label.setWordWrap(True)
        self.announce_label.setStyleSheet("font-size: 13px; color: #1C1B1F;")
        announce_layout.addWidget(self.announce_label)
        body_layout.addWidget(self.announce_card)

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
        invite_qr_btn = QPushButton("群邀请二维码")
        invite_qr_btn.setObjectName("ghost")
        invite_qr_btn.setStyleSheet("font-size: 12px;")
        invite_qr_btn.setToolTip(
            "出示群邀请二维码：扫码自动填入数字ID与地址（不含群密码，密码仍需手动输入）"
        )
        invite_qr_btn.clicked.connect(self._show_invite_qr)
        self.group_id_row.addWidget(invite_qr_btn)
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

    def _show_invite_qr(self):
        group_id = self.vm.active_group_numeric_id()
        if not group_id:
            return
        GroupInviteQrDialog(self.vm, group_id, self.vm.active_group_name, self.window()).exec()

    def _confirm_leave(self):
        box = QMessageBox(self.window())
        box.setWindowTitle("退出群组")
        box.setText("退出后群组仍会保留在列表中，可随时重新进入连接。聊天记录不会被删除。")
        leave_btn = box.addButton("退出", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is leave_btn:
            self.on_leave()

    def _open_group_settings(self):
        if not self.vm.active_is_host or self.vm.active_group_id is None:
            return
        dialog = GroupSettingsDialog(self.vm, self.window())
        dialog.exec()

    def _confirm_kick(self, peer_id: str):
        if not self.vm.active_is_host or self.vm.active_group_id is None:
            return
        peers = self.vm.active_peers()
        peer = peers.get(peer_id)
        name = peer.name if peer is not None else peer_id
        box = QMessageBox(self.window())
        box.setWindowTitle("移出成员")
        box.setText(
            f"确定要将 {name} 移出群组吗？\n对方将立即断开连接，且无法再收到本群消息。"
        )
        kick_btn = box.addButton("移出", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is kick_btn:
            self.vm.kick_active_member(peer_id)

    def _show_member_fingerprint(self, peer_id: str):
        """Security-code dialog for one group member: shows the TOFU-bound
        device identity fingerprint (安全码) to compare out-of-band against
        the member's own settings screen."""
        gid = self.vm.active_group_id
        if not gid:
            return
        peers = self.vm.active_peers()
        peer = peers.get(peer_id)
        name = peer.name if peer is not None else peer_id
        fingerprint = self.vm.group_member_fingerprint(gid, peer_id)
        if fingerprint:
            box = QMessageBox(self.window())
            box.setWindowTitle("成员安全码")
            box.setText(
                f"{name} 的设备安全码：\n\n{fingerprint}\n\n"
                "与对方设置页「本机安全码」当面比对一致，即可完全排除中间人。"
            )
            copy_btn = box.addButton("复制", QMessageBox.ButtonRole.ActionRole)
            box.addButton("关闭", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is copy_btn:
                QApplication.clipboard().setText(fingerprint)
                Toast(self.window()).show_message("已复制安全码")
        else:
            box = QMessageBox(self.window())
            box.setWindowTitle("成员安全码")
            box.setText(
                f"{name} 尚未在本群绑定设备身份（未收到过其签名消息）。\n"
                "对方发送消息后，这里会显示其设备安全码。"
            )
            box.exec()

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
        self.settings_btn.setVisible(bool(is_host) and gid is not None)
        self.conference_btn.setVisible(gid is not None)
        announcement = self.vm.active_group_announcement() if gid else ""
        self.announce_label.setText(announcement)
        self.announce_card.setVisible(bool(announcement) and gid is not None)
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
        gid = self.vm.active_group_id
        item = QListWidgetItem()
        row = PeerRow(
            peer,
            is_self,
            on_call=self._start_call if not is_self else None,
            # the owner may remove members; members only get the call buttons
            on_kick=(
                self._confirm_kick
                if (not is_self and self.vm.active_is_host)
                else None
            ),
            on_voice_call=self._start_voice_call if not is_self else None,
            verified=(
                not is_self
                and bool(gid)
                and self.vm.group_member_verified(gid, peer.id)
            ),
            on_fingerprint=self._show_member_fingerprint if not is_self else None,
        )
        item.setSizeHint(row.sizeHint())
        self.peer_list.addItem(item)
        self.peer_list.setItemWidget(item, row)

    def _start_call(self, peer_id: str):
        self.vm.start_call(peer_id)

    def _start_voice_call(self, peer_id: str):
        self.vm.start_call(peer_id, media=MEDIA_AUDIO)

    def _start_group_call(self):
        self.vm.start_group_call()
