"""QR dialogs: show this device's QR, and import a scanned/typed payload.

Generation degrades gracefully: without the optional `qrcode` package the
dialogs show the payload text with a copy button (plus a note that installs
`qrcode` to render the image). Import accepts an image file (OpenCV, already
a hard dependency, decodes it) or pasted payload text.
"""

import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import qrshare
from ..view_model import ChatViewModel
from .theme import ERROR
from .widgets import Toast


def qr_pixmap(text: str, size: int = 220) -> QPixmap:
    """Render a QR matrix to a QPixmap; an empty pixmap when `qrcode` is
    missing (the dialog shows the fallback text instead)."""
    matrix = qrshare.qr_matrix(text)
    if not matrix:
        return QPixmap()
    rows = len(matrix)
    scale = max(1, size // rows)
    quiet = 2  # the matrix already carries a border, keep it crisp anyway
    side = (rows + 2 * quiet) * scale
    pm = QPixmap(side, side)
    pm.fill(Qt.GlobalColor.white)
    painter = QPainter(pm)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1C1B1F"))
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                painter.drawRect(
                    (c + quiet) * scale, (r + quiet) * scale, scale - 1, scale - 1
                )
    painter.end()
    return pm


def _copy_text(widget: QWidget, text: str, toast: str) -> None:
    QApplication.clipboard().setText(text)
    Toast(widget.window()).show_message(toast)


class _PayloadQrDialog(QDialog):
    """Base: QR image (or fallback text) + copyable payload + optional
    extra rows (security code / group id)."""

    def __init__(self, title: str, payload: str, extra_rows=(), parent=None):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(380)
        self.payload = payload

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(8)

        pm = qr_pixmap(payload)
        if not pm.isNull():
            image = QLabel()
            image.setPixmap(pm)
            image.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(image)
        else:
            note = QLabel(
                "未安装 qrcode 库，无法显示二维码图片。\n"
                "可安装后重试（pip install qrcode），或直接复制下方文本"
                "发送给对方。"
            )
            note.setObjectName("faint")
            note.setWordWrap(True)
            note.setStyleSheet(f"font-size: 12px; color: {ERROR};")
            layout.addWidget(note)

        for label, value in extra_rows:
            row_label = QLabel(label)
            row_label.setObjectName("hint")
            layout.addWidget(row_label)
            row = QHBoxLayout()
            value_label = QLabel(value)
            value_label.setStyleSheet("font-size: 14px; font-weight: 600;")
            value_label.setWordWrap(True)
            row.addWidget(value_label, 1)
            copy_btn = QPushButton("复制")
            copy_btn.setObjectName("ghost")
            copy_btn.clicked.connect(
                lambda checked=False, v=value: _copy_text(self, v, "已复制")
            )
            row.addWidget(copy_btn)
            layout.addLayout(row)

        text_label = QLabel("二维码内容")
        text_label.setObjectName("hint")
        layout.addWidget(text_label)
        text_row = QHBoxLayout()
        self.payload_edit = QLineEdit(payload)
        self.payload_edit.setReadOnly(True)
        text_row.addWidget(self.payload_edit, 1)
        payload_copy = QPushButton("复制")
        payload_copy.setObjectName("ghost")
        payload_copy.clicked.connect(
            lambda checked=False: _copy_text(self, payload, "已复制内容")
        )
        text_row.addWidget(payload_copy)
        layout.addLayout(text_row)

        layout.addSpacing(4)
        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("ghost")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)


class ContactQrDialog(_PayloadQrDialog):
    """「我的二维码」: contact payload + the security code so the peer can
    compare it out-of-band before accepting."""

    def __init__(self, vm: ChatViewModel, parent=None):
        payload = vm.qr_contact_payload()
        code = vm.security_code
        rows = [
            ("名字", vm.nickname or "用户"),
            ("本机安全码（请与对方核对）", code if code else "未生成"),
        ]
        super().__init__("我的二维码", payload, rows, parent)


class GroupInviteQrDialog(_PayloadQrDialog):
    """「群邀请二维码」: numeric id + endpoint (+ relay). The password is
    deliberately absent: joiners type it themselves after scanning."""

    def __init__(self, vm: ChatViewModel, group_id: str, group_name: str, parent=None):
        payload = qrshare.encode_group_invite(
            group_id=group_id,
            name=group_name,
            ip=vm.local_ip,
            port=vm.local_port,
            relay=vm.signaling_server,
        )
        rows = [("群组名称", group_name)]
        super().__init__("群邀请二维码", payload, rows, parent)


class QrImportDialog(QDialog):
    """「扫二维码」 on desktop: decode a QR from an image file, or paste the
    payload text. Image import needs OpenCV (qrshare.scanner_available)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("confirmDialog")
        self.setWindowTitle("导入二维码")
        self.setModal(True)
        self.setMinimumWidth(380)
        self.payload: str = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(8)

        hint = QLabel(
            "让对方出示二维码，选择其一：\n"
            "1. 从图片文件导入二维码（手机截图等）；\n"
            "2. 直接粘贴二维码内容文本。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        if qrshare.scanner_available():
            image_btn = QPushButton("从图片导入二维码…")
            image_btn.setObjectName("outline")
            image_btn.clicked.connect(self._pick_image)
            layout.addWidget(image_btn)
        else:
            note = QLabel(
                "未安装 opencv-python，无法从图片识别二维码。"
                "可安装后重试（pip install opencv-python），或直接粘贴文本。"
            )
            note.setObjectName("faint")
            note.setWordWrap(True)
            note.setStyleSheet(f"font-size: 12px; color: {ERROR};")
            layout.addWidget(note)

        paste_label = QLabel("粘贴二维码内容")
        paste_label.setObjectName("hint")
        layout.addWidget(paste_label)
        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("localchat://contact?... 或 localchat://group?...")
        self.text_edit.setMinimumHeight(38)
        layout.addWidget(self.text_edit)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"font-size: 12px; color: {ERROR};")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        ok_btn = QPushButton("确定")
        ok_btn.clicked.connect(self._on_confirm)
        buttons.addWidget(ok_btn)
        layout.addLayout(buttons)

    def _set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择二维码图片", os.path.expanduser("~"), "图片 (*.png *.jpg *.jpeg *.bmp)"
        )
        if not path:
            return
        text = qrshare.decode_qr_image_file(path)
        if not text:
            self._set_error("图片中未识别到二维码，请换一张或直接粘贴文本")
            return
        self.text_edit.setText(text)

    def _on_confirm(self) -> None:
        text = self.text_edit.text().strip()
        if not text:
            self._set_error("请先粘贴二维码内容或导入图片")
            return
        self.payload = text
        self.accept()


def parse_scanned(parent: QWidget, vm: ChatViewModel, text: str):
    """Decode a scanned/imported payload; toasts on garbage. Returns the
    ContactInvite / GroupInvite or None."""
    invite = vm.parse_qr_invite(text)
    if invite is None:
        Toast(parent.window()).show_message("二维码内容无法识别")
    return invite


def confirm_contact_invite(parent: QWidget, vm: ChatViewModel, invite) -> bool:
    """Show a scanned contact card with its 安全码 and add it on confirm.

    The security code comparison is mandatory UX (never hidden), and the
    fingerprint is pinned so the first handshake must present exactly that
    identity — an impostor between scan and dial is refused by cryptography,
    not just by eyeballing."""
    box = QMessageBox(parent.window())
    box.setWindowTitle("添加联系人")
    box.setText(
        f"扫码识别到联系人：\n\n名字：{invite.name or invite.ip}\n"
        f"地址：{invite.ip}:{invite.port}\n对方安全码：{invite.fingerprint or '（二维码未含安全码）'}\n"
    )
    box.setInformativeText(
        "请与对方「设置 → 本机安全码」当面核对一致后再添加；\n"
        "不一致说明可能存在中间人。添加后首次连接将校验该安全码。"
    )
    add_btn = box.addButton("添加", QMessageBox.ButtonRole.AcceptRole)
    cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is not add_btn:
        return False
    return vm.add_direct_contact(
        f"{invite.ip}:{invite.port}",
        invite.name,
        expected_fingerprint=invite.fingerprint,
    )


def open_import_dialog(parent: QWidget, title: str = "扫二维码"):
    """Run the import dialog; returns the raw payload text or None."""
    dialog = QrImportDialog(parent.window())
    dialog.setWindowTitle(title)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return dialog.payload or None
