"""Unicode emoji picker for the chat inputs.

A frameless popup (Qt.Popup, so an outside click dismisses it) with one tab
per emoji category and a "最近" tab holding the 24 most recently used emoji,
persisted in the local settings table (no images / sticker packs: plain
Unicode characters). Clicking an emoji emits emoji_picked and records it in
the recents list; the panel stays open so several emoji can be inserted at
once.
"""

import json

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

RECENT_KEY = "recent_emoji"
RECENT_CAP = 24
GRID_COLUMNS = 8
RECENT_CATEGORY = "最近"

# Ordered (category, emoji) groups. Plain literals: the picker inserts the
# character itself, whatever the input field supports.
EMOJI_CATEGORIES = [
    (RECENT_CATEGORY, ()),
    (
        "笑脸",
        (
            "😀", "😃", "😄", "😁", "😆", "😅", "😂", "🤣",
            "😊", "😇", "🙂", "🙃", "😉", "😌", "😍", "🥰",
            "😘", "😗", "😙", "😚", "😋", "😛", "😝", "😜",
            "🤪", "🤨", "🧐", "🤓", "😎", "🥳", "😏", "😒",
            "😞", "😔", "😟", "😕", "🙁", "😣", "😖", "😫",
            "😩", "🥺", "😢", "😭", "😤", "😠", "😡", "🤬",
            "🤯", "😳", "🥵", "🥶", "😱", "😨", "😰", "😥",
            "😓", "🤗", "🤔", "🤭", "🤫", "🤥", "😶", "😐",
            "😑", "😬", "🙄", "😯", "😦", "😧", "😮", "😲",
            "🥱", "😴", "🤤", "😪", "🤐", "🥴", "🤢", "🤮",
            "🤧", "😷", "🤒", "🤕",
        ),
    ),
    (
        "手势",
        (
            "👍", "👎", "👌", "✌️", "🤞", "🤟", "🤘", "🤙",
            "👈", "👉", "👆", "👇", "☝️", "✋", "🤚", "🖐️",
            "🖖", "👋", "🤝", "🙏", "💪", "🦾", "✍️", "💅",
            "🤳", "🦵", "🦶", "👂", "👃", "👀", "👁️", "👅",
            "👄",
        ),
    ),
    (
        "动物",
        (
            "🐶", "🐱", "🐭", "🐹", "🐰", "🦊", "🐻", "🐼",
            "🐨", "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🙈",
            "🙉", "🙊", "🐔", "🐧", "🐦", "🐤", "🦆", "🦅",
            "🦉", "🦇", "🐺", "🐗", "🐴", "🦄", "🐝", "🐛",
            "🦋", "🐌", "🐞", "🐜", "🦗", "🕷️", "🦂", "🐢",
            "🐍", "🦎", "🐙", "🦑", "🦀", "🐠", "🐟", "🐬",
            "🐳", "🐋", "🦈",
        ),
    ),
    (
        "食物",
        (
            "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓",
            "🍈", "🍒", "🍑", "🥭", "🍍", "🥥", "🥝", "🍅",
            "🥑", "🥦", "🥕", "🌽", "🌶️", "🥒", "🥬", "🧄",
            "🧅", "🍄", "🥜", "🍞", "🥐", "🥖", "🥨", "🧀",
            "🥚", "🍳", "🥞", "🧇", "🥓", "🍔", "🍟", "🍕",
            "🌭", "🥪", "🌮", "🌯",
        ),
    ),
    (
        "物品",
        (
            "📱", "💻", "⌨️", "🖥️", "🖨️", "🖱️", "💽", "💾",
            "💿", "📀", "📷", "📸", "📹", "🎥", "📞", "☎️",
            "📟", "📠", "📺", "📻", "🎙️", "⏰", "⌚", "⏳",
            "🔋", "🔌", "💡", "🔦", "🕯️", "🧯", "💸", "💵",
            "💰", "💳", "💎", "⚖️", "🔧", "🔨", "⚒️", "🛠️",
        ),
    ),
    (
        "符号",
        (
            "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍",
            "🤎", "💔", "❣️", "💕", "💞", "💓", "💗", "💖",
            "💘", "💝", "💟", "☮️", "✝️", "☪️", "🕉️", "☸️",
            "✡️", "🔯", "🕎", "☯️", "☦️", "🛐", "⛎", "♈",
            "♉", "♊", "♋", "♌", "♍", "♎", "♏", "♐",
            "♑", "♒", "♓",
        ),
    ),
]


def load_recent(store) -> list:
    """Recently used emoji, newest first (bounded by RECENT_CAP)."""
    raw = store.get_setting(RECENT_KEY, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, str)][:RECENT_CAP]


def record_recent(store, emoji: str) -> list:
    """Move [emoji] to the front of the recents list and persist it."""
    recent = load_recent(store)
    if emoji in recent:
        recent.remove(emoji)
    recent.insert(0, emoji)
    recent = recent[:RECENT_CAP]
    store.set_setting(RECENT_KEY, json.dumps(recent, ensure_ascii=False))
    return recent


class EmojiPanel(QWidget):
    emoji_picked = pyqtSignal(str)

    def __init__(self, store, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.store = store
        self._recent = load_recent(store)
        self.setFixedSize(344, 286)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        tabs = QHBoxLayout()
        tabs.setSpacing(2)
        self._buttons = []
        for index, (name, _emojis) in enumerate(EMOJI_CATEGORIES):
            btn = QToolButton()
            btn.setText(name)
            btn.setCheckable(True)
            btn.setAutoExclusive(True)
            btn.setObjectName("ghost")
            btn.clicked.connect(
                lambda checked=False, i=index: self._show_category(i)
            )
            tabs.addWidget(btn)
            self._buttons.append(btn)
        tabs.addStretch()
        outer.addLayout(tabs)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self._grid = QGridLayout(host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(2)
        self._scroll.setWidget(host)
        outer.addWidget(self._scroll, 1)

        self._buttons[0].setChecked(True)
        self._show_category(0)

    def _show_category(self, index: int) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        name, emojis = EMOJI_CATEGORIES[index]
        items = list(self._recent) if name == RECENT_CATEGORY else list(emojis)
        if not items:
            placeholder = QLabel("还没有使用过的表情")
            placeholder.setObjectName("faint")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._grid.addWidget(placeholder, 0, 0, 1, GRID_COLUMNS)
            return
        for i, emoji in enumerate(items):
            btn = QPushButton(emoji)
            btn.setObjectName("ghost")
            btn.setFixedSize(38, 32)
            btn.setToolTip(emoji)
            font = btn.font()
            font.setPointSize(14)
            btn.setFont(font)
            # clicked injects a bool into the first positional arg: keep it
            # from overwriting the emoji default (AGENTS.md PyQt6 trap)
            btn.clicked.connect(lambda checked=False, e=emoji: self._pick(e))
            self._grid.addWidget(btn, i // GRID_COLUMNS, i % GRID_COLUMNS)

    def _pick(self, emoji: str) -> None:
        self._recent = record_recent(self.store, emoji)
        self.emoji_picked.emit(emoji)
        if self._buttons[0].isChecked():
            # keep the 最近 tab current while it is the visible one
            self._show_category(0)
