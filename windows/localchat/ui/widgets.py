import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel

from .theme import avatar_color


def avatar_char(name: str) -> str:
    if not name:
        return "?"
    return name[0].upper()


def format_group_time(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return ""
    now = datetime.datetime.now()
    t = datetime.datetime.fromtimestamp(timestamp_ms / 1000)
    diff = (now - t).total_seconds()
    if diff < 24 * 3600:
        return t.strftime("%H:%M")
    if diff < 7 * 24 * 3600:
        return t.strftime("%m/%d")
    return t.strftime("%y/%m/%d")


def is_same_day(a_ms: int, b_ms: int) -> bool:
    a = datetime.datetime.fromtimestamp(a_ms / 1000)
    b = datetime.datetime.fromtimestamp(b_ms / 1000)
    return (a.year, a.month, a.day) == (b.year, b.month, b.day)


def date_header_text(timestamp_ms: int) -> str:
    t = datetime.datetime.fromtimestamp(timestamp_ms / 1000)
    now = datetime.datetime.now()
    today = datetime.datetime(now.year, now.month, now.day)
    day = datetime.datetime(t.year, t.month, t.day)
    diff = (today - day).days
    if diff == 0:
        return "今天"
    if diff == 1:
        return "昨天"
    if t.year == now.year:
        return f"{t.month}月{t.day}日"
    return f"{t.year}年{t.month}月{t.day}日"


def format_message_time(timestamp_ms: int) -> str:
    return datetime.datetime.fromtimestamp(timestamp_ms / 1000).strftime("%H:%M")


class AvatarLabel(QLabel):
    def __init__(self, name: str, size: int, font_size: int = 11, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        color = avatar_color(name)
        self.setStyleSheet(
            f"background: {color.name()}; border-radius: {size // 2}px;"
            f"color: #1C1B1F; font-weight: 600; font-size: {font_size}px;"
        )
        self.setText(avatar_char(name))


class Toast(QLabel):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "background: rgba(28, 27, 31, 0.92); color: white; border-radius: 18px;"
            "padding: 9px 22px; font-size: 13px;"
        )
        self.hide()
        self._timer = None

    def show_message(self, text: str, duration_ms: int = 2600):
        from PyQt6.QtCore import QTimer

        self.setText(text)
        self.adjustSize()
        parent = self.parentWidget()
        if parent is not None:
            x = (parent.width() - self.width()) // 2
            y = parent.height() - self.height() - 48
            self.move(max(x, 8), max(y, 8))
        self.raise_()
        self.show()
        if self._timer is not None:
            self._timer.stop()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._timer.start(duration_ms)
