from PyQt6.QtCore import QEvent, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMainWindow, QMenu, QStackedWidget, QSystemTrayIcon

from ..view_model import ChatViewModel
from .call_window import CallWindow, IncomingCallDialog
from .chat_page import ChatPage
from .direct_chat_page import DirectChatPage
from .group_list_page import GroupListPage
from .group_lobby_page import GroupLobbyPage
from .member_list_page import MemberListPage
from .setup_page import SetupPage
from .settings_page import SettingsPage
from .theme import ON_PRIMARY, PRIMARY
from .widgets import Toast

PAGE_GROUPS = 0
PAGE_SETUP = 1
PAGE_LOBBY = 2
PAGE_CHAT = 3
PAGE_MEMBERS = 4
PAGE_DIRECT = 5
PAGE_SETTINGS = 6


def app_icon_pixmap(size: int = 64) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(PRIMARY))
    p.setPen(Qt.PenStyle.NoPen)
    rect = pm.rect().adjusted(2, 2, -2, -2)
    p.drawRoundedRect(rect, rect.height() * 0.25, rect.height() * 0.25)
    p.setPen(QColor(ON_PRIMARY))
    font = p.font()
    font.setPixelSize(int(size * 0.5))
    font.setBold(True)
    p.setFont(font)
    p.drawText(rect, Qt.AlignmentFlag.AlignCenter, "聊")
    p.end()
    return pm


class MainWindow(QMainWindow):
    def __init__(self, vm: ChatViewModel):
        super().__init__()
        self.vm = vm
        self.setWindowTitle("LocalChat - 局域网群组聊天")
        self.setWindowIcon(QIcon(app_icon_pixmap(64)))
        self.resize(920, 680)
        self.setMinimumSize(720, 520)

        self.stack = QStackedWidget(self)
        self.setCentralWidget(self.stack)

        self.pages = {}
        self.pages[PAGE_GROUPS] = GroupListPage(vm, self._go_setup, on_open_members=self._go_members)
        self.pages[PAGE_GROUPS].on_open_group = self._go_lobby
        self.pages[PAGE_GROUPS].on_open_settings = self._go_settings
        self.pages[PAGE_SETUP] = SetupPage(vm, self._go_groups, self._on_group_entered)
        self.pages[PAGE_LOBBY] = GroupLobbyPage(
            vm,
            on_back=self._go_groups,
            on_open_chat=self._go_chat,
            on_leave=self._on_left_group,
        )
        self.pages[PAGE_CHAT] = ChatPage(vm, self._go_lobby)
        self.pages[PAGE_MEMBERS] = MemberListPage(
            vm,
            on_open_groups=self._go_groups,
            on_open_settings=self._go_settings,
            on_open_chat=self._go_direct,
        )
        self.pages[PAGE_DIRECT] = DirectChatPage(vm, self._go_members)
        self.pages[PAGE_SETTINGS] = SettingsPage(vm, self._back_from_settings)
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.toast = Toast(self)
        vm.status_message.connect(self.toast.show_message)
        vm.join_successful.connect(self._go_lobby)
        vm.create_failed.connect(self._go_setup)

        self._really_quit = False
        self._tray_hint_shown = False
        self._last_notify_gid = None
        self._incoming_dialog = None
        self._call_window = None

        self._setup_tray()
        self._setup_call_ui()
        # member-first: the home page is the member list
        self._go_members()

        QTimer.singleShot(300, self._check_host_hint)

    def _setup_call_ui(self):
        cm = self.vm.call_manager
        cm.incoming_call.connect(self._on_incoming_call)
        cm.state_changed.connect(self._on_call_state)
        cm.call_ended.connect(lambda reason: self.toast.show_message(f"通话结束：{reason}"))
        cm.call_error.connect(self.toast.show_message)

    def _on_incoming_call(self, call_id: str, caller_name: str):
        if self._incoming_dialog is not None:
            return
        dialog = IncomingCallDialog(
            caller_name,
            on_accept=self.vm.accept_call,
            on_reject=self.vm.reject_call,
            parent=self,
        )
        self._incoming_dialog = dialog
        dialog.finished.connect(lambda _: setattr(self, "_incoming_dialog", None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_call_state(self, state: str, peer_name: str, detail: str):
        if state in ("outgoing", "active"):
            if self._call_window is None:
                win = CallWindow(self.vm.call_manager, self)
                self._call_window = win
                win.finished.connect(lambda _: setattr(self, "_call_window", None))
                win.show()
        elif state == "idle":
            if self._incoming_dialog is not None:
                self._incoming_dialog.close()
            if self._call_window is not None:
                self._call_window.close()

    def _setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(QIcon(app_icon_pixmap(32)), self)
        self.tray.setToolTip("LocalChat")
        menu = QMenu(self)
        show_action = QAction("显示/隐藏窗口", self)
        show_action.triggered.connect(self._toggle_window)
        menu.addAction(show_action)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.messageClicked.connect(self._on_message_clicked)
        self.vm.tray_notification.connect(self._show_tray_notification)
        self.tray.show()

    def _quit(self):
        self._really_quit = True
        self.close()

    def request_quit(self):
        """Real exit (used by the Ctrl+C handler): bypasses the close-to-tray
        behavior and shuts the app down."""
        self._really_quit = True
        self.close()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._toggle_window()

    def _toggle_window(self):
        if self.isVisible():
            self.hide()
        else:
            self._show_window()

    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _show_tray_notification(self, gid: str, title: str, body: str):
        self._last_notify_gid = gid
        if self.tray is not None and self.tray.isVisible():
            self.tray.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, 3000)

    def _on_message_clicked(self):
        """Clicking the tray bubble brings the window back and opens the
        group the notification came from."""
        self._show_window()
        gid = self._last_notify_gid
        if gid:
            self.vm.switch_to_group(gid)
            self._go_lobby()

    def _check_host_hint(self):
        if not self.vm.can_create_group():
            return
        ip = self.vm.local_ip
        if ip:
            self.toast.show_message(f"本机地址 {ip}:{self.vm.local_port}", 4000)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.ActivationChange:
            self.vm.set_window_active(self.isActiveWindow())
        elif event.type() == QEvent.Type.WindowStateChange:
            # minimize/restore: minimized windows are not "active" for
            # notification purposes even if focus tracking lags behind
            self.vm.set_window_active(self.isActiveWindow() and not self.isMinimized())
        super().changeEvent(event)

    def hideEvent(self, event):
        self.vm.set_window_active(False)
        super().hideEvent(event)

    def showEvent(self, event):
        self.vm.set_window_active(self.isActiveWindow())
        super().showEvent(event)

    def _go_groups(self):
        self.stack.setCurrentIndex(PAGE_GROUPS)

    def _go_setup(self):
        self.pages[PAGE_SETUP].refresh()
        self.stack.setCurrentIndex(PAGE_SETUP)

    def _go_lobby(self):
        self.stack.setCurrentIndex(PAGE_LOBBY)
        self.pages[PAGE_LOBBY].refresh()

    def _go_chat(self):
        gid = self.vm.active_group_id
        if gid:
            self.vm.clear_unread(gid)
        self.stack.setCurrentIndex(PAGE_CHAT)
        self.pages[PAGE_CHAT]._on_group_changed()

    def _go_members(self):
        self.pages[PAGE_MEMBERS].refresh()
        self.stack.setCurrentIndex(PAGE_MEMBERS)

    def _go_direct(self, contact):
        self.pages[PAGE_DIRECT].open_chat(contact)
        self.stack.setCurrentIndex(PAGE_DIRECT)

    def _go_settings(self):
        self._settings_from = self.stack.currentIndex()
        self.pages[PAGE_SETTINGS].refresh()
        self.stack.setCurrentIndex(PAGE_SETTINGS)

    def _back_from_settings(self):
        # return to wherever settings was opened from (member home or groups)
        self.stack.setCurrentIndex(getattr(self, "_settings_from", PAGE_MEMBERS))

    def _on_group_entered(self):
        self._go_lobby()

    def _on_left_group(self):
        self.vm.leave_active_group()
        self._go_groups()

    def closeEvent(self, event):
        if self.tray is None or self._really_quit:
            self.vm.shutdown()
            if self.tray is not None:
                # drop the tray icon immediately so no ghost icon lingers
                self.tray.hide()
            # A window hidden in the tray (or one whose close was requested
            # programmatically while hidden) never emits lastWindowClosed,
            # so quitOnLastWindowClosed would leave app.exec() running with
            # a destroyed window — the process lingers invisibly. End the
            # event loop explicitly.
            QApplication.quit()
            event.accept()
            return
        # close-to-tray: hide instead of quitting so notifications keep
        # arriving; the tray menu's 退出 action sets _really_quit first
        event.ignore()
        self.hide()
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.showMessage(
                "LocalChat",
                "已最小化到系统托盘，点击托盘图标或通知可恢复窗口",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )
