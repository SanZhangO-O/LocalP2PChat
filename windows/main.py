import getpass
import os
import sys

from PyQt6.QtCore import QLockFile, QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QMessageBox

from PyQt6 import QtSvg  # noqa: F401  (registers the SVG image-format plugin; needed by the frozen build)

from localchat.storage import ChatStore
from localchat.ui.main_window import MainWindow
from localchat.ui.theme import APP_QSS
from localchat.view_model import ChatViewModel


def base_dir() -> str:
    """Directory that holds the app's mutable data. Portable by default (next
    to the exe / source file); falls back to %LOCALAPPDATA%\\LocalChat when the
    exe directory is not writable (e.g. installed under Program Files)."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        if os.access(exe_dir, os.W_OK):
            return exe_dir
        local = os.environ.get("LOCALAPPDATA")
        if local:
            path = os.path.join(local, "LocalChat")
            try:
                os.makedirs(path, exist_ok=True)
                return path
            except OSError:
                pass
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))


def _instance_name() -> str:
    # Windows named pipes are per-user; include the user name so two users on
    # one machine can each run their own instance.
    return "LocalChat-" + getpass.getuser()


def acquire_single_instance(data_dir: str):
    """Single-instance gate.

    Returns (lock, server) when this process is the first instance, or None
    when another instance is already running (best-effort: asks it to bring
    its window to the front). QLockFile is the authoritative gate — on Windows
    it is a real OS file lock that the kernel releases automatically when the
    holding process dies, so a crashed instance never blocks a restart.
    """
    lock = QLockFile(os.path.join(data_dir, "localchat.lock"))
    if not lock.tryLock(100):
        # already running: ask the first instance to show itself, then exit
        sock = QLocalSocket()
        sock.connectToServer(_instance_name())
        if sock.waitForConnected(1000):
            sock.disconnectFromServer()
        else:
            QMessageBox.warning(
                None, "LocalChat", "LocalChat 已经在运行中。\n请查看系统托盘图标。"
            )
        return None

    # best-effort channel so a second launch can wake this window; on Windows
    # several servers may listen on the same pipe name, but only the process
    # that holds the lock ever exists, so this stays unambiguous.
    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())
    server.listen(_instance_name())
    return lock, server


def _install_ctrl_c_handler(app, window) -> None:
    """Make Ctrl+C in the console quit the app cleanly.

    Python's own SIGINT handling cannot run while the main thread is blocked
    inside the Qt event loop (C++), so a plain signal.signal handler never
    fires and Ctrl+C appears to do nothing. On Windows we install a console
    control handler (SetConsoleCtrlHandler) that sets a flag on the Ctrl+C
    event; a QTimer polling inside the Qt loop then performs a real quit
    (bypassing the close-to-tray behavior).
    """
    pressed = {"flag": False}

    def check_flag():
        if pressed["flag"]:
            pressed["flag"] = False
            window.request_quit()

    # parented to the app so the timer outlives this function
    timer = QTimer(app)
    timer.timeout.connect(check_flag)
    timer.start(100)

    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        CTRL_C_EVENT = 0

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        def _console_handler(ctrl_type: int) -> int:
            if ctrl_type == CTRL_C_EVENT:
                pressed["flag"] = True
                return True  # handled; suppress the default SIGINT behavior
            return False

        try:
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
            # Keep a strong reference for the whole process lifetime:
            # SetConsoleCtrlHandler still holds the function pointer, and if
            # the Python callback object is garbage-collected the OS would
            # call a freed pointer (access violation) during shutdown.
            app._localchat_ctrl_c_handler = _console_handler
        except Exception:
            pass
    else:
        import signal

        signal.signal(signal.SIGINT, lambda *_: pressed.__setitem__("flag", True))


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("LocalChat")
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(APP_QSS)

    data_dir = os.path.join(base_dir(), "data")
    os.makedirs(data_dir, exist_ok=True)

    guard = acquire_single_instance(data_dir)
    if guard is None:
        return 0
    lock, single = guard  # kept alive by main()'s frame for the whole run

    store = ChatStore(os.path.join(data_dir, "localchat.db"))

    vm = ChatViewModel(store, data_dir=data_dir)
    window = MainWindow(vm)
    _install_ctrl_c_handler(app, window)
    window.show()
    # a second launch asks us to come to the foreground (works from tray too)
    single.newConnection.connect(window._show_window)
    app.aboutToQuit.connect(vm.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
