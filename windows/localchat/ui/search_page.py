"""Global message search page.

Searches persisted group + direct-chat history (ChatStore.search_messages)
with an optional conversation scope; every hit shows 会话名 + 发送者 + 摘要 +
时间. Activating a hit is delegated to MainWindow (on_activate), which opens
the conversation and asks the chat page to scroll to and highlight that
message.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..models import FILE_KIND_IMAGE, FILE_KIND_VIDEO
from ..view_model import ChatViewModel
from .theme import PRIMARY
from .widgets import format_group_time

# How much of the matched message body is shown in a result row.
SNIPPET_MAX = 60


def message_preview(msg) -> str:
    """One-line preview of a hit: media/file messages get a bracketed kind
    prefix, a folder entry shows its relative path, text shows its body."""
    text = (msg.content or "").replace("\n", " ").strip()
    is_file = (
        msg.file_size > 0
        or bool(msg.relative_path)
        or bool(msg.folder_name)
        or bool(msg.folder_id)
    )
    if not is_file:
        return text if len(text) <= SNIPPET_MAX else text[:SNIPPET_MAX] + "..."
    if bool(msg.relative_path):
        text = msg.relative_path
    prefix = {
        FILE_KIND_IMAGE: "[图片] ",
        FILE_KIND_VIDEO: "[视频] ",
    }.get(msg.kind, "[文件] ")
    body = prefix + text
    return body if len(body) <= SNIPPET_MAX else body[:SNIPPET_MAX] + "..."


class SearchResultCard(QFrame):
    def __init__(self, hit: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        msg = hit["message"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(8)
        conversation = QLabel(hit["conversation"])
        conversation.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {PRIMARY};"
        )
        top.addWidget(conversation, 1)
        time_label = QLabel(format_group_time(msg.timestamp))
        time_label.setStyleSheet("font-size: 11px; color: #A7A2AF;")
        top.addWidget(time_label, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top)

        sender = "我" if msg.is_from_me else (msg.sender_name or "")
        preview = QLabel(f"{sender}：{message_preview(msg)}")
        preview.setObjectName("faint")
        preview.setWordWrap(False)
        layout.addWidget(preview)


class SearchPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_activate, on_back):
        super().__init__()
        self.vm = vm
        self.on_activate = on_activate
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
        title = QLabel("搜索消息")
        title.setStyleSheet("font-size: 16px; font-weight: 700;")
        header_layout.addWidget(title, 1)
        layout.addWidget(header)

        filters = QFrame()
        filters_layout = QHBoxLayout(filters)
        filters_layout.setContentsMargins(16, 4, 16, 8)
        filters_layout.setSpacing(8)
        self.scope_combo = QComboBox()
        self.scope_combo.setMinimumWidth(160)
        filters_layout.addWidget(self.scope_combo)
        self.keyword_edit = QLineEdit()
        self.keyword_edit.setPlaceholderText("输入关键词，搜索群聊与私聊记录")
        self.keyword_edit.setMinimumHeight(38)
        self.keyword_edit.returnPressed.connect(self._run_search)
        filters_layout.addWidget(self.keyword_edit, 1)
        search_btn = QPushButton("搜索")
        search_btn.setMinimumSize(80, 38)
        search_btn.clicked.connect(self._run_search)
        filters_layout.addWidget(search_btn)
        layout.addWidget(filters)

        self.status_label = QLabel("输入关键词后回车或点击“搜索”")
        self.status_label.setObjectName("faint")
        self.status_label.setContentsMargins(16, 0, 16, 6)
        layout.addWidget(self.status_label)

        self.results = QListWidget()
        self.results.setSpacing(6)
        self.results.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.results.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.results, 1)

    def refresh_scopes(self) -> None:
        """Rebuild the range selector: 全部 + every known conversation."""
        current = None
        data = self.scope_combo.currentData()
        if data is not None:
            current = data
        self.scope_combo.clear()
        self.scope_combo.addItem("全部会话", None)
        index = 0
        for pos, scope in enumerate(self.vm.search_scopes(), start=1):
            self.scope_combo.addItem(scope["name"], scope["id"])
            if current is not None and scope["id"] == current:
                index = pos
        self.scope_combo.setCurrentIndex(index)

    def refresh(self) -> None:
        self.refresh_scopes()

    def focus_keyword(self) -> None:
        self.keyword_edit.setFocus()

    def _run_search(self) -> None:
        keyword = self.keyword_edit.text().strip()
        self.results.clear()
        if not keyword:
            self.status_label.setText("请输入关键词")
            return
        scope_id = self.scope_combo.currentData()
        try:
            hits = self.vm.search_history(keyword, scope_id)
        except Exception as exc:  # never let a search crash the window
            self.status_label.setText(f"搜索失败：{exc}")
            return
        if not hits:
            self.status_label.setText("没有找到匹配的消息")
            return
        self.status_label.setText(f"找到 {len(hits)} 条消息")
        for hit in hits:
            item = QListWidgetItem()
            card = SearchResultCard(hit)
            item.setData(Qt.ItemDataRole.UserRole, hit)
            item.setSizeHint(card.sizeHint())
            self.results.addItem(item)
            self.results.setItemWidget(item, card)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        hit = item.data(Qt.ItemDataRole.UserRole)
        if hit:
            self.on_activate(hit)
