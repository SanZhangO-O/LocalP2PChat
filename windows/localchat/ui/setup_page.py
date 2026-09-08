from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..models import GroupInfo
from ..view_model import MAX_NAME_LENGTH, ChatViewModel
from .widgets import Toast

MODE_SELECT = 0
MODE_CREATE = 1
MODE_JOIN = 2


class ConfirmJoinDialog(QDialog):
    def __init__(self, vm: ChatViewModel, group_info: GroupInfo, parent=None):
        super().__init__(parent)
        self.vm = vm
        self.setObjectName("confirmDialog")
        self.setWindowTitle("确认加入群组")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(8)

        hint = QLabel("找到以下群组，请确认是否加入：")
        hint.setObjectName("hint")
        layout.addWidget(hint)
        layout.addSpacing(6)

        rows = [
            ("群组名称", group_info.group_name),
            ("创建者", group_info.creator_name),
            ("当前成员数", f"{group_info.member_count}人"),
        ]
        for label, value in rows:
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setObjectName("hint")
            row.addWidget(lbl)
            row.addStretch()
            val = QLabel(value)
            val.setStyleSheet("font-size: 13px; font-weight: 600;")
            row.addWidget(val)
            layout.addLayout(row)

        self.loading_row = QHBoxLayout()
        self.loading_label = QLabel("正在加入...")
        self.loading_label.setObjectName("hint")
        self.loading_row.addWidget(self.loading_label)
        self.loading_row.addStretch()
        self.loading_row.setParent(self)
        layout.addLayout(self.loading_row)

        layout.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.clicked.connect(self._on_cancel)
        buttons.addWidget(self.cancel_btn)
        self.confirm_btn = QPushButton("确认加入")
        self.confirm_btn.clicked.connect(self._on_confirm)
        buttons.addWidget(self.confirm_btn)
        layout.addLayout(buttons)

        # set_loading touches confirm_btn/cancel_btn, so it must run after
        # they are created (crashing __init__ before this crashed the whole
        # app from inside a Qt slot)
        self.set_loading(False)

        self.vm.join_ui_state_changed.connect(self._on_join_state_changed)

    def set_loading(self, loading: bool):
        self.loading_label.setVisible(loading)
        self.confirm_btn.setEnabled(not loading)
        self.cancel_btn.setEnabled(not loading)

    def _on_confirm(self):
        self.set_loading(True)
        self.vm.confirm_join()

    def _on_cancel(self):
        if self.vm.is_joining():
            return
        self.vm.cancel_join()
        self.accept()

    def _on_join_state_changed(self):
        result = self.vm.connection_result()
        if result is None:
            self.set_loading(self.vm.is_joining())
            return
        self.set_loading(False)
        self.accept()

    def closeEvent(self, event):
        self.vm.join_ui_state_changed.disconnect(self._on_join_state_changed)
        super().closeEvent(event)


class SetupPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_back, on_group_entered):
        super().__init__()
        self.vm = vm
        self.on_back = on_back
        self.on_group_entered = on_group_entered
        self._dialog: ConfirmJoinDialog | None = None
        self._error_shown = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 8)
        back_btn = QPushButton("← 返回")
        back_btn.setObjectName("ghost")
        back_btn.clicked.connect(self._go_back)
        header_layout.addWidget(back_btn)
        header_layout.addStretch()
        layout.addWidget(header)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_mode_select())
        self.stack.addWidget(self._build_create_form())
        self.stack.addWidget(self._build_join_form())
        layout.addWidget(self.stack, 1)

        self.vm.query_state_changed.connect(self._on_query_state_changed)
        self.vm.join_ui_state_changed.connect(self._on_join_state_changed)
        self.vm.create_failed.connect(self._on_create_failed)
        self._refresh_address()
        self.show_mode(MODE_SELECT)

    def _build_mode_select(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(48, 24, 48, 24)
        layout.setSpacing(8)

        layout.addStretch()
        title = QLabel("LocalChat")
        title.setObjectName("pageTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        subtitle = QLabel("局域网群组聊天")
        subtitle.setObjectName("pageSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)
        layout.addSpacing(36)

        self.address_card = QFrame()
        self.address_card.setObjectName("card")
        card_layout = QVBoxLayout(self.address_card)
        card_layout.setContentsMargins(16, 10, 16, 10)
        addr_title = QLabel("本机地址")
        addr_title.setObjectName("faint")
        addr_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(addr_title)
        addr_row = QHBoxLayout()
        self.address_label = QLabel("")
        self.address_label.setStyleSheet("font-size: 17px; font-weight: 600;")
        addr_row.addWidget(self.address_label, 1)
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setObjectName("ghost")
        self.copy_btn.setStyleSheet("font-size: 12px;")
        self.copy_btn.clicked.connect(self._copy_address)
        addr_row.addWidget(self.copy_btn)
        card_layout.addLayout(addr_row)
        layout.addWidget(self.address_card)

        layout.addSpacing(24)
        self.create_btn = QPushButton("创建群组")
        self.create_btn.setMinimumHeight(46)
        self.create_btn.clicked.connect(lambda: self.show_mode(MODE_CREATE))
        layout.addWidget(self.create_btn)
        self.select_error_label = QLabel("")
        self.select_error_label.setObjectName("errorLabel")
        self.select_error_label.setWordWrap(True)
        self.select_error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.select_error_label.hide()
        layout.addWidget(self.select_error_label)
        layout.addSpacing(6)

        join_btn = QPushButton("加入群组")
        join_btn.setObjectName("outline")
        join_btn.setMinimumHeight(46)
        join_btn.clicked.connect(lambda: self.show_mode(MODE_JOIN))
        layout.addWidget(join_btn)

        back_btn = QPushButton("返回")
        back_btn.setObjectName("ghost")
        back_btn.clicked.connect(self.on_back)
        layout.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        return page

    def _form_page(self, title_text, fields: list, button_text, button_callback, back_callback, enabled_check=None) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(48, 12, 48, 24)
        layout.setSpacing(8)

        layout.addStretch()
        title = QLabel(title_text)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        layout.addSpacing(20)

        inputs = []
        for name, placeholder in fields:
            lbl = QLabel(name)
            lbl.setObjectName("hint")
            layout.addWidget(lbl)
            edit = QLineEdit()
            edit.setPlaceholderText(placeholder)
            edit.setMinimumHeight(40)
            layout.addWidget(edit)
            inputs.append(edit)

        layout.addSpacing(8)
        self.error_label = QLabel("")
        self.error_label.setObjectName("errorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)

        layout.addSpacing(8)
        btn = QPushButton(button_text)
        btn.setMinimumHeight(46)
        btn.clicked.connect(button_callback)
        layout.addWidget(btn)
        back_btn = QPushButton("返回")
        back_btn.setObjectName("ghost")
        back_btn.clicked.connect(back_callback)
        layout.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        return page, inputs, btn

    def _build_create_form(self) -> QWidget:
        page, inputs, btn = self._form_page(
            "创建群组",
            [("你的昵称", ""), ("群组名称", "")],
            "创建",
            self._on_create,
            lambda: self.show_mode(MODE_SELECT),
        )
        self.create_name_edit, self.create_group_edit = inputs
        self.create_submit_btn = btn
        for edit in (self.create_name_edit, self.create_group_edit):
            edit.setMaxLength(MAX_NAME_LENGTH)
        page_layout = page.layout()
        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 10, 16, 10)
        addr_title = QLabel("本机地址（分享给其他人加入）")
        addr_title.setObjectName("faint")
        addr_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(addr_title)
        addr_row = QHBoxLayout()
        self.create_address_label = QLabel("")
        self.create_address_label.setStyleSheet("font-size: 16px; font-weight: 600;")
        addr_row.addWidget(self.create_address_label, 1)
        copy_btn = QPushButton("复制")
        copy_btn.setObjectName("ghost")
        copy_btn.setStyleSheet("font-size: 12px;")
        copy_btn.clicked.connect(self._copy_address)
        addr_row.addWidget(copy_btn)
        card_layout.addLayout(addr_row)
        page_layout.insertWidget(page_layout.indexOf(self.create_submit_btn), card)
        self.create_name_edit.textChanged.connect(self._on_create_input_changed)
        self.create_group_edit.textChanged.connect(self._on_create_input_changed)
        self._on_create_input_changed()
        return page

    def _build_join_form(self) -> QWidget:
        page, inputs, btn = self._form_page(
            "加入群组",
            [
                ("你的昵称", ""),
                ("群组数字ID", "例如: 4829 1357"),
                ("创建者的IP地址（可含端口）", "例如: 192.168.1.100:9999"),
                ("群组密码（可选）", "创建者分享的8位密码"),
            ],
            "查找群组",
            self._on_query,
            lambda: self.show_mode(MODE_SELECT),
        )
        self.join_name_edit, self.join_group_edit, self.join_ip_edit, self.join_password_edit = inputs
        self.join_submit_btn = btn
        self.join_name_edit.setMaxLength(MAX_NAME_LENGTH)
        ip_hint = QLabel("ID 由创建者设备指纹生成，与群名无关；地址可在创建者的“本机地址”卡片中点击复制")
        ip_hint.setObjectName("faint")
        ip_hint.setWordWrap(True)
        page.layout().insertWidget(page.layout().indexOf(self.join_submit_btn), ip_hint)
        self.join_name_edit.textChanged.connect(self._on_join_input_changed)
        self.join_group_edit.textChanged.connect(self._on_join_input_changed)
        self.join_ip_edit.textChanged.connect(self._on_join_input_changed)
        self.join_password_edit.textChanged.connect(self._on_join_input_changed)
        self._on_join_input_changed()
        return page

    def refresh(self):
        """Re-evaluate page state every time it is shown."""
        self._refresh_address()
        self.show_mode(MODE_SELECT)

    def show_mode(self, mode: int):
        self.stack.setCurrentIndex(mode)
        if mode == MODE_CREATE:
            if self.vm.nickname:
                self.create_name_edit.setText(self.vm.nickname)
            self._refresh_address_labels()
        elif mode == MODE_JOIN:
            if self.vm.nickname:
                self.join_name_edit.setText(self.vm.nickname)
            self._clear_error()

    def _go_back(self):
        if self.stack.currentIndex() == MODE_SELECT:
            self.on_back()
        else:
            self.show_mode(MODE_SELECT)

    def _refresh_address(self):
        ip = self.vm.local_ip
        address = f"{ip}:{self.vm.local_port}" if ip else "未连接到网络"
        self.address_label.setText(address)
        self.create_address_label.setText(address)
        self.copy_btn.setEnabled(bool(ip))

    def _refresh_address_labels(self):
        ip = self.vm.local_ip
        address = f"{ip}:{self.vm.local_port}" if ip else "未连接到网络"
        self.create_address_label.setText(address)

    def _copy_address(self):
        from PyQt6.QtWidgets import QApplication

        ip = self.vm.local_ip
        if not ip:
            return
        address = f"{ip}:{self.vm.local_port}"
        QApplication.clipboard().setText(address)
        self._show_toast("已复制地址")

    def _show_toast(self, text: str):
        toast = Toast(self.window())
        toast.show_message(text)

    def _set_error(self, message: str | None):
        if message:
            self.error_label.setText(message)
            self.error_label.show()
            self._error_shown = True
        else:
            self.error_label.hide()
            self._error_shown = False

    def _clear_error(self):
        if self._error_shown:
            self._set_error(None)

    def _set_select_error(self, message: str | None):
        if message:
            self.select_error_label.setText(message)
            self.select_error_label.show()
        else:
            self.select_error_label.hide()

    def _on_create_failed(self, message: str):
        self.show_mode(MODE_SELECT)
        self._set_select_error(message)

    def _on_create(self):
        # The same group name derives the same group id, so re-creating with
        # an existing name silently REPLACES the old instance; ask first.
        name = self.create_group_edit.text().strip()
        if self.vm.group_name_exists(name):
            box = QMessageBox(self.window())
            box.setWindowTitle("创建群组")
            box.setText("已存在同名群组，重新创建将替换原群组（聊天记录保留），是否继续？")
            recreate_btn = box.addButton("继续创建", QMessageBox.ButtonRole.DestructiveRole)
            cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is not recreate_btn:
                return
        self.vm.create_group(self.create_name_edit.text(), self.create_group_edit.text())
        self.on_group_entered()

    def _on_create_input_changed(self):
        enabled = bool(self.create_name_edit.text().strip() and self.create_group_edit.text().strip())
        self.create_submit_btn.setEnabled(enabled)

    def _on_query(self):
        self._set_error(None)
        self.vm.query_group(
            self.join_name_edit.text(),
            self.join_group_edit.text(),
            self.join_ip_edit.text(),
            password=self.join_password_edit.text().strip() or None,
        )

    def _on_join_input_changed(self):
        join_id = "".join(ch for ch in self.join_group_edit.text() if ch.isdigit())
        enabled = bool(
            self.join_name_edit.text().strip()
            and len(join_id) == 8
            and self.join_ip_edit.text().strip()
        )
        self.join_submit_btn.setEnabled(enabled)
        self._clear_error()

    def _on_query_state_changed(self):
        error = self.vm.query_error()
        if error:
            self._set_error(error)
        join_id = "".join(ch for ch in self.join_group_edit.text() if ch.isdigit())
        self.join_submit_btn.setEnabled(
            not self.vm.is_querying_group()
            and bool(self.join_name_edit.text().strip())
            and len(join_id) == 8
            and bool(self.join_ip_edit.text().strip())
        )
        self._update_confirm_dialog()

    def _on_join_state_changed(self):
        self.join_submit_btn.setEnabled(
            not self.vm.is_joining()
            and bool(self.join_name_edit.text().strip())
            and bool(self.join_group_edit.text().strip())
            and bool(self.join_ip_edit.text().strip())
        )
        result = self.vm.connection_result()
        if result is not None and not result[0] and self.stack.currentIndex() == MODE_JOIN:
            self._set_error(result[1])
        self._update_confirm_dialog()

    def _update_confirm_dialog(self):
        info = self.vm.queried_group_info()
        result = self.vm.connection_result()
        show_dialog = (
            self.stack.currentIndex() == MODE_JOIN
            and info is not None
            and (result is None or result[0])
        )
        if show_dialog and self._dialog is None:
            self._dialog = ConfirmJoinDialog(self.vm, info, self.window())
            self._dialog.finished.connect(self._on_dialog_finished)
            self._dialog.show()
        elif not show_dialog and self._dialog is not None:
            self._dialog.accept()

    def _on_dialog_finished(self, _result):
        self._dialog = None
