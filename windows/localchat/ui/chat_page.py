import os
import sys

from PyQt6.QtCore import QEvent, QPointF, QRect, QRectF, QSize, Qt, QUrl
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QStandardItem,
    QStandardItemModel,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from ..models import (
    MAX_CONTENT_LENGTH,
    MAX_FOLDER_FILES,
    MAX_REPLY_PREVIEW,
    ChatMessage,
    FILE_KIND_IMAGE,
    FILE_KIND_VIDEO,
    MEDIA_KINDS,
    sanitize_file_name,
)
from ..view_model import ChatViewModel
from .theme import (
    BUBBLE_MINE,
    BUBBLE_NAME,
    BUBBLE_OTHER,
    BUBBLE_TEXT_OTHER,
    PRIMARY,
    TEXT_SUBTLE,
    bubble_path,
)
from .widgets import (
    DroppableTextEdit,
    Toast,
    date_header_text,
    format_message_time,
    is_same_day,
)

MSG_ROLE = Qt.ItemDataRole.UserRole + 1
HEADER_ROLE = Qt.ItemDataRole.UserRole + 2

H_PAD = 12
V_PAD = 8
TIME_H = 14
NAME_H = 17
SIDE_MARGIN = 12
FILE_CARD_H = 66
# Inline media image: longest edge cap (logical px) inside the conversation.
MEDIA_IMAGE_MAX = 240
# Quoted-message header strip inside a reply bubble (sender line + preview
# line) and the width of its left accent bar.
REPLY_H = 28
REPLY_ACCENT_W = 3


def _safe_save_name(name: str) -> str:
    # 远端文件名只作建议名：复用 models.sanitize_file_name（FileInfo 入口
    # 已经消毒过，这里是针对旧存量数据的二次防线）
    return sanitize_file_name(name or "")


def format_file_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def file_offer_expired(fi) -> bool:
    """A restored file offer is expired: the download address was captured in
    a previous session and the sender's server died with its process (blanked
    by the ViewModel on restore), so a download can no longer succeed."""
    return fi is None or not fi.download_host or fi.download_port <= 0


class FolderGroup:
    """Synthetic conversation row: one folder card for the file messages that
    share a folderId (a folder offer). Entries are ordered by relative_path and
    include the sender's own offers (rendered as "已发送")."""

    __slots__ = ("folder_id", "folder_name", "entries")

    def __init__(self, folder_id: str, folder_name: str, entries):
        self.folder_id = folder_id
        self.folder_name = folder_name or "文件夹"
        self.entries = entries

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def size(self) -> int:
        return sum(
            e.file_info.file_size for e in self.entries if e.file_info is not None
        )

    @property
    def is_from_me(self) -> bool:
        return bool(self.entries) and all(e.is_from_me for e in self.entries)

    @property
    def expired(self) -> bool:
        return all(file_offer_expired(e.file_info) for e in self.entries)

    @property
    def timestamp(self) -> int:
        return self.entries[0].timestamp if self.entries else 0


class CallLogEntry:
    """Synthetic conversation row: one local call-log line (system style, not
    a chat bubble). Never sent over the wire, never deletable/forwardable and
    never a delete tombstone — it is only displayed and (on click) can redial."""

    __slots__ = (
        "id",
        "peer_id",
        "peer_name",
        "direction",
        "result",
        "media",
        "timestamp",
        "duration",
    )

    def __init__(
        self,
        log_id: str,
        peer_id: str,
        direction: str,
        result: str,
        media: str,
        timestamp: int,
        duration: int = 0,
        peer_name: str = "",
    ):
        self.id = log_id
        self.peer_id = peer_id
        self.peer_name = peer_name
        self.direction = direction
        self.result = result
        self.media = media
        self.timestamp = timestamp
        self.duration = duration

    # duck-type placeholders so generic message code paths cannot crash on a
    # call-log row (it is filtered out of menus/deletes explicitly)
    file_info = None
    is_from_me = False
    pending = False
    content = ""
    sender_name = ""


def call_log_text(entry: CallLogEntry) -> str:
    """Display text of one call-log row ("未接来电", "视频通话 02:31", ...).
    Audio is called out explicitly; video is the implicit default."""
    if entry.result == "answered":
        mm, ss = divmod(max(0, int(entry.duration)), 60)
        hh, mm = divmod(mm, 60)
        dur = f"{hh:d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        base = f"{'语音' if entry.media == 'audio' else '视频'}通话 {dur}"
    elif entry.result == "missed":
        base = "未接来电" if entry.direction == "incoming" else "对方未接听"
    elif entry.result == "rejected":
        base = "已拒绝" if entry.direction == "incoming" else "对方已拒绝"
    elif entry.result == "cancelled":
        base = "已取消"
    else:
        base = "通话未接通"
    if entry.media == "audio" and entry.result != "answered":
        base += "（语音）"
    return base


def call_log_row(log) -> CallLogEntry:
    """Build a conversation row from a persisted SavedCallLog."""
    return CallLogEntry(
        log_id=log.id,
        peer_id=log.peer_id,
        direction=log.direction,
        result=log.result,
        media=log.media,
        timestamp=log.start_time,
        duration=log.duration,
        peer_name=log.peer_name,
    )


def iter_message_rows(messages, call_logs=None):
    """Expand a flat message list into conversation rows:
    ("header", ts) / ("msg", ChatMessage) / ("folder", FolderGroup) /
    ("call", CallLogEntry).
    File messages sharing a folderId collapse into one folder row placed at
    the first entry, so a folder offer renders as a single card (Android
    parity) while old peers still saw the individual files. [call_logs] are
    local call-log rows merged into the flow by timestamp (never part of the
    message protocol)."""
    rows = []
    folders = {}
    for msg in messages:
        fi = msg.file_info
        if fi is not None and fi.folder_id:
            group = folders.get(fi.folder_id)
            if group is None:
                group = FolderGroup(fi.folder_id, fi.folder_name, [msg])
                folders[fi.folder_id] = group
                rows.append(("folder", group))
            else:
                group.entries.append(msg)
            continue
        rows.append(("msg", msg))
    for group in folders.values():
        group.entries.sort(
            key=lambda m: (m.file_info.relative_path or m.file_info.file_name)
        )
    for entry in call_logs or ():
        rows.append(("call", entry))
    rows.sort(key=lambda row: row[1].timestamp if row[1] is not None else 0)
    out = []
    prev_day = None
    for kind, payload in rows:
        ts = payload.timestamp
        if prev_day is None or not is_same_day(prev_day, ts):
            out.append(("header", ts))
            prev_day = ts
        out.append((kind, payload))
    return out


class MessageDelegate(QStyledItemDelegate):
    def __init__(
        self,
        view,
        on_file_click=None,
        file_states=None,
        media_resolver=None,
        on_media_open=None,
        folder_states=None,
        on_folder_click=None,
        on_folder_open=None,
        show_read_state=False,
        on_call_click=None,
        parent=None,
    ):
        super().__init__(parent)
        self._view = view
        self.on_file_click = on_file_click
        self.file_states = file_states if file_states is not None else {}
        # msg -> local path of an already-downloaded image/video copy (None
        # when not downloaded yet); lets the delegate render media inline.
        self.media_resolver = media_resolver
        # path -> open a downloaded media file with the system viewer/player.
        self.on_media_open = on_media_open
        # folderId -> (status, detail) for folder cards; click starts saving
        # the whole tree, a second click on a finished card opens the folder.
        self.folder_states = folder_states if folder_states is not None else {}
        self.on_folder_click = on_folder_click
        self.on_folder_open = on_folder_open
        # Direct chats only: own bubbles carry 已读/未读 (read_receipt). Group
        # chats do not track per-reader receipts, so their page leaves it off.
        self.show_read_state = show_read_state
        # CallLogEntry -> redial with the same media kind (audio/video).
        self.on_call_click = on_call_click
        self._pixmaps: dict = {}

    # ------------------------------------------------------------- media

    def _is_media(self, msg: ChatMessage) -> bool:
        return msg.file_info is not None and msg.file_info.kind in MEDIA_KINDS

    def _media_path(self, msg: ChatMessage):
        if self.media_resolver is None:
            return None
        try:
            return self.media_resolver(msg)
        except Exception:
            return None

    def _load_pixmap(self, path: str):
        pm = self._pixmaps.get(path)
        if pm is None:
            pm = QPixmap(path)
            if pm.isNull():
                return None
            if len(self._pixmaps) > 64:
                self._pixmaps.clear()
            self._pixmaps[path] = pm
        return pm

    def _fit_size(self, width: int, height: int) -> tuple:
        if width <= 0 or height <= 0:
            return 0, 0
        scale = min(1.0, MEDIA_IMAGE_MAX / width, MEDIA_IMAGE_MAX / height)
        return max(1, int(width * scale)), max(1, int(height * scale))

    def _media_image_size(self, msg: ChatMessage, max_bubble_w: int):
        """Displayed (w, h) for a downloaded image message, or None. Shared by
        _layout and _paint_media_image so both honor the same caps (media cap
        AND the bubble width cap) with the aspect ratio preserved."""
        path = self._media_path(msg)
        if not path:
            return None
        pm = self._load_pixmap(path)
        if pm is None:
            return None
        w, h = self._fit_size(pm.width(), pm.height())
        cap = max_bubble_w - 2 * V_PAD
        if w > cap > 0:
            h = max(1, int(h * cap / w))
            w = cap
        return w, h

    def _layout(self, msg: ChatMessage, max_bubble_w: int) -> tuple:
        if isinstance(msg, FolderGroup):
            return min(max_bubble_w, 320), FILE_CARD_H, None
        if msg.file_info is not None:
            if self._is_media(msg):
                size = self._media_image_size(msg, max_bubble_w)
                if size is not None:
                    w, h = size
                    return w + 2 * V_PAD, h + 2 * V_PAD, None
                # image not downloaded yet (or unreadable): placeholder card
            # fixed-size file / media card; width capped so the card never
            # dominates
            return min(max_bubble_w, 320), FILE_CARD_H, None
        font = self._view.font()
        font.setPointSize(10)
        fm = QFontMetrics(font)
        limit_w = max(max_bubble_w - 2 * H_PAD, 60)
        bounding = fm.boundingRect(
            QRect(0, 0, limit_w, 10000),
            Qt.TextFlag.TextWordWrap,
            msg.content,
        )
        bubble_w = min(bounding.width(), limit_w) + 2 * H_PAD
        # the bottom-row label ("待送达 · HH:MM") shares the bubble's inner
        # width with the content: never let the bubble be narrower than the
        # label itself, or a short pending message clips it
        time_font = QFont(font)
        time_font.setPointSize(7)
        label_w = QFontMetrics(time_font).horizontalAdvance(self._time_label(msg))
        bubble_w = max(bubble_w, label_w + 2 * H_PAD)
        height = bounding.height() + 2 * V_PAD + TIME_H + 2
        if not msg.is_from_me:
            height += NAME_H
        if msg.reply_to:
            # quoted header strip above the content
            height += REPLY_H + 2
        return bubble_w, height, bounding

    def sizeHint(self, option, index) -> QSize:
        if index.data(HEADER_ROLE) is not None:
            return QSize(100, 28)
        msg = index.data(MSG_ROLE)
        if msg is None:
            return QSize(100, 40)
        if isinstance(msg, CallLogEntry):
            return QSize(100, 30)
        view_w = self._view.viewport().width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, height, _ = self._layout(msg, max_bubble_w)
        return QSize(max_bubble_w + 2 * SIDE_MARGIN, height + 2 * V_PAD)

    def editorEvent(self, event, model, option, index):
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and event.button() == Qt.MouseButton.LeftButton
        ):
            msg = index.data(MSG_ROLE)
            if isinstance(msg, CallLogEntry):
                # clicking a call-log line redials the same media kind
                if self.on_call_click is not None:
                    self.on_call_click(msg)
                return True
            if isinstance(msg, FolderGroup):
                if msg.is_from_me or msg.expired:
                    return True
                state = self.folder_states.get(msg.folder_id, ("idle", ""))[0]
                if state == "done":
                    if self.on_folder_open:
                        self.on_folder_open(msg)
                elif state != "downloading" and self.on_folder_click:
                    self.on_folder_click(msg)
                return True
            if msg is not None and msg.file_info is not None:
                if self._is_media(msg):
                    # downloaded media: click opens the system viewer/player;
                    # otherwise click downloads into the app media dir and
                    # the bubble flips to the inline image / playable card
                    # (own offers are never re-downloaded: the sender already
                    # has the original — and videos are simply not mirrored)
                    path = self._media_path(msg)
                    if path:
                        if self.on_media_open:
                            self.on_media_open(path)
                        return True
                    if (
                        not msg.is_from_me
                        and not file_offer_expired(msg.file_info)
                        and self.on_file_click
                    ):
                        state = self.file_states.get(msg.id, ("idle", "", ""))[0]
                        if state != "downloading":
                            self.on_file_click(msg)
                    return True
                if self.on_file_click:
                    if file_offer_expired(msg.file_info):
                        return True  # expired offers are not clickable
                    state = self.file_states.get(msg.id, ("idle", "", ""))[0]
                    if state != "downloading":
                        self.on_file_click(msg)
                return True
        return super().editorEvent(event, model, option, index)

    def paint(self, painter: QPainter, option, index) -> None:
        header_text = index.data(HEADER_ROLE)
        if header_text is not None:
            self._paint_header(painter, option, header_text)
            return
        msg = index.data(MSG_ROLE)
        if msg is None:
            return
        if isinstance(msg, CallLogEntry):
            self._paint_call_log(painter, option, msg)
            return
        if isinstance(msg, FolderGroup):
            self._paint_folder_message(painter, option, msg)
            return
        if msg.file_info is not None:
            if self._is_media(msg) and self._media_path(msg):
                pm = self._load_pixmap(self._media_path(msg))
                if pm is not None:
                    self._paint_media_image(painter, option, msg, pm)
                    return
            self._paint_file_message(painter, option, msg)
        else:
            self._paint_message(painter, option, msg)

    def _paint_header(self, painter: QPainter, option, text: str) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#A7A2AF"))
        font = self._view.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(
            option.rect,
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.restore()

    def _paint_call_log(self, painter: QPainter, option, entry: CallLogEntry) -> None:
        """System-style call-log line: centered, no bubble, not selectable."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        missed = entry.result == "missed" and entry.direction == "incoming"
        painter.setPen(QColor("#B3261E") if missed else QColor("#8A8794"))
        font = self._view.font()
        font.setPointSize(9)
        painter.setFont(font)
        text = f"{call_log_text(entry)} {format_message_time(entry.timestamp)}"
        painter.drawText(
            option.rect,
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.restore()

    def _time_label(self, msg: ChatMessage) -> str:
        """Bottom-row label: a "待送达" prefix when the message is a pending
        (offline-queued) direct-chat send, so the user knows it is still
        waiting for the peer to come online (Android parity). In direct chats
        an own delivered message also shows 已读/未读 from the peer's
        read_receipt (group chats never do)."""
        parts = []
        if msg.pending:
            parts.append("待送达")
        elif self.show_read_state and msg.is_from_me:
            parts.append("已读" if msg.read else "未读")
        parts.append(format_message_time(msg.timestamp))
        return " · ".join(parts)

    def _paint_reply_header(self, painter: QPainter, msg: ChatMessage, rect, mine: bool) -> None:
        """Draw the quoted-message header inside a reply bubble: a rounded
        strip with a left accent bar, the original sender's name and an
        elided one-line preview. Both come from the reply wire fields, so the
        quote renders even when the referenced message is not in history."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        strip = QRectF(rect.x(), rect.y(), rect.width(), rect.height() - 2)
        bg = QColor("#FFFFFF" if mine else "#000000")
        bg.setAlpha(30 if mine else 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(strip, 6, 6)
        accent = QColor("#FFFFFF" if mine else PRIMARY)
        accent.setAlpha(210 if mine else 255)
        painter.setBrush(accent)
        painter.drawRect(
            QRectF(strip.x(), strip.y() + 3, REPLY_ACCENT_W, strip.height() - 6)
        )
        text_color = QColor("#FFFFFF" if mine else BUBBLE_TEXT_OTHER)
        text_x = strip.x() + REPLY_ACCENT_W + 6
        text_w = max(strip.width() - REPLY_ACCENT_W - 12, 20)
        sender_font = QFont(self._view.font())
        sender_font.setPointSize(7)
        sender_font.setBold(True)
        painter.setFont(sender_font)
        painter.setPen(text_color)
        sender = (msg.reply_sender or "").strip() or "回复"
        painter.drawText(
            QRectF(text_x, strip.y() + 1, text_w, 12),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(sender_font).elidedText(
                sender, Qt.TextElideMode.ElideRight, int(text_w)
            ),
        )
        preview_font = QFont(self._view.font())
        preview_font.setPointSize(7)
        preview_color = QColor(text_color)
        preview_color.setAlpha(175)
        painter.setFont(preview_font)
        painter.setPen(preview_color)
        preview = (msg.reply_preview or "").replace("\n", " ").strip()
        painter.drawText(
            QRectF(text_x, strip.y() + 12, text_w, 14),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(preview_font).elidedText(
                preview, Qt.TextElideMode.ElideRight, int(text_w)
            ),
        )
        painter.restore()

    def _paint_message(self, painter: QPainter, option, msg: ChatMessage) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        view_w = rect.width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, bubble_h, _ = self._layout(msg, max_bubble_w)
        if msg.is_from_me:
            bubble_x = rect.left() + rect.width() - bubble_w - SIDE_MARGIN
        else:
            bubble_x = rect.left() + SIDE_MARGIN
        bubble_y = rect.top() + V_PAD
        bubble_rect = QRectF(bubble_x, bubble_y, bubble_w, bubble_h)

        path = bubble_path(bubble_rect.toRect(), 14, msg.is_from_me)
        painter.fillPath(path, QColor(BUBBLE_MINE if msg.is_from_me else BUBBLE_OTHER))

        inner = QRectF(bubble_rect.x() + H_PAD, bubble_rect.y() + V_PAD, bubble_rect.width() - 2 * H_PAD, bubble_rect.height() - 2 * V_PAD)
        y = inner.y()
        font = self._view.font()
        font.setPointSize(10)
        painter.setFont(font)
        if not msg.is_from_me:
            painter.setPen(QColor(BUBBLE_NAME))
            name_font = QFont(font)
            name_font.setPointSize(8)
            painter.setFont(name_font)
            painter.drawText(
                QRectF(inner.x(), y, inner.width(), NAME_H),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                msg.sender_name,
            )
            y += NAME_H
            painter.setFont(font)
        if msg.reply_to:
            self._paint_reply_header(
                painter,
                msg,
                QRectF(inner.x(), y, inner.width(), REPLY_H),
                msg.is_from_me,
            )
            y += REPLY_H + 2
            painter.setFont(font)
        painter.setPen(QColor("#FFFFFF" if msg.is_from_me else BUBBLE_TEXT_OTHER))
        content_rect = QRectF(inner.x(), y, inner.width(), inner.height() - (y - inner.y()) - TIME_H - 2)
        painter.drawText(
            content_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
            msg.content,
        )
        time_color = QColor("#FFFFFF" if msg.is_from_me else "#6B6875")
        time_color.setAlpha(200)
        painter.setPen(time_color)
        time_font = QFont(font)
        time_font.setPointSize(7)
        painter.setFont(time_font)
        painter.drawText(
            QRectF(inner.x(), inner.y() + inner.height() - TIME_H, inner.width(), TIME_H),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            self._time_label(msg),
        )
        painter.restore()

    def _paint_file_message(self, painter: QPainter, option, msg: ChatMessage) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        view_w = rect.width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, bubble_h, _ = self._layout(msg, max_bubble_w)
        if msg.is_from_me:
            bubble_x = rect.left() + rect.width() - bubble_w - SIDE_MARGIN
        else:
            bubble_x = rect.left() + SIDE_MARGIN
        bubble_y = rect.top() + V_PAD
        bubble_rect = QRectF(bubble_x, bubble_y, bubble_w, bubble_h)
        path = bubble_path(bubble_rect.toRect(), 14, msg.is_from_me)
        painter.fillPath(path, QColor(BUBBLE_MINE if msg.is_from_me else BUBBLE_OTHER))

        fi = msg.file_info
        state = self.file_states.get(msg.id, ("idle", "", ""))
        expired = file_offer_expired(fi)
        is_media = self._is_media(msg)
        downloaded = self._media_path(msg) is not None
        # throttle-friendly progress text: the ViewModel reports integer
        # percent for a known size (state[2]) or nothing at all
        progress_text = (
            f"下载中 {state[2]}%" if state[0] == "downloading" and state[2] else "下载中..."
        )
        if expired and not downloaded:
            status_text = "已过期"
        else:
            if is_media:
                idle_text = "点击播放" if fi.kind == FILE_KIND_VIDEO else "点击查看"
                status_text = {
                    "idle": "点击播放" if downloaded else idle_text,
                    "downloading": progress_text,
                    "done": "点击播放" if fi.kind == FILE_KIND_VIDEO else "已下载",
                    "failed": state[2] or "下载失败",
                }.get(state[0], idle_text if not downloaded else "点击播放")
            else:
                status_text = {
                    "idle": "点击下载",
                    "downloading": progress_text,
                    "done": "已保存",
                    "failed": state[2] or "下载失败",
                }.get(state[0], "点击下载")
        text_color = QColor("#FFFFFF" if msg.is_from_me else BUBBLE_TEXT_OTHER)
        subtle = QColor("#FFFFFF" if msg.is_from_me else "#6B6875")
        subtle.setAlpha(200 if msg.is_from_me else 255)
        name_font = self._view.font()
        name_font.setPointSize(10)
        fm = QFontMetrics(name_font)
        small_font = QFont(name_font)
        small_font.setPointSize(8)

        # icon (drawn with QPainter so it never depends on emoji font support)
        painter.setPen(text_color)
        icon_rect = QRectF(bubble_rect.x() + 12, bubble_rect.y() + 12, 34, 34)
        if is_media and fi.kind == FILE_KIND_VIDEO:
            self._paint_play_icon(painter, icon_rect, text_color)
        elif is_media:
            self._paint_picture_icon(painter, icon_rect, text_color)
        else:
            self._paint_file_icon(painter, icon_rect, text_color)

        # status on the right, vertically centered (fixed band so it never
        # overlaps the size line)
        status_fm = QFontMetrics(small_font)
        status_rect = QRectF(bubble_rect.right() - 96, bubble_rect.y(), 84, bubble_rect.height())
        painter.setFont(small_font)
        painter.setPen(text_color)
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            status_fm.elidedText(status_text, Qt.TextElideMode.ElideRight, 84),
        )

        # middle column: file name + size
        inner_x = icon_rect.right() + 10
        text_w = status_rect.left() - inner_x - 8
        file_name = fm.elidedText(fi.file_name, Qt.TextElideMode.ElideRight, max(int(text_w), 40))
        painter.setFont(name_font)
        painter.drawText(
            QRectF(inner_x, bubble_rect.y() + 12, text_w, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            file_name,
        )
        painter.setFont(small_font)
        painter.setPen(subtle)
        size_text = format_file_size(fi.file_size)
        painter.drawText(
            QRectF(inner_x, bubble_rect.y() + 32, text_w, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            size_text,
        )
        painter.restore()

    def _paint_folder_message(self, painter: QPainter, option, group: FolderGroup) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        view_w = rect.width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w = min(max_bubble_w, 320)
        bubble_h = FILE_CARD_H
        if group.is_from_me:
            bubble_x = rect.left() + rect.width() - bubble_w - SIDE_MARGIN
        else:
            bubble_x = rect.left() + SIDE_MARGIN
        bubble_y = rect.top() + V_PAD
        bubble_rect = QRectF(bubble_x, bubble_y, bubble_w, bubble_h)
        path = bubble_path(bubble_rect.toRect(), 14, group.is_from_me)
        painter.fillPath(path, QColor(BUBBLE_MINE if group.is_from_me else BUBBLE_OTHER))

        state = self.folder_states.get(group.folder_id, ("idle", ""))
        if group.is_from_me:
            status_text = "已发送"
        elif group.expired:
            status_text = "已过期"
        else:
            status_text = {
                "idle": "点击保存",
                "downloading": state[1] or "保存中...",
                "done": "已保存",
                "failed": state[1] or "保存失败",
                "cancelled": "已取消",
            }.get(state[0], "点击保存")
        text_color = QColor("#FFFFFF" if group.is_from_me else BUBBLE_TEXT_OTHER)
        subtle = QColor("#FFFFFF" if group.is_from_me else TEXT_SUBTLE)
        subtle.setAlpha(200 if group.is_from_me else 255)
        name_font = self._view.font()
        name_font.setPointSize(10)
        fm = QFontMetrics(name_font)
        small_font = QFont(name_font)
        small_font.setPointSize(8)

        painter.setPen(text_color)
        icon_rect = QRectF(bubble_rect.x() + 12, bubble_rect.y() + 12, 34, 34)
        self._paint_folder_icon(painter, icon_rect, text_color)

        status_fm = QFontMetrics(small_font)
        status_rect = QRectF(bubble_rect.right() - 96, bubble_rect.y(), 84, bubble_rect.height())
        painter.setFont(small_font)
        painter.setPen(text_color)
        painter.drawText(
            status_rect,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            status_fm.elidedText(status_text, Qt.TextElideMode.ElideRight, 84),
        )

        inner_x = icon_rect.right() + 10
        text_w = status_rect.left() - inner_x - 8
        name = fm.elidedText(
            group.folder_name, Qt.TextElideMode.ElideRight, max(int(text_w), 40)
        )
        painter.setFont(name_font)
        painter.drawText(
            QRectF(inner_x, bubble_rect.y() + 12, text_w, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            name,
        )
        painter.setFont(small_font)
        painter.setPen(subtle)
        painter.drawText(
            QRectF(inner_x, bubble_rect.y() + 32, text_w, 16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{group.total} 个文件 · {format_file_size(group.size)}",
        )
        painter.restore()

    def _paint_folder_icon(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """Draw a simple folder glyph (back tab + body)."""
        painter.save()
        pen = QPen(color, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        body = QRectF(rect.x() + 4, rect.y() + 13, 27, 18)
        tab = QRectF(rect.x() + 4, rect.y() + 8, 13, 7)
        painter.drawRoundedRect(tab, 2, 2)
        painter.drawRoundedRect(body, 3, 3)
        painter.restore()

    def _paint_file_icon(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """Draw a simple document glyph (body + folded corner + text lines)."""
        painter.save()
        pen = QPen(color, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        body = QRectF(rect.x() + 5, rect.y() + 4, 24, 27)
        painter.drawRoundedRect(body, 3, 3)
        fold = QPainterPath()
        fold.moveTo(body.right() - 7, body.top() + 0.5)
        fold.lineTo(body.right() - 7, body.top() + 7.5)
        fold.lineTo(body.right() - 0.5, body.top() + 7.5)
        painter.drawPath(fold)
        for dy, width in ((13, 14), (18, 14), (23, 9)):
            painter.drawLine(
                QPointF(body.x() + 5, body.y() + dy),
                QPointF(body.x() + 5 + width, body.y() + dy),
            )
        painter.restore()

    def _paint_picture_icon(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """Draw a simple picture glyph (frame + sun + mountains)."""
        painter.save()
        pen = QPen(color, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        frame = QRectF(rect.x() + 4, rect.y() + 6, 27, 23)
        painter.drawRoundedRect(frame, 3, 3)
        sun = QRectF(frame.x() + 4.5, frame.y() + 4, 5, 5)
        painter.drawEllipse(sun)
        mountain = QPainterPath()
        mountain.moveTo(frame.x() + 2.5, frame.bottom() - 2.5)
        mountain.lineTo(frame.x() + 11, frame.top() + 9)
        mountain.lineTo(frame.x() + 16, frame.bottom() - 2.5)
        painter.drawPath(mountain)
        mountain2 = QPainterPath()
        mountain2.moveTo(frame.x() + 13, frame.bottom() - 2.5)
        mountain2.lineTo(frame.x() + 20, frame.top() + 12)
        mountain2.lineTo(frame.right() - 2.5, frame.bottom() - 2.5)
        painter.drawPath(mountain2)
        painter.restore()

    def _paint_play_icon(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """Draw a simple video glyph (rounded frame + play triangle)."""
        painter.save()
        pen = QPen(color, 2)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        frame = QRectF(rect.x() + 3, rect.y() + 7, 29, 21)
        painter.drawRoundedRect(frame, 4, 4)
        painter.setBrush(color)
        triangle = QPainterPath()
        triangle.moveTo(frame.x() + 12, frame.y() + 5)
        triangle.lineTo(frame.x() + 12, frame.bottom() - 5)
        triangle.lineTo(frame.x() + 20, frame.center().y())
        triangle.closeSubpath()
        painter.drawPath(triangle)
        painter.restore()

    def _paint_media_image(self, painter: QPainter, option, msg: ChatMessage, pm: QPixmap) -> None:
        """Inline image message: the downloaded image rendered directly in the
        conversation bubble (rounded corners, aspect ratio preserved)."""
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        view_w = rect.width()
        max_bubble_w = max(int(view_w * 0.72), 200)
        bubble_w, bubble_h, _ = self._layout(msg, max_bubble_w)
        if msg.is_from_me:
            bubble_x = rect.left() + rect.width() - bubble_w - SIDE_MARGIN
        else:
            bubble_x = rect.left() + SIDE_MARGIN
        bubble_rect = QRectF(bubble_x, rect.top() + V_PAD, bubble_w, bubble_h)
        image_rect = QRectF(
            bubble_rect.x() + V_PAD,
            bubble_rect.y() + V_PAD,
            bubble_rect.width() - 2 * V_PAD,
            bubble_rect.height() - 2 * V_PAD,
        )
        scaled = pm.scaled(
            int(image_rect.width()),
            int(image_rect.height()),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        clip = QPainterPath()
        clip.addRoundedRect(image_rect, 12, 12)
        painter.setClipPath(clip)
        painter.drawPixmap(
            QPointF(
                image_rect.x() + (image_rect.width() - scaled.width()) / 2,
                image_rect.y() + (image_rect.height() - scaled.height()) / 2,
            ),
            scaled,
        )
        painter.restore()


class ChatInput(DroppableTextEdit):
    def __init__(self, on_send, on_files_dropped=None, parent=None):
        super().__init__(on_send, on_files_dropped, parent)
        self.setPlaceholderText("输入消息...")
        self.setMaximumHeight(120)
        self.setAcceptRichText(False)


class ChatPage(QWidget):
    def __init__(self, vm: ChatViewModel, on_back):
        super().__init__()
        self.vm = vm
        self.on_back = on_back
        self._stick_to_bottom = True
        self._building = False
        # fileId -> (status, target_path, message) for file messages
        self._file_states: dict = {}
        # folderId -> (status, detail) for folder cards
        self._folder_states: dict = {}
        # The message being replied to (None when not replying).
        self._reply_target = None

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
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("font-size: 16px; font-weight: 700;")
        header_layout.addWidget(self.title_label, 1)
        layout.addWidget(header)

        # Typing indicator line (group chat): "xx 正在输入…" while a member is
        # composing; hidden otherwise.
        self.typing_label = QLabel("")
        self.typing_label.setStyleSheet(
            "font-size: 11px; color: #6B6875; padding: 0 16px 4px 16px;"
        )
        self.typing_label.hide()
        layout.addWidget(self.typing_label)

        self.model = QStandardItemModel(self)
        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(
            MessageDelegate(
                self.list_view,
                on_file_click=self._download_file,
                file_states=self._file_states,
                media_resolver=self._media_path,
                on_media_open=self._open_media,
                folder_states=self._folder_states,
                on_folder_click=self._download_folder,
                on_folder_open=self._open_folder,
                parent=self,
            )
        )
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list_view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_message_menu)
        self.list_view.verticalScrollBar().valueChanged.connect(self._on_scroll)
        layout.addWidget(self.list_view, 1)

        bottom = QFrame()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(14, 8, 14, 12)
        bottom_layout.setSpacing(2)
        # Reply/quote bar: hidden until the user picks 回复 on a message; shows
        # what will be quoted and lets the user cancel it.
        self.reply_bar = QFrame()
        self.reply_bar.setObjectName("replyBar")
        self.reply_bar.setStyleSheet(
            "QFrame#replyBar { background-color: #F2F1F5; border-radius: 6px; }"
        )
        reply_layout = QHBoxLayout(self.reply_bar)
        reply_layout.setContentsMargins(8, 4, 4, 4)
        reply_layout.setSpacing(6)
        self.reply_label = QLabel("")
        self.reply_label.setObjectName("faint")
        self.reply_label.setWordWrap(True)
        reply_layout.addWidget(self.reply_label, 1)
        self.reply_cancel_btn = QPushButton("×")
        self.reply_cancel_btn.setObjectName("ghost")
        self.reply_cancel_btn.setFixedSize(24, 24)
        self.reply_cancel_btn.setToolTip("取消回复")
        # clicked injects a bool into the first lambda parameter: keep it
        # explicit (AGENTS.md PyQt6 trap) so _clear_reply never receives it
        self.reply_cancel_btn.clicked.connect(
            lambda checked=False: self._clear_reply()
        )
        reply_layout.addWidget(self.reply_cancel_btn)
        self.reply_bar.hide()
        bottom_layout.addWidget(self.reply_bar)
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.file_btn = QPushButton()
        if getattr(sys, "_MEIPASS", None):
            icon_path = os.path.join(
                sys._MEIPASS, "localchat", "ui", "assets", "paperclip.svg"
            )
        else:
            icon_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "assets", "paperclip.svg"
            )
        self.file_btn.setIcon(QIcon(icon_path))
        self.file_btn.setIconSize(QSize(20, 20))
        self.file_btn.setObjectName("ghost")
        self.file_btn.setFixedSize(40, 40)
        self.file_btn.setToolTip("发送文件")
        self.file_btn.clicked.connect(self._pick_file)
        input_row.addWidget(self.file_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        self.input_edit = ChatInput(self._send_input, self._send_files)
        input_row.addWidget(self.input_edit, 1)
        self.send_btn = QPushButton("发送")
        self.send_btn.setMinimumSize(80, 40)
        self.send_btn.clicked.connect(self._send_input)
        input_row.addWidget(self.send_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        bottom_layout.addLayout(input_row)
        self.count_label = QLabel("")
        self.count_label.setObjectName("faint")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.count_label.hide()
        bottom_layout.addWidget(self.count_label)
        layout.addWidget(bottom)

        self.input_edit.textChanged.connect(self._on_input_changed)
        self.vm.active_group_changed.connect(self._on_group_changed)
        self.vm.active_messages_changed.connect(self._on_messages_changed)
        self.vm.active_connection_lost_changed.connect(self._on_connection_state_changed)
        self.vm.file_download_finished.connect(self._on_file_download_finished)
        self.vm.media_ready.connect(self._on_media_ready)
        self.vm.file_progress.connect(self._on_file_progress)
        self.vm.folder_progress.connect(self._on_folder_progress)
        self.vm.folder_download_finished.connect(self._on_folder_download_finished)
        self.vm.folder_send_finished.connect(self._on_folder_send_finished)
        self.vm.folder_send_truncated.connect(self._on_folder_send_truncated)
        self.vm.typing_state_changed.connect(self._refresh_typing)

    def _on_group_changed(self):
        self.title_label.setText(self.vm.active_group_name)
        self.model.setRowCount(0)
        self.input_edit.clear()
        self._file_states.clear()
        self._folder_states.clear()
        self._clear_reply()
        self._stick_to_bottom = True
        self._rebuild()
        self._refresh_typing()

    # ------------------------------------------------------------ reply/typing

    def _set_reply_target(self, msg: ChatMessage) -> None:
        """Arm the reply bar for [msg]: the next send quotes it."""
        self._reply_target = msg
        preview = (msg.content or "").replace("\n", " ").strip()
        if msg.file_info is not None and not preview:
            preview = msg.file_info.file_name
        if len(preview) > MAX_REPLY_PREVIEW:
            preview = preview[:MAX_REPLY_PREVIEW] + "…"
        self.reply_label.setText(
            f"回复 {msg.sender_name}：{preview}" if preview else f"回复 {msg.sender_name}"
        )
        self.reply_bar.show()

    def _clear_reply(self) -> None:
        self._reply_target = None
        self.reply_bar.hide()

    def _reply_payload(self):
        """(reply_to, reply_preview, reply_sender) for the armed reply, or
        (None, None, None) when not replying. The preview is capped like the
        wire field (Android parity)."""
        target = self._reply_target
        if target is None:
            return None, None, None
        preview = (target.content or "").replace("\n", " ").strip()
        if len(preview) > MAX_REPLY_PREVIEW:
            preview = preview[:MAX_REPLY_PREVIEW]
        return target.id, preview, target.sender_name

    def _refresh_typing(self):
        gid = self.vm.active_group_id
        names = self.vm.group_typing_names(gid) if gid else []
        if names:
            self.typing_label.setText("、".join(names) + " 正在输入…")
            self.typing_label.show()
        else:
            self.typing_label.hide()

    def _on_messages_changed(self):
        self._rebuild()

    def _rebuild(self):
        self._building = True
        self.model.setRowCount(0)
        msgs = self.vm.active_messages()
        for kind, payload in iter_message_rows(msgs):
            item = QStandardItem()
            if kind == "header":
                item.setData(date_header_text(payload), HEADER_ROLE)
            else:
                item.setData(payload, MSG_ROLE)
            self.model.appendRow(item)
        self._building = False
        if self._stick_to_bottom and msgs:
            self.list_view.scrollToBottom()

    def _on_scroll(self, value):
        if self._building:
            return
        bar = self.list_view.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - 40

    def _send_input(self):
        text = self.input_edit.toPlainText()
        if not text.strip():
            return
        if len(text) > MAX_CONTENT_LENGTH:
            # no silent truncation (Android parity): the Enter key can bypass
            # the disabled button, so reject over-long input here too
            self.count_label.setText(f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
            return
        if self._connection_blocked() or not self.vm.send_message(
            text, *self._reply_payload()
        ):
            self._show_send_blocked()
            return
        self.input_edit.clear()
        self._clear_reply()

    def _connection_blocked(self) -> bool:
        gid = self.vm.active_group_id
        if gid is None:
            return True
        p2p = self.vm.group_p2p_map.get(gid)
        return p2p is None or self.vm.active_connection_lost()

    def _show_send_blocked(self):
        self.count_label.setText("消息未发送：已断开连接")
        self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
        self.count_label.show()

    def _on_connection_state_changed(self):
        self._update_send_ui()

    def _on_input_changed(self):
        self._update_send_ui()
        # composing a non-empty draft refreshes our typing indicator (the
        # ViewModel throttles it); clearing the box must NOT re-arm it after
        # a send — the VM ends typing as the message goes out
        if self.input_edit.toPlainText().strip():
            self.vm.notify_group_typing()

    def _update_send_ui(self):
        length = len(self.input_edit.toPlainText())
        too_long = length > MAX_CONTENT_LENGTH
        blocked = self._connection_blocked()
        self.send_btn.setEnabled(
            bool(self.input_edit.toPlainText().strip()) and not too_long and not blocked
        )
        if blocked:
            self.count_label.setText("已断开连接，无法发送消息")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
        elif too_long:
            self.count_label.setText(f"消息过长（最多 {MAX_CONTENT_LENGTH} 字）")
            self.count_label.setStyleSheet("font-size: 11px; color: #B3261E;")
            self.count_label.show()
        elif length > MAX_CONTENT_LENGTH - 200:
            self.count_label.setText(f"{length}/{MAX_CONTENT_LENGTH}")
            self.count_label.setStyleSheet("font-size: 11px; color: #6B6875;")
            self.count_label.show()
        else:
            self.count_label.hide()

    def _pick_file(self):
        menu = QMenu(self)
        file_action = menu.addAction("发送文件")
        folder_action = menu.addAction("发送文件夹")
        chosen = menu.exec(self.file_btn.mapToGlobal(self.file_btn.rect().bottomLeft()))
        if chosen is file_action:
            path, _ = QFileDialog.getOpenFileName(self.window(), "选择要发送的文件")
            if path:
                self._send_files([path])
        elif chosen is folder_action:
            path = QFileDialog.getExistingDirectory(self.window(), "选择要发送的文件夹")
            if path:
                self._send_files([path])

    def _send_files(self, paths):
        """Offer dropped/picked files and folders to the active group. Every
        path is tried so one unavailable entry never swallows the rest; a
        folder is offered as one file_message per contained file. Folder
        offers are built on a worker thread (folder_send_finished reports the
        outcome) so a large folder never freezes the UI; plain files stay
        synchronous so their failure toast shows right away."""
        failed = False
        for path in paths:
            if os.path.isdir(path):
                self.vm.send_folder(path)
            elif not self.vm.send_file(path):
                failed = True
        if failed:
            Toast(self.window()).show_message("无法发送文件：未连接或文件不可用")

    def _on_folder_send_finished(self, ok: bool):
        if not ok:
            Toast(self.window()).show_message("无法发送文件夹：未连接或文件夹不可用")

    def _on_folder_send_truncated(self, folder_name: str):
        Toast(self.window()).show_message(
            f"文件夹超过 {MAX_FOLDER_FILES} 个文件，仅发送前 {MAX_FOLDER_FILES} 个"
        )

    # ------------------------------------------------------- folder saving

    def _download_folder(self, group: FolderGroup):
        if group.is_from_me or group.expired:
            return
        dest = QFileDialog.getExistingDirectory(self.window(), "选择保存文件夹位置")
        if not dest:
            return
        self._folder_states[group.folder_id] = ("downloading", f"0/{group.total}")
        self.list_view.viewport().update()
        self.vm.download_folder(group.folder_id, dest)

    def _open_folder(self, group: FolderGroup):
        state = self._folder_states.get(group.folder_id, ("", ""))
        path = state[1] if state[0] == "done" else ""
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _on_folder_progress(self, folder_id: str, done: int, total: int):
        if folder_id not in self._folder_states:
            return
        self._folder_states[folder_id] = ("downloading", f"{done}/{total}")
        self.list_view.viewport().update()

    def _on_folder_download_finished(self, folder_id: str, ok: bool, message: str):
        if folder_id not in self._folder_states:
            return
        if ok:
            self._folder_states[folder_id] = ("done", message)
            Toast(self.window()).show_message("文件夹已保存")
        elif "取消" in message:
            self._folder_states[folder_id] = ("cancelled", message)
        else:
            self._folder_states[folder_id] = ("failed", message)
            Toast(self.window()).show_message(f"文件夹保存失败：{message}")
        self.list_view.viewport().update()
        self.list_view.doItemsLayout()

    def _media_path(self, msg):
        fi = msg.file_info
        if fi is None:
            return None
        return self.vm.downloaded_media_path(fi.file_id, fi.file_name)

    def _open_media(self, path: str):
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
            Toast(self.window()).show_message("无法打开文件")

    def _download_file(self, msg):
        fi = msg.file_info
        if fi is None:
            return
        if fi.kind in MEDIA_KINDS:
            # media: NO save dialog — download into the app media dir so the
            # message renders inline; "另存为" in the context menu saves a copy
            target = self.vm.media_target_path(fi.file_id, fi.file_name)
            if os.path.isfile(target):
                self._file_states[msg.id] = ("done", target, "")
                self._open_media(target)
                return
            self._file_states[msg.id] = ("downloading", target, "")
            self.list_view.viewport().update()
            self.vm.download_file(msg.id, target)
            return
        self._save_file_as(msg)

    def _save_file_as(self, msg):
        fi = msg.file_info
        if fi is None:
            return
        downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        os.makedirs(downloads, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self.window(), "保存文件", os.path.join(downloads, _safe_save_name(fi.file_name))
        )
        if not path:
            return
        self._file_states[msg.id] = ("downloading", path, "0")
        self.list_view.viewport().update()
        self.vm.download_file(msg.id, path)

    def _on_file_progress(self, file_id: str, received: int, total: int):
        state = self._file_states.get(file_id)
        if state is None or state[0] != "downloading":
            return
        if total > 0:
            percent = min(99, int(received * 100 / total))
            self._file_states[file_id] = ("downloading", state[1], str(percent))
            self.list_view.viewport().update()

    def _cancel_download(self, msg):
        gid = self.vm.active_group_id
        if gid is None:
            return
        self.vm.cancel_download(gid, msg.id)
        state = self._file_states.get(msg.id)
        if state is not None and state[0] == "downloading":
            self._file_states[msg.id] = ("cancelled", "", "")
            self.list_view.viewport().update()

    def _on_file_download_finished(self, file_id: str, ok: bool, message: str):
        if ok:
            path = self._file_states.get(file_id, ("", "", ""))[1]
            self._file_states[file_id] = ("done", path, "")
            Toast(self.window()).show_message("文件已保存")
        elif "取消" in message:
            self._file_states[file_id] = ("cancelled", "", "")
        else:
            self._file_states[file_id] = ("failed", "", message)
            Toast(self.window()).show_message(f"下载失败：{message}")
        self.list_view.viewport().update()
        # a downloaded image swaps its placeholder card for a much taller
        # inline bubble: repaint alone keeps the stale row height
        self.list_view.doItemsLayout()

    def _on_media_ready(self):
        """An own sent image landed in the media dir: re-render so the
        sender's own bubble flips to the inline image."""
        self.list_view.viewport().update()
        self.list_view.doItemsLayout()

    def _show_message_menu(self, pos):
        index = self.list_view.indexAt(pos)
        msg = index.data(MSG_ROLE) if index.isValid() else None
        if msg is None:
            return
        if isinstance(msg, FolderGroup):
            self._show_folder_menu(msg, pos)
            return
        menu = QMenu(self.list_view)
        if msg.file_info is not None:
            is_media = msg.file_info.kind in MEDIA_KINDS
            open_action = None
            if is_media:
                local = self._media_path(msg)
                if local:
                    open_action = menu.addAction("打开")
            download_action = None
            cancel_action = None
            dl_state = self._file_states.get(msg.id, ("idle", "", ""))[0]
            if not file_offer_expired(msg.file_info):
                if dl_state == "downloading":
                    # an in-flight download offers 取消下载 instead of starting
                    # a second one
                    cancel_action = menu.addAction("取消下载")
                else:
                    download_action = menu.addAction("另存为..." if is_media else "下载 / 另存为")
            copy_name_action = menu.addAction("复制文件名")
            delete_action = None
            if msg.is_from_me:
                menu.addSeparator()
                delete_action = menu.addAction("删除")
            chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
            if open_action is not None and chosen is open_action:
                self._open_media(self._media_path(msg))
            elif download_action is not None and chosen is download_action:
                self._save_file_as(msg)
            elif cancel_action is not None and chosen is cancel_action:
                self._cancel_download(msg)
            elif chosen is copy_name_action:
                QApplication.clipboard().setText(msg.file_info.file_name)
            elif chosen is delete_action:
                self._confirm_delete(msg.id)
            return
        reply_action = menu.addAction("回复")
        copy_action = menu.addAction("复制")
        forward_action = menu.addAction("转发")
        delete_action = None
        if msg.is_from_me:
            menu.addSeparator()
            delete_action = menu.addAction("删除")
        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen is reply_action:
            self._set_reply_target(msg)
        elif chosen is copy_action:
            QApplication.clipboard().setText(msg.content)
        elif chosen is forward_action:
            self._show_forward_dialog(msg.content)
        elif chosen is delete_action:
            self._confirm_delete(msg.id)

    def _show_folder_menu(self, group: FolderGroup, pos):
        menu = QMenu(self.list_view)
        state = self._folder_states.get(group.folder_id, ("idle", ""))[0]
        save_action = None
        cancel_action = None
        if not group.is_from_me and not group.expired:
            if state == "downloading":
                cancel_action = menu.addAction("取消下载")
            else:
                save_action = menu.addAction("保存文件夹...")
        copy_action = menu.addAction("复制文件夹名")
        delete_action = None
        if group.is_from_me:
            menu.addSeparator()
            delete_action = menu.addAction("删除")
        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen is save_action:
            self._download_folder(group)
        elif chosen is cancel_action:
            self._cancel_folder(group)
        elif chosen is copy_action:
            QApplication.clipboard().setText(group.folder_name)
        elif chosen is delete_action:
            self._confirm_delete_folder(group)

    def _cancel_folder(self, group: FolderGroup):
        gid = self.vm.active_group_id
        if gid is None:
            return
        self.vm.cancel_download(gid, group.folder_id)
        self._folder_states[group.folder_id] = ("cancelled", "")
        self.list_view.viewport().update()

    def _confirm_delete_folder(self, group: FolderGroup):
        box = QMessageBox(self.window())
        box.setWindowTitle("删除文件夹")
        box.setText("删除后，这个文件夹的所有消息会从群内聊天记录中移除，且无法恢复。")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_btn:
            for entry in list(group.entries):
                self.vm.delete_message(entry.id)

    def _show_forward_dialog(self, content: str):
        gid = self.vm.active_group_id
        targets = [g for g in self.vm.groups_list() if g.group_id != gid and g.connected]
        dialog = QDialog(self.window())
        dialog.setObjectName("confirmDialog")
        dialog.setWindowTitle("转发消息")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(20, 16, 20, 12)
        preview = content if len(content) <= 20 else content[:20] + "..."
        preview_label = QLabel(f'"{preview}"')
        preview_label.setObjectName("hint")
        preview_label.setWordWrap(True)
        layout.addWidget(preview_label)
        layout.addSpacing(8)
        if not targets:
            none_label = QLabel("没有其他可转发的群组（未连接的群组无法转发）")
            none_label.setObjectName("hint")
            none_label.setWordWrap(True)
            layout.addWidget(none_label)
        else:
            for group in targets:
                btn = QPushButton(group.group_name)
                btn.setObjectName("outline")
                btn.setStyleSheet("text-align: left;")
                btn.clicked.connect(
                    lambda checked=False, gid_=group.group_id: self._forward(gid_, content, dialog)
                )
                layout.addWidget(btn)
        layout.addSpacing(8)
        cancel_btn = QPushButton("取消")
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(dialog.accept)
        layout.addWidget(cancel_btn, alignment=Qt.AlignmentFlag.AlignRight)
        dialog.exec()

    def _forward(self, target_group_id: str, content: str, dialog: QDialog):
        dialog.accept()
        if not self.vm.send_message_to_group(target_group_id, content):
            Toast(self.window()).show_message("消息未发送：已断开连接")

    def _confirm_delete(self, message_id: str):
        box = QMessageBox(self.window())
        box.setWindowTitle("删除消息")
        box.setText("删除后，这条消息会从群内所有成员的聊天记录中移除，且无法恢复。")
        delete_btn = box.addButton("删除", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is delete_btn:
            self.vm.delete_message(message_id)
