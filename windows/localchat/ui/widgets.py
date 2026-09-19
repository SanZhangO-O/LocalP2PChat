import datetime
import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QTextEdit

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


class DroppableTextEdit(QTextEdit):
    """Message input that also accepts OS drag-and-drop of local files and
    folders.

    Dropping one or more images/files/folders offers them over the same send
    path as the paperclip button (the media kind is classified by extension on
    send, folders become a grouped folder offer), so a dropped image renders
    inline like a received one. Only URLs that resolve to an existing local
    file or directory are treated as drops: remote links and plain text fall
    through to QTextEdit's default drop handling."""

    def __init__(self, on_send, on_files_dropped=None, parent=None):
        super().__init__(parent)
        self.on_send = on_send
        self.on_files_dropped = on_files_dropped
        self.setAcceptDrops(True)
        self._grow_min = None
        self._grow_max = None

    def enable_auto_grow(self, min_height: int = 40, max_height: int = 120) -> None:
        """Start compact at [min_height] and grow with the document up to
        [max_height], then scroll. A plain QTextEdit claims a ~120px sizeHint,
        which left the composer half empty and bottom-aligned the action
        buttons beside it."""
        self._grow_min = min_height
        self._grow_max = max_height
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.document().contentsChanged.connect(self._apply_auto_grow)
        self._apply_auto_grow()

    def _apply_auto_grow(self) -> None:
        if self._grow_min is None:
            return
        layout = self.document().documentLayout()
        content_h = int(layout.documentSize().height()) if layout is not None else 0
        # padding (8px top + 8px bottom) + the 1px frame on each side
        target = max(self._grow_min, min(self._grow_max, content_h + 18))
        if target != self.height():
            self.setFixedHeight(target)

    def resizeEvent(self, event):
        # a width change re-wraps the document, so the grown height must be
        # recomputed (this also settles the height once the composer gets its
        # real width after the first layout pass)
        super().resizeEvent(event)
        self._apply_auto_grow()

    @staticmethod
    def _local_paths(mime) -> list:
        if not mime.hasUrls():
            return []
        paths = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            raw = url.toLocalFile()
            if not raw:
                continue
            # QUrl always yields forward slashes; normalize to native form so
            # the send path matches what QFileDialog hands over
            path = os.path.normpath(raw)
            if os.path.isfile(path) or os.path.isdir(path):
                paths.append(path)
        return paths

    def _set_drag_active(self, active: bool) -> None:
        if self.property("dragActive") == active:
            return
        self.setProperty("dragActive", active)
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def keyPressEvent(self, event):
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.on_send()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if self._local_paths(event.mimeData()):
            self._set_drag_active(True)
            # force Copy: a Move action would let the source delete the file
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._local_paths(event.mimeData()):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self._set_drag_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        paths = self._local_paths(event.mimeData())
        self._set_drag_active(False)
        if paths and self.on_files_dropped is not None:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            self.on_files_dropped(paths)
            return
        super().dropEvent(event)


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
