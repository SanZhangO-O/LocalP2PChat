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


def acquire_named_mutex(data_dir: str):
    """Native named-mutex single-instance gate (second layer under QLockFile).

    Creates (or detects) a Windows kernel mutex named per-user + per-data-dir,
    so the granularity matches QLockFile: two users or two data dirs may run
    their own instances; a second process on the SAME data dir is rejected.
    The kernel releases the mutex automatically when the holding process dies
    (including a hard crash or kill), so a stale lock can never block a
    restart.

    Returns a HANDLE for the process to keep alive, the string "exists" when
    another instance already holds the lock, or None when the mutex could not
    be created (never blocks the app on exotic environments)."""
    if os.name != "nt":
        return None
    import ctypes
    import hashlib
    from ctypes import wintypes

    digest = hashlib.sha1(
        os.path.normcase(os.path.abspath(data_dir)).encode("utf-8")
    ).hexdigest()[:16]
    user = getpass.getuser()
    names = [f"Global\\LocalChat-{user}-{digest}",
             f"Local\\LocalChat-{user}-{digest}"]
    ERROR_ALREADY_EXISTS = 183

    kernel32 = ctypes.windll.kernel32
    CreateMutexW = kernel32.CreateMutexW
    CreateMutexW.restype = wintypes.HANDLE
    CreateMutexW.argtypes = [
        wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
    ]
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    GetLastError = kernel32.GetLastError

    for name in names:
        try:
            handle = CreateMutexW(None, False, name)
            if not handle:
                continue
            if GetLastError() == ERROR_ALREADY_EXISTS:
                CloseHandle(handle)
                return "exists"
            return handle
        except Exception:
            continue
    return None


def _wake_existing_instance() -> None:
    """A second launch on the same data dir: ask the first instance to show
    itself (best-effort), then exit."""
    sock = QLocalSocket()
    sock.connectToServer(_instance_name())
    if sock.waitForConnected(1000):
        sock.disconnectFromServer()
    else:
        QMessageBox.warning(
            None, "LocalChat", "LocalChat 已经在运行中。\n请查看系统托盘图标。"
        )


def release_named_mutex(handle) -> None:
    """Release a handle from [acquire_named_mutex]. No-op on non-Windows /
    None (a failed acquire never blocked the app, so there is nothing to
    release)."""
    if handle is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    try:
        ctypes.windll.kernel32.CloseHandle(
            ctypes.c_void_p(handle)
        )
    except Exception:
        pass


def acquire_single_instance(data_dir: str):
    """Single-instance gate.

    Returns (lock, server, mutex) when this process is the first instance, or
    None when another instance is already running (best-effort: asks it to
    bring its window to the front). A native named mutex is the first check
    (kernel object, released automatically on process death); QLockFile
    remains the authoritative gate that the wake-up QLocalServer is tied to —
    on Windows it is a real OS file lock that the kernel releases
    automatically when the holding process dies, so a crashed instance never
    blocks a restart.
    """
    # Create the mutex exactly once: the handle is the process's hold on the
    # kernel object (kept alive by main()'s frame). Re-acquiring here would
    # both leak the first handle and return "exists" — the second call can
    # never produce a fresh handle for the SAME process's own mutex.
    mutex = acquire_named_mutex(data_dir)
    if mutex == "exists":
        _wake_existing_instance()
        return None
    lock = QLockFile(os.path.join(data_dir, "localchat.lock"))
    if not lock.tryLock(100):
        # a same-data-dir instance started between the mutex check and here:
        # this process exits right away, so give the kernel object back
        release_named_mutex(mutex)
        _wake_existing_instance()
        return None

    # best-effort channel so a second launch can wake this window; on Windows
    # several servers may listen on the same pipe name, but only the process
    # that holds the lock ever exists, so this stays unambiguous.
    server = QLocalServer()
    QLocalServer.removeServer(_instance_name())
    server.listen(_instance_name())
    return lock, server, mutex


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
    lock, single, mutex = guard  # kept alive by main()'s frame for the whole run

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
