"""群文件 (group file share area) dialog.

Lists the active group's shared-file index (name / uploader / size / time)
and exposes the share / download / delete actions. The bytes are served from
the uploader's shared listener (stable port); downloads reuse the chat-file
transfer contract (resume + progress). Only the uploader or the group owner
may delete an entry — the button is hidden otherwise and the ViewModel
re-checks authorization.
"""

import time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .theme import TEXT_SUBTLE
from .widgets import Toast


def _format_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _format_time(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts_ms / 1000.0))
    except (OverflowError, OSError, ValueError):
        return ""


class GroupFilesDialog(QDialog):
    """The 「群文件」 page: list + share + download + delete."""

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self.vm = vm
        self.group_id = vm.active_group_id
        self._entries = {}
        self._downloading_id = ""
        self.setWindowTitle("群文件")
        self.setObjectName("confirmDialog")
        self.setMinimumSize(480, 400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("群文件")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        header.addWidget(title)
        hint = QLabel("群内共享文件（与聊天文件分开，长期保留）")
        hint.setObjectName("faint")
        header.addWidget(hint, 1)
        self.share_btn = QPushButton("分享文件...")
        self.share_btn.setObjectName("outline")
        self.share_btn.clicked.connect(self._on_share)
        header.addWidget(self.share_btn)
        layout.addLayout(header)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setObjectName("fileList")
        self.list.itemSelectionChanged.connect(self._update_actions)
        self.list.itemDoubleClicked.connect(lambda _item: self._on_download())
        layout.addWidget(self.list, 1)

        self.empty_label = QLabel("暂无群文件，点击右上角「分享文件...」上传。")
        self.empty_label.setObjectName("faint")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)

        self.status_label = QLabel("")
        self.status_label.setObjectName("faint")
        self.status_label.setStyleSheet(f"color: {TEXT_SUBTLE};")
        layout.addWidget(self.status_label)

        actions = QHBoxLayout()
        self.download_btn = QPushButton("下载")
        self.download_btn.setObjectName("outline")
        self.download_btn.clicked.connect(self._on_download)
        actions.addWidget(self.download_btn)
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self._on_delete)
        actions.addWidget(self.delete_btn)
        actions.addStretch(1)
        close_btn = QPushButton("关闭")
        close_btn.setObjectName("ghost")
        close_btn.clicked.connect(self.accept)
        actions.addWidget(close_btn)
        layout.addLayout(actions)

        self.vm.group_files_changed.connect(self._on_files_changed)
        self.vm.file_progress.connect(self._on_progress)
        self.vm.file_download_finished.connect(self._on_download_finished)
        self.finished.connect(self._detach)
        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self):
        selected = self._selected_file_id()
        self.list.clear()
        self._entries = {}
        entries = self.vm.group_files(self.group_id) if self.group_id else []
        for entry in entries:
            self._entries[entry.file_id] = entry
            subtitle = " · ".join(
                part
                for part in (
                    entry.sender_name or entry.sender_id,
                    _format_size(entry.size),
                    _format_time(entry.ts),
                )
                if part
            )
            item = QListWidgetItem(f"{entry.name}\n{subtitle}")
            item.setData(Qt.ItemDataRole.UserRole, entry.file_id)
            self.list.addItem(item)
            if entry.file_id == selected:
                item.setSelected(True)
        has_any = bool(entries)
        self.list.setVisible(has_any)
        self.empty_label.setVisible(not has_any)
        self._update_actions()

    def _selected_file_id(self) -> str:
        items = self.list.selectedItems()
        if not items:
            return ""
        return str(items[0].data(Qt.ItemDataRole.UserRole) or "")

    def _selected_entry(self):
        return self._entries.get(self._selected_file_id())

    def _update_actions(self):
        entry = self._selected_entry()
        self.download_btn.setEnabled(entry is not None and not self._downloading_id)
        can_remove = entry is not None and self.vm.can_remove_shared_file(entry)
        self.delete_btn.setEnabled(can_remove and not self._downloading_id)

    # --------------------------------------------------------------- actions

    def _on_share(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要分享的文件")
        if not path:
            return
        if not self.vm.share_group_file(path):
            Toast(self.window()).show_message("分享失败：请检查连接或文件大小")
            return
        self.refresh()

    def _on_download(self):
        entry = self._selected_entry()
        if entry is None or self._downloading_id:
            return
        target, _ = QFileDialog.getSaveFileName(self, "保存文件", entry.name)
        if not target:
            return
        self._downloading_id = entry.file_id
        self.status_label.setText(f"正在下载 {entry.name}...")
        self._update_actions()
        self.vm.download_group_file(entry.file_id, target)

    def _on_delete(self):
        entry = self._selected_entry()
        if entry is None:
            return
        box = QMessageBox(self.window())
        box.setWindowTitle("删除群文件")
        box.setText(f"确定从群文件删除「{entry.name}」吗？所有成员都会看到该文件被移除。")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is not delete_btn:
            return
        if not self.vm.remove_group_file(entry.file_id):
            Toast(self.window()).show_message("删除失败：没有权限或未连接")
            return
        self.refresh()

    # ----------------------------------------------------------------- signals

    def _on_files_changed(self, group_id: str):
        if group_id == self.group_id:
            self.refresh()

    def _on_progress(self, file_id: str, received: int, total: int):
        if file_id != self._downloading_id:
            return
        total = max(0, total)
        percent = int(received * 100 / total) if total else 0
        self.status_label.setText(
            f"正在下载... {percent}%（{_format_size(received)}/{_format_size(total)}）"
        )

    def _on_download_finished(self, file_id: str, ok: bool, message: str):
        if file_id != self._downloading_id:
            return
        self._downloading_id = ""
        self.status_label.setText("" if ok else f"下载失败：{message}")
        if ok:
            Toast(self.window()).show_message("群文件下载完成")
        self._update_actions()

    def _detach(self):
        for signal, slot in (
            (self.vm.group_files_changed, self._on_files_changed),
            (self.vm.file_progress, self._on_progress),
            (self.vm.file_download_finished, self._on_download_finished),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                pass
