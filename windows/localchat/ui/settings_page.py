"""独立设置界面：显示/修改自己的昵称、本机 IP 与本机端口。

端口修改后整个程序的监听地址随之改变（群组与直聊都走它），需要把新的
"IP:端口" 分享给其他人。
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import network as network_module
from ..view_model import MAX_NAME_LENGTH, ChatViewModel
from .widgets import Toast


class SettingsPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_back):
        super().__init__()
        self.vm = vm
        self.on_back = on_back

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
        title = QLabel("设置")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        header_layout.addWidget(title, 1)
        layout.addWidget(header)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 12, 24, 24)
        body_layout.setSpacing(14)
        layout.addWidget(body, 1)

        # ---- 昵称 ----
        nick_card = QFrame()
        nick_card.setObjectName("card")
        nick_layout = QVBoxLayout(nick_card)
        nick_layout.setContentsMargins(16, 14, 16, 14)
        nick_layout.setSpacing(8)
        nick_title = QLabel("我的昵称")
        nick_title.setStyleSheet("font-size: 14px; font-weight: 600;")
        nick_layout.addWidget(nick_title)
        nick_hint = QLabel("群组、直聊和通话中显示的名字；新加入的群组会使用它")
        nick_hint.setObjectName("faint")
        nick_hint.setWordWrap(True)
        nick_layout.addWidget(nick_hint)
        self.nick_edit = QLineEdit()
        self.nick_edit.setPlaceholderText("昵称")
        self.nick_edit.setMaxLength(MAX_NAME_LENGTH)
        self.nick_edit.setMaximumWidth(320)
        self.nick_edit.setMinimumHeight(36)
        nick_layout.addWidget(self.nick_edit)
        nick_save = QPushButton("保存昵称")
        nick_save.setObjectName("outline")
        nick_save.clicked.connect(self._save_nickname)
        nick_layout.addWidget(nick_save, alignment=Qt.AlignmentFlag.AlignRight)
        body_layout.addWidget(nick_card)

        # ---- 本机 IP ----
        ip_card = QFrame()
        ip_card.setObjectName("card")
        ip_layout = QVBoxLayout(ip_card)
        ip_layout.setContentsMargins(16, 14, 16, 14)
        ip_layout.setSpacing(8)
        ip_title = QLabel("本机 IP")
        ip_title.setStyleSheet("font-size: 14px; font-weight: 600;")
        ip_layout.addWidget(ip_title)
        ip_hint = QLabel("其他设备通过它连接你；创建群组后把 \u201cIP:端口\u201d 分享给成员")
        ip_hint.setObjectName("faint")
        ip_hint.setWordWrap(True)
        ip_layout.addWidget(ip_hint)
        ip_row = QHBoxLayout()
        self.ip_label = QLabel("")
        self.ip_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        ip_row.addWidget(self.ip_label, 1)
        ip_copy = QPushButton("复制")
        ip_copy.setObjectName("ghost")
        ip_copy.clicked.connect(self._copy_ip)
        ip_row.addWidget(ip_copy)
        ip_layout.addLayout(ip_row)
        body_layout.addWidget(ip_card)

        # ---- 端口 ----
        port_card = QFrame()
        port_card.setObjectName("card")
        port_layout = QVBoxLayout(port_card)
        port_layout.setContentsMargins(16, 14, 16, 14)
        port_layout.setSpacing(8)
        port_title = QLabel("本机端口")
        port_title.setStyleSheet("font-size: 14px; font-weight: 600;")
        port_layout.addWidget(port_title)
        port_hint = QLabel(
            f"整个程序使用同一个端口（默认 {network_module.TCP_PORT}），所有群组和直聊都通过它。"
            "修改后立即生效，需要把新的 \u201cIP:端口\u201d 地址分享给其他人。"
        )
        port_hint.setObjectName("faint")
        port_hint.setWordWrap(True)
        port_layout.addWidget(port_hint)
        self.port_edit = QLineEdit(str(self.vm.local_port))
        self.port_edit.setMaximumWidth(160)
        self.port_edit.setMinimumHeight(36)
        port_layout.addWidget(self.port_edit)
        self.port_error = QLabel("")
        self.port_error.setStyleSheet("font-size: 12px; color: #B3261E;")
        self.port_error.hide()
        port_layout.addWidget(self.port_error)
        port_save = QPushButton("保存端口")
        port_save.setObjectName("outline")
        port_save.clicked.connect(self._save_port)
        port_layout.addWidget(port_save, alignment=Qt.AlignmentFlag.AlignRight)
        body_layout.addWidget(port_card)

        # ---- 本机安全码 ----
        sec_card = QFrame()
        sec_card.setObjectName("card")
        sec_layout = QVBoxLayout(sec_card)
        sec_layout.setContentsMargins(16, 14, 16, 14)
        sec_layout.setSpacing(8)
        sec_title = QLabel("本机安全码")
        sec_title.setStyleSheet("font-size: 14px; font-weight: 600;")
        sec_layout.addWidget(sec_title)
        sec_hint = QLabel(
            "设备身份密钥的指纹。直聊和音视频通话全程加密；首次联系后若对方"
            "身份变化会自动拒绝连接。可与对方当面核对安全码，完全排除中间人。"
        )
        sec_hint.setObjectName("faint")
        sec_hint.setWordWrap(True)
        sec_layout.addWidget(sec_hint)
        sec_row = QHBoxLayout()
        self.security_label = QLabel("")
        self.security_label.setStyleSheet("font-size: 16px; font-weight: 600; letter-spacing: 2px;")
        sec_row.addWidget(self.security_label, 1)
        self.copy_security_btn = QPushButton("复制")
        self.copy_security_btn.setObjectName("ghost")
        self.copy_security_btn.clicked.connect(self._copy_security_code)
        sec_row.addWidget(self.copy_security_btn)
        sec_layout.addLayout(sec_row)
        body_layout.addWidget(sec_card)

        body_layout.addStretch()
        self.refresh()

    def refresh(self):
        self.nick_edit.setText(self.vm.nickname or "")
        ip = self.vm.local_ip
        self.ip_label.setText(ip if ip else "未连接到网络")
        self.port_edit.setText(str(self.vm.local_port))
        code = self.vm.security_code
        self.security_label.setText(code if code else "未生成")
        self.copy_security_btn.setEnabled(bool(code))

    def _copy_security_code(self):
        code = self.vm.security_code
        if not code:
            return
        QApplication.clipboard().setText(code)
        Toast(self.window()).show_message("已复制安全码")

    def _save_nickname(self):
        nick = self.nick_edit.text().strip()
        if not nick:
            Toast(self.window()).show_message("昵称不能为空")
            return
        self.vm.set_nickname(nick)
        Toast(self.window()).show_message("昵称已保存")

    def _copy_ip(self):
        ip = self.vm.local_ip
        if not ip:
            return
        QApplication.clipboard().setText(ip)
        Toast(self.window()).show_message("已复制IP")

    def _save_port(self):
        text = self.port_edit.text().strip()
        # isascii() first: str.isdigit() alone is True for superscripts ("²")
        # and other unicode digits, which int() then rejects with ValueError
        if not (text.isascii() and text.isdigit()) or not (1 <= int(text) <= 65535):
            self.port_error.setText("端口必须在 1-65535 之间")
            self.port_error.show()
            return
        self.port_error.hide()
        self.vm.set_port(int(text))
