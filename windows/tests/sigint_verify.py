import ctypes
import os
import subprocess
import sys
import tempfile
import time

from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHILD = r"""
import os, sys, tempfile, faulthandler
sys.path.insert(0, r"@ROOT@")
faulthandler.enable()
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import main as m
m.base_dir = lambda: tempfile.mkdtemp(prefix="lc_sigint_")
print("APP_READY", flush=True)
rc = m.main()
print("APP_EXITED rc=%s" % rc, flush=True)
sys.exit(0)
""".replace("@ROOT@", ROOT)

CREATE_NEW_CONSOLE = 0x00000010
CTRL_C_EVENT = 0


def send_ctrl_c(child_pid: int) -> bool:
    ctypes.windll.kernel32.FreeConsole()
    if not ctypes.windll.kernel32.AttachConsole(child_pid):
        print("  attach failed, err=%d" % ctypes.windll.kernel32.GetLastError())
        return False
    ok = ctypes.windll.kernel32.GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0)
    ctypes.windll.kernel32.FreeConsole()
    print("  GenerateConsoleCtrlEvent ->", bool(ok))
    return bool(ok)


def main() -> int:
    child = subprocess.Popen(
        [sys.executable, "-X", "faulthandler", "-c", CHILD],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NEW_CONSOLE,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        line = child.stdout.readline()
        if not line:
            break
        sys.stdout.write(line)
        sys.stdout.flush()
        if "APP_READY" in line:
            break
    time.sleep(2.0)
    send_ctrl_c(child.pid)
    try:
        out, _ = child.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        print("RESULT: app STILL RUNNING after Ctrl+C")
        child.kill()
        child.wait(timeout=5)
        return 1
    print(out)
    print("RESULT: rc=%d" % child.returncode)
    return 0 if child.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
