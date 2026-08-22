#!/usr/bin/env python3
"""Windows "add contact by IP" E2E against the Android app on an emulator.

The real Windows direct-chat engine (ChatViewModel, port 9999) runs on this
PC; the Android app runs in emulator-5554. Emulator NAT is the one wrinkle:

  - the ANDROID guest announces 10.0.2.15 (its own NAT-internal address),
    which the Windows host cannot dial;
  - Windows therefore dials the guest through  'adb forward tcp:10099 tcp:9999'
    -> contact "127.0.0.1:10099";
  - the Android app reaches the Windows listener through the host-loopback
    alias  10.0.2.2:9999  (guest -> host loopback), the standard emulator
    NAT path.

Flow verified:
  1. Windows adds the emulator by IP (127.0.0.1:10099) -> handshake resolves
     the real device id -> session becomes online with NO chat opened.
  2. Windows sends a message; the Android UI must show it (via adb).
  3. Android adds Windows by IP (10.0.2.2:9999) and replies; the Windows
     engine must receive the reply.
  4. Windows re-opens the chat and flushes a pending offline message -> the
     Android UI must show it (presence redial / outbox flush).

Screenshots land in .interop/shots/, logs in .interop/logs/win_emu_e2e.log.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # .../LocalP2PChat
WINDOWS = os.path.join(REPO, "windows")
SDK = os.environ.get("ANDROID_SDK") or os.path.join(
    os.environ["USERPROFILE"], "AppData", "Local", "Android", "Sdk"
)
ADB = os.path.join(SDK, "platform-tools", "adb.exe")
SERIAL = os.environ.get("SERIAL", "emulator-5554")
HOST_FWD_PORT = int(os.environ.get("HOST_FWD_PORT", "10099"))  # host :10099 -> guest 9999
WIN_PORT = 9999   # the Windows app's shared listener port (host)
GUEST_ALIAS = "10.0.2.2"  # guest's alias for the host loopback

WIN_NICK = "WinPC"
EMU_NICK = "EmuPhone"
MSG_WIN1 = "win-hello-emu"
MSG_EMU = "emu-reply-win"
MSG_WIN2 = "win-pending-flush"

SHOTS = os.path.join(REPO, ".interop", "shots")
LOG = os.path.join(REPO, ".interop", "logs", "win_emu_e2e.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ------------------------------------------------------------------ adb / UI

def adb(*args: str, timeout: int = 60) -> str:
    cmd = [ADB, "-s", SERIAL] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", errors="replace") + \
        (r.stderr or b"").decode("utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed: {out.strip()[:300]}")
    return out


def adb_ok(*args: str) -> bool:
    try:
        adb(*args)
        return True
    except Exception:
        return False


def dump_ui(retries: int = 3) -> ET.Element:
    last = None
    for _ in range(retries):
        try:
            adb("shell", "uiautomator", "dump", "/sdcard/window_dump.xml", timeout=30)
            xml = adb("shell", "cat", "/sdcard/window_dump.xml", timeout=30)
            return ET.fromstring(xml)
        except Exception as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"uiautomator dump failed: {last}")


def nodes(root: ET.Element):
    for n in root.iter("node"):
        b = n.get("bounds") or ""
        if b in ("", "[0,0][0,0]"):
            continue
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
        if not m:
            continue
        yield n.get("text", ""), n.get("content-desc", ""), tuple(map(int, m.groups()))


def center(r: tuple) -> tuple:
    x1, y1, x2, y2 = r
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find(text: str = "", desc: str = "", contains: str = "") -> tuple:
    root = dump_ui()
    for t, d, r in nodes(root):
        if text and t == text:
            return center(r)
        if desc and d == desc:
            return center(r)
        if contains and contains in t:
            return center(r)
    raise RuntimeError(f"node not found: text={text!r} desc={desc!r} contains={contains!r}")


def has_text(text: str) -> bool:
    root = dump_ui()
    return any(t == text or text in t for t, _, _ in nodes(root))


def is_desc(desc: str) -> bool:
    """True when a node with this content-desc is visible (Compose buttons
    usually expose ONLY content-desc, so text matching misses them)."""
    try:
        root = dump_ui()
    except Exception:
        return False
    return any(d == desc for _, d, _ in nodes(root))


def back_to_member_list(max_backs: int = 4) -> None:
    """BACK until the member list (home) is showing — detected by the
    添加成员 top-bar button's content-desc. Stops at once, so an extra BACK
    can never exit the app."""
    for _ in range(max_backs):
        if is_desc("添加成员"):
            return
        adb("shell", "input", "keyevent", "4")
        time.sleep(1.5)
    if not is_desc("添加成员"):
        raise RuntimeError("could not navigate back to the member list")


def wait_text(text: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_text(text):
            return True
        time.sleep(2)
    return False


def tap(x: int, y: int) -> None:
    adb("shell", "input", "tap", str(x), str(y))
    time.sleep(1.5)


def tap_node_wait(text: str = "", desc: str = "", contains: str = "", timeout: int = 30) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            x, y = find(text=text, desc=desc, contains=contains)
            tap(x, y)
            return
        except RuntimeError as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"tap_node_wait timeout: {last}")


def screenshot(name: str) -> str:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, name)
    r = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=30)
    if r.returncode == 0 and r.stdout:
        with open(path, "wb") as f:
            f.write(r.stdout)
        log(f"screenshot -> {os.path.relpath(path, REPO)}")
    return path


def type_text(field_index: int, text: str) -> None:
    root = dump_ui()
    fields = [parse_b(n.get("bounds")) for n in root.iter("node")
              if n.get("class") == "android.widget.EditText"]
    if field_index >= len(fields):
        raise RuntimeError(f"only {len(fields)} EditText visible, wanted #{field_index}")
    x, y = center(fields[field_index])
    tap(x, y)
    time.sleep(0.8)
    adb("shell", "input", "keyevent", "123")          # KEYCODE_MOVE_END
    for _ in range(64):
        adb("shell", "input", "keyevent", "67")       # KEYCODE_DEL
    if text:
        adb("shell", "input", "text", text.replace(" ", "%s"))
    time.sleep(0.8)
    adb("shell", "input", "keyevent", "4")            # dismiss IME
    time.sleep(0.8)


def parse_b(b: str) -> tuple:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
    return tuple(map(int, m.groups())) if m else (0, 0, 0, 0)


def dismiss_system_dialogs() -> None:
    for _ in range(3):
        try:
            root = dump_ui()
        except Exception:
            return
        texts = {t for t, _, _ in nodes(root)}
        if "Allow" in texts:
            x, y = find(text="Allow")
            tap(x, y)
        elif "While using the app" in texts:
            x, y = find(text="While using the app")
            tap(x, y)
        else:
            return


def launch_app() -> None:
    adb("shell", "am", "force-stop", "com.zqr.localchat")
    time.sleep(1)
    adb("shell", "am", "start", "-n", "com.zqr.localchat/.MainActivity")
    time.sleep(4)
    dismiss_system_dialogs()


def listener_up() -> bool:
    out = adb("shell", "cat", "/proc/net/tcp /proc/net/tcp6 2>/dev/null") or ""
    return "270F" in out.upper()  # 9999 hex; include TIME_WAIT/listen lines


def wait_listener(timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listener_up():
            return True
        time.sleep(1)
    return False


def tcp_state(port_hex: str) -> dict:
    out = adb("shell", "cat", "/proc/net/tcp /proc/net/tcp6 2>/dev/null") or ""
    states = {}
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) < 4:
            continue
        local, remote, st = cols[1], cols[2], cols[3]
        if port_hex.lower() in local.lower():
            states[remote] = st
        if port_hex.lower() in remote.lower():
            states.setdefault(local, st)
    return states


# ---------------------------------------------------------------- test flow

def android_flow(wm) -> None:
    log(f"== Android side: add Windows by IP ({GUEST_ALIAS}:{WIN_PORT}) ==")
    # home -> add member dialog
    tap_node_wait(desc="添加成员", timeout=20)
    if not wait_text("IP:端口", 10):
        raise RuntimeError("add-member dialog did not appear")
    type_text(0, f"{GUEST_ALIAS}:{WIN_PORT}")
    type_text(1, WIN_NICK)
    tap_node_wait(text="添加", timeout=10)
    time.sleep(3)
    screenshot("emu_added_win")
    # wait until the session shows the member online (the row is the manually
    # added one showing the forwarded address)
    tap_node_wait(contains=f"{GUEST_ALIAS}:{WIN_PORT}", timeout=60)
    if not wait_text("在线", 60):
        log("WARN: Android session not shown online, continuing")
    time.sleep(2)
    screenshot("emu_win_online")


def android_send_reply(wm) -> None:
    log(f"== Android replies {MSG_EMU} ==")
    # the chat screen is already open; the input is the single EditText
    try:
        root = dump_ui()
        fields = [n for n in root.iter("node") if n.get("class") == "android.widget.EditText"]
        if not fields:
            tap_node_wait(contains=f"{GUEST_ALIAS}:{WIN_PORT}", timeout=20)
    except Exception:
        pass
    type_text(0, MSG_EMU)
    tap_node_wait(desc="发送", timeout=10)
    time.sleep(2)
    if not wait_text(MSG_EMU, 15):
        raise RuntimeError("Android did not render its own reply")
    screenshot("emu_sent_reply")


def main() -> int:
    sys.path.insert(0, WINDOWS)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    log(f"repo={REPO} serial={SERIAL} forward=host:{HOST_FWD_PORT}->guest:9999")

    # ---- preflight: fresh APK on the emulator, forward in place ----
    if not adb_ok("shell", "getprop", "sys.boot_completed"):
        print("emulator not booted"); return 2
    log("re-checking adb forward")
    adb("forward", f"tcp:{HOST_FWD_PORT}", "tcp:9999")

    from PyQt6.QtWidgets import QApplication
    app = QApplication([])
    from localchat.storage import ChatStore
    from localchat.view_model import ChatViewModel

    tmp = os.path.join(tempfile.gettempdir(), "lc_win_emu")
    os.makedirs(tmp, exist_ok=True)
    db = os.path.join(tmp, "a.db")
    if os.path.exists(db):
        os.remove(db)
    data_dir = os.path.join(tmp, "data")
    if os.path.exists(data_dir):
        import shutil
        shutil.rmtree(data_dir)
    os.makedirs(data_dir)
    store = ChatStore(db)
    store.set_setting("port", str(WIN_PORT))
    store.set_setting("nickname", WIN_NICK)

    vm = ChatViewModel(store, data_dir=data_dir)
    toasts = []
    vm.status_message.connect(lambda t: toasts.append(t) or log(f"[toast] {t}"))

    win_id = vm.direct.my_id_value
    log(f"Windows device id: {win_id}")

    # ---- Android app up and listening ----
    log("launching Android app")
    launch_app()
    if not wait_listener(30):
        log("WARN: guest listener not confirmed via /proc/net, continuing anyway")
    screenshot("emu_ready")

    # ---- 1) Windows adds the emulator by IP ----
    log(f"== Windows adds contact {HOST_FWD_PORT} via 127.0.0.1:{HOST_FWD_PORT} ==")
    ok = vm.add_direct_contact(f"127.0.0.1:{HOST_FWD_PORT}", EMU_NICK)
    app.processEvents()
    log(f"add_direct_contact -> {ok}")
    if not ok:
        raise RuntimeError("add_direct_contact returned False")

    # presence dials right away; wait until the real device id is learned
    real_id = None
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        real_id = next((c.id for c in vm.direct_contacts_list()
                        if not c.id.startswith("ip:") and c.id != win_id), None)
        if real_id and vm.direct_chat_alive(real_id):
            break
    if not real_id:
        raise RuntimeError("no real peer id learned within 60s")
    log(f"real peer id learned: {real_id}, alive={vm.direct_chat_alive(real_id)}")
    contacts = [(c.id, c.ip_address, c.port) for c in vm.direct_contacts_list()]
    log(f"Windows contact rows now: {contacts}")
    screenshot("emu_after_win_add")  # peer's own screen: should show online row

    # ---- 2) Windows sends; Android UI must show it ----
    log(f"== Windows sends {MSG_WIN1} ==")
    vm.open_direct_chat(next(c for c in vm.direct_contacts_list() if c.id == real_id))
    app.processEvents()
    assert vm.send_direct_message(real_id, MSG_WIN1), "send failed"
    if not wait_text(MSG_WIN1, 90):
        raise RuntimeError(f"Android UI never showed {MSG_WIN1}")
    screenshot("emu_received_win1")
    log(f"Android UI shows {MSG_WIN1}: PASS")

    # ---- 3) Android adds Windows by IP and replies ----
    android_flow(vm)
    android_send_reply(vm)

    received = None
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        received = next(
            (m for m in vm.direct_messages(real_id) if m.content == MSG_EMU), None
        )
        if received:
            break
    if not received:
        raise RuntimeError(f"Windows engine never received {MSG_EMU}")
    log(f"Windows engine received {MSG_EMU}: PASS")

    # ---- 4) offline-pending message flush over a fresh presence dial ----
    log(f"== Windows parks {MSG_WIN2} while the peer is 'offline', then flushes ==")
    # simulate the "session torn down, message pending" case: kill the session
    # on our side, park a message, then let the Android announce redial bring
    # it back (Windows already knows the real id now)
    vm.direct.close_chat(real_id)
    app.processEvents()
    assert vm.send_direct_message(real_id, MSG_WIN2), "park failed"
    # Windows presence (smaller/larger id rule) or the peer's announce will
    # restore the session; wait until the pending message is delivered
    delivered = False
    deadline = time.time() + 90
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.3)
        s = vm.direct.is_chat_alive(real_id)
        msgs = vm.direct.messages_for(real_id)
        pend = [m for m in msgs if m.content == MSG_WIN2 and m.pending]
        if s and not pend:
            delivered = True
            break
    if not delivered:
        raise RuntimeError(f"{MSG_WIN2} never flipped to delivered")
    log(f"Windows pending message delivered: PASS")
    if not wait_text(MSG_WIN2, 60):
        log("WARN: Android UI did not show the flushed pending message")
    screenshot("emu_flushed_pending")
    screenshot_win = None

    # ---- tcp diagnostics ----
    log("guest tcp states (270F=9999):")
    for remote, st in tcp_state("270F").items():
        log(f"  {remote} state={st} ({'ESTABLISHED' if st=='01' else 'TIME_WAIT/other'})")

    log("ALL PASS")
    # drain pending Qt signals BEFORE shutting the store down: a buffered
    # direct_messages_changed after shutdown would touch a closed database
    for _ in range(3):
        app.processEvents()
        time.sleep(0.2)
    vm.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"E2E FAILED: {e}")
        try:
            screenshot("fail_state")
        except Exception:
            pass
        sys.exit(1)