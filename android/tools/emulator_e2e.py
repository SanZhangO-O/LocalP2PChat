#!/usr/bin/env python3
"""LocalChat two-emulator E2E driver (adb + 2 AVDs).

Boots two Android emulators, installs the debug APK on both, then drives the
app over adb (uiautomator dump + input tap/text) through the main flows:

  1. create a group on device A (host)
  2. join it from device B through an adb-forwarded address
  3. group chat in both directions (host-relay path)
  4. direct member chat (B pulls up A through a forward; A replies through
     a second forward on B)
  5. cleanup: clear app data / uninstall (--cleanup)

Usage (run from the repo root):

    python tools/emulator_e2e.py --boot            # boot both emulators (skip if already up)
    python tools/emulator_e2e.py --install         # install APK + grant permissions
    python tools/emulator_e2e.py --test            # run the E2E chat flows
    python tools/emulator_e2e.py --cleanup         # clear app data on both (uninstall with --uninstall)
    python tools/emulator_e2e.py --all             # boot + install + test
    python tools/emulator_e2e.py --test --keep-avd # reuse running emulators (default)

Environment overrides:
    ANDROID_SDK   path to the SDK (default: C:\\Users\\<user>\\AppData\\Local\\Android\\Sdk)
    SERIAL_A / SERIAL_B  adb serials (default emulator-5554 / emulator-5556)
    APK           path to the debug APK (default app/build/outputs/apk/debug/app-debug.apk)

Emulator-NAT caveat (why the join address is 10.0.2.2):
  Both guests announce 10.0.2.15, which is each guest's OWN address inside its
  isolated NAT — so peer-to-peer links that use the ANNOUNCED ip (group mesh,
  file download, call media) cannot work between two emulators. Everything that
  rides the host relay works: device B reaches device A's listener through
  'adb -s A forward tcp:9999 tcp:9999' + the host-loopback alias 10.0.2.2.
  Direct chat uses the same trick in both directions: B adds A at
  10.0.2.2:9999, and A adds B at 10.0.2.2:10001 (adb forward on B maps
  host:10001 -> B:9999). The manually added contacts must be tapped by their
  forwarded address — the group-synced contacts carry the announced ip and
  would self-connect on an emulator.

Screenshots of every step land in .interop/shots/.
"""

import argparse
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SDK = os.environ.get("ANDROID_SDK") or os.path.join(
    os.environ["USERPROFILE"], "AppData", "Local", "Android", "Sdk"
)
ADB = os.path.join(SDK, "platform-tools", "adb.exe")
EMULATOR = os.path.join(SDK, "emulator", "emulator.exe")

SERIAL_A = os.environ.get("SERIAL_A", "emulator-5554")
SERIAL_B = os.environ.get("SERIAL_B", "emulator-5556")
APK = os.environ.get("APK", os.path.join(REPO, "app", "build", "outputs", "apk", "debug", "app-debug.apk"))

AVD_A = os.environ.get("AVD_A", "Medium_Phone_2")
AVD_B = os.environ.get("AVD_B", "Medium_Phone_API_36.1")

PORT = 9999            # Constants.TCP_PORT
JOIN_HOST = "10.0.2.2"  # host-loopback alias seen from inside an emulator
NICK_A = "NodeA"
NICK_B = "NodeB"
GROUP_NAME = "TestGroup"
MSG_A1 = "hello-from-A"
MSG_B1 = "reply-from-B"
MSG_A2 = "A-again"
DIRECT_MSG_B = "direct-from-B"
DIRECT_MSG_A = "direct-from-A"

SHOTS = os.path.join(REPO, ".interop", "shots")
LOG = os.path.join(REPO, ".interop", "logs", "e2e.log")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sh(serial: str, *args: str, timeout: int = 30) -> str:
    """Run an adb command; return stdout (stderr merged). UTF-8: adb prints
    the device's UTF-8 bytes, and matching Chinese UI text requires it."""
    cmd = [ADB, "-s", serial] + list(args)
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    out = (r.stdout or b"").decode("utf-8", errors="replace") + (r.stderr or b"").decode("utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} failed ({r.returncode}): {out.strip()[:300]}")
    return out


def sh_ok(serial: str, *args: str, timeout: int = 30) -> bool:
    try:
        sh(serial, *args, timeout=timeout)
        return True
    except Exception:
        return False


def wait_boot(serial: str, timeout: int = 600) -> None:
    log(f"waiting for {serial} to boot...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = sh(serial, "shell", "getprop", "sys.boot_completed").strip()
            if state == "1":
                log(f"{serial} booted")
                sh(serial, "shell", "settings", "put", "global", "window_animation_scale", "0")
                sh(serial, "shell", "settings", "put", "global", "transition_animation_scale", "0")
                sh(serial, "shell", "settings", "put", "global", "animator_duration_scale", "0")
                return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError(f"{serial} did not boot in {timeout}s")


def boot_emulator(port: int, avd: str, logfile: str) -> subprocess.Popen:
    os.makedirs(os.path.dirname(logfile), exist_ok=True)
    cmd = [EMULATOR, "-avd", avd, "-port", str(port),
           "-no-snapshot-load", "-no-boot-anim", "-netdelay", "none", "-netspeed", "full"]
    log(f"booting {avd} on port {port}: {' '.join(cmd)}")
    with open(logfile, "a", encoding="utf-8") as f:
        return subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)


PERMISSIONS = ("android.permission.ACCESS_LOCAL_NETWORK",
               "android.permission.POST_NOTIFICATIONS",
               "android.permission.CAMERA",
               "android.permission.RECORD_AUDIO")


def install_and_grant(serial: str) -> None:
    log(f"installing APK on {serial}")
    sh(serial, "install", "-r", APK, timeout=180)
    grant_permissions(serial)
    log(f"{serial}: APK installed, permissions granted")


def grant_permissions(serial: str) -> None:
    for perm in PERMISSIONS:
        sh_ok(serial, "shell", "pm", "grant", "com.zqr.localchat", perm)


def dismiss_system_dialogs(serial: str) -> None:
    """Tap through system permission dialogs if any are on screen. The app
    requests POST_NOTIFICATIONS / CAMERA / RECORD_AUDIO at runtime; pm grant
    usually prevents the dialogs, but this is a belt-and-suspenders sweep."""
    for _ in range(3):
        try:
            root = dump_ui(serial)
        except Exception:
            return
        texts = {t for t, _, _ in nodes(root)}
        if "Allow" in texts:
            x, y = find(serial, text="Allow")
            tap(serial, x, y)
            time.sleep(1.5)
        elif "While using the app" in texts:
            x, y = find(serial, text="While using the app")
            tap(serial, x, y)
            time.sleep(1.5)
        else:
            return


def launch(serial: str) -> None:
    sh(serial, "shell", "am", "force-stop", "com.zqr.localchat")
    time.sleep(1)
    sh(serial, "shell", "am", "start", "-n", "com.zqr.localchat/.MainActivity")
    time.sleep(4)
    dismiss_system_dialogs(serial)


def screenshot(serial: str, name: str) -> None:
    os.makedirs(SHOTS, exist_ok=True)
    path = os.path.join(SHOTS, f"{name}.png")
    r = subprocess.run([ADB, "-s", serial, "exec-out", "screencap", "-p"],
                       capture_output=True, timeout=30)
    if r.returncode == 0 and r.stdout:
        with open(path, "wb") as f:
            f.write(r.stdout)
        log(f"screenshot -> {os.path.relpath(path, REPO)}")


def dump_ui(serial: str, retries: int = 3) -> ET.Element:
    last = None
    for _ in range(retries):
        try:
            sh(serial, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml", timeout=30)
            xml = sh(serial, "shell", "cat", "/sdcard/window_dump.xml", timeout=30)
            return ET.fromstring(xml)
        except Exception as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"uiautomator dump failed: {last}")


def nodes(root: ET.Element):
    """Yield (text, content_desc, bounds_rect) for every visible node."""
    for n in root.iter("node"):
        if n.get("bounds") in (None, "", "[0,0][0,0]"):
            continue
        yield n.get("text", ""), n.get("content-desc", ""), parse_bounds(n.get("bounds", ""))


def parse_bounds(b: str) -> tuple:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
    if not m:
        return (0, 0, 0, 0)
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1, y1, x2, y2)


def center(rect: tuple) -> tuple:
    x1, y1, x2, y2 = rect
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def find(serial: str, text: str = "", desc: str = "", contains: str = "") -> tuple:
    """Find a node's center; raise if missing."""
    root = dump_ui(serial)
    for t, d, rect in nodes(root):
        if text and t == text:
            return center(rect)
        if desc and d == desc:
            return center(rect)
        if contains and contains in t:
            return center(rect)
    raise RuntimeError(f"node not found: text={text!r} desc={desc!r} contains={contains!r}")


def has_text(serial: str, text: str) -> bool:
    root = dump_ui(serial)
    return any(t == text or text in t for t, _, _ in nodes(root))


def wait_text(serial: str, text: str, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if has_text(serial, text):
            return True
        time.sleep(2)
    return False


def tap(serial: str, x: int, y: int) -> None:
    sh(serial, "shell", "input", "tap", str(x), str(y))
    time.sleep(1.5)


def wait_node(serial: str, timeout: int = 30, text: str = "", desc: str = "", contains: str = "") -> tuple:
    """Wait until a node matches, then return its center. Raises on timeout."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            return find(serial, text=text, desc=desc, contains=contains)
        except RuntimeError as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"node did not appear in {timeout}s: text={text!r} desc={desc!r} contains={contains!r}")


def tap_node(serial: str, text: str = "", desc: str = "", contains: str = "") -> None:
    x, y = find(serial, text=text, desc=desc, contains=contains)
    tap(serial, x, y)


def tap_node_wait(serial: str, timeout: int = 30, text: str = "", desc: str = "", contains: str = "") -> None:
    x, y = wait_node(serial, timeout, text=text, desc=desc, contains=contains)
    tap(serial, x, y)


def type_text(serial: str, field_index: int, text: str) -> None:
    """Tap the n-th visible EditText (0-based), clear it, type ASCII text.

    Re-dumps the tree right before tapping (the IME shifts the layout, so a
    stale dump would hit the wrong field) and dismisses the keyboard
    afterwards so the next field/button is at a known position."""
    root = dump_ui(serial)
    fields = [(n.get("text", ""), parse_bounds(n.get("bounds", "")))
              for n in root.iter("node") if n.get("class") == "android.widget.EditText"]
    if field_index >= len(fields):
        raise RuntimeError(f"only {len(fields)} EditText visible, wanted #{field_index}")
    x, y = center(fields[field_index][1])
    tap(serial, x, y)
    time.sleep(0.8)
    sh(serial, "shell", "input", "keyevent", "123")            # KEYCODE_MOVE_END
    for _ in range(64):
        sh(serial, "shell", "input", "keyevent", "67")         # KEYCODE_DEL
    if text:
        sh(serial, "shell", "input", "text", text.replace(" ", "%s"))
    time.sleep(0.8)
    sh(serial, "shell", "input", "keyevent", "4")              # dismiss IME
    time.sleep(0.8)


def dismiss_keyboard(serial: str) -> None:
    sh(serial, "shell", "input", "keyevent", "4")
    time.sleep(1)


def send_chat_message(serial: str, text: str) -> None:
    """Type into the single visible chat input and tap the send button.

    ENTER/IME-Send is NOT used: on the emulator's keyboard the keyevent inserts
    a newline (multiline input) instead of firing the Send action, so the text
    silently never leaves the field. type_text() dismisses the IME, which puts
    the send button back at its natural position."""
    type_text(serial, 0, text)
    tap_node_wait(serial, desc="发送")
    time.sleep(2)


def extract_numeric_id(serial: str) -> str:
    """Read '群组数字ID: 1234 5678' from the host lobby card."""
    root = dump_ui(serial)
    for t, _, _ in nodes(root):
        m = re.search(r"群组数字ID:\s*([\d\s]{8,})", t)
        if m:
            return re.sub(r"\s", "", m.group(1))
    raise RuntimeError("numeric group id not found on host lobby")


def extract_password(serial: str) -> str:
    root = dump_ui(serial)
    for t, _, _ in nodes(root):
        m = re.search(r"群组密码:\s*(\d+)", t)
        if m:
            return m.group(1)
    raise RuntimeError("group password not found on host lobby")


# ---------------------------------------------------------------- test flows

def test_create_group(serial: str, nick: str) -> None:
    log(f"[A] create group as {nick}")
    launch(serial)
    tap_node_wait(serial, desc="群组")            # MemberList -> GroupList
    tap_node_wait(serial, desc="添加群组")         # GroupList FAB
    tap_node_wait(serial, contains="创建群组")     # Setup mode select -> create form
    type_text(serial, 0, nick)                    # 你的昵称
    type_text(serial, 1, GROUP_NAME)              # 群组名称
    tap_node_wait(serial, text="创建")
    dismiss_system_dialogs(serial)
    if not wait_text(serial, "等待其他设备加入", 30):
        if not wait_text(serial, "群组成员", 30):
            raise RuntimeError("group lobby did not appear after create")


def test_join_group(serial: str, nick: str, gid: str, password: str) -> None:
    log(f"[B] join group as {nick} (id={gid} host={JOIN_HOST}:{PORT})")
    launch(serial)
    tap_node_wait(serial, desc="群组")
    tap_node_wait(serial, desc="添加群组")
    tap_node_wait(serial, contains="加入群组")
    type_text(serial, 0, nick)                      # 你的昵称
    type_text(serial, 1, gid)                       # 群组数字ID
    type_text(serial, 2, f"{JOIN_HOST}:{PORT}")
    type_text(serial, 3, password)                  # 群组密码
    tap_node_wait(serial, text="查找群组")
    if not wait_text(serial, "确认加入", 30):
        raise RuntimeError("confirm-join dialog did not appear")
    tap_node(serial, text="确认加入")
    dismiss_system_dialogs(serial)
    if not wait_text(serial, "已连接到群组", 60):
        if not wait_text(serial, "群组成员", 60):
            raise RuntimeError("B did not land in the group lobby")


def open_group_chat(serial: str) -> None:
    """From the group lobby, open the chat screen. The app must already be
    running and in the lobby — NEVER force-stop mid-flow: that kills the
    host/member connections (A↔B died this way in early test runs)."""
    tap_node_wait(serial, desc="聊天")


def is_member_list(serial: str) -> bool:
    try:
        root = dump_ui(serial)
    except Exception:
        return False
    descs = {d for _, d, _ in nodes(root)}
    return "添加成员" in descs


def back_to_member_list(serial: str) -> None:
    """BACK repeatedly until the member list (home) is showing. Stops as soon
    as the home screen is detected so an extra BACK cannot exit the app."""
    for _ in range(4):
        if is_member_list(serial):
            return
        sh(serial, "shell", "input", "keyevent", "4")
        time.sleep(1.5)
    if not is_member_list(serial):
        raise RuntimeError("could not navigate back to the member list")


def test_group_chat(serial_a: str, serial_b: str) -> None:
    log("group chat: A -> B -> A (relay, no app restarts)")
    # A opens chat and sends
    tap_node_wait(serial_a, desc="聊天")
    send_chat_message(serial_a, MSG_A1)
    screenshot(serial_a, "a_sent_msg1")
    if not wait_text(serial_a, MSG_A1, 10):
        raise RuntimeError("A did not render its own message")
    sh(serial_a, "shell", "input", "keyevent", "4")   # back to lobby
    time.sleep(1.5)
    # B opens chat and must see the message
    tap_node_wait(serial_b, desc="聊天")
    if not wait_text(serial_b, MSG_A1, 60):
        raise RuntimeError(f"B never received {MSG_A1}")
    screenshot(serial_b, "b_received_msg1")
    # B replies
    send_chat_message(serial_b, MSG_B1)
    screenshot(serial_b, "b_sent_reply")
    sh(serial_b, "shell", "input", "keyevent", "4")   # back to lobby
    time.sleep(1.5)
    # A (still running) sees the reply
    tap_node_wait(serial_a, desc="聊天")
    if not wait_text(serial_a, MSG_B1, 60):
        raise RuntimeError(f"A never received {MSG_B1}")
    screenshot(serial_a, "a_received_reply")
    sh(serial_a, "shell", "input", "keyevent", "4")   # back to lobby
    time.sleep(1.5)
    log("group chat OK")


def test_direct_chat(serial_a: str, serial_b: str) -> None:
    log("direct chat: B adds A via forward :9999, A adds B via forward :10001")
    # B: back to member list, add A by forwarded address, open the chat
    back_to_member_list(serial_b)
    tap_node_wait(serial_b, desc="添加成员")          # MemberList top bar '+'
    if not wait_text(serial_b, "IP:端口", 10):
        raise RuntimeError("add-member dialog did not appear")
    type_text(serial_b, 0, f"{JOIN_HOST}:{PORT}")
    type_text(serial_b, 1, "NodeA")
    tap_node_wait(serial_b, text="添加")
    # tap the JUST-ADDED entry (its row shows the forwarded address), not the
    # group-synced NodeA@10.0.2.15 which would self-connect on an emulator
    tap_node_wait(serial_b, contains=f"{JOIN_HOST}:{PORT}")
    if not wait_text(serial_b, "在线", 60):
        log("WARN: B direct session not shown online")
    send_chat_message(serial_b, DIRECT_MSG_B)
    if not wait_text(serial_b, DIRECT_MSG_B, 10):
        raise RuntimeError("B did not render its direct message")
    screenshot(serial_b, "b_direct_sent")
    # A: back to member list; the direct message preview must appear there
    back_to_member_list(serial_a)
    if not wait_text(serial_a, DIRECT_MSG_B, 60):
        raise RuntimeError(f"A never received direct message {DIRECT_MSG_B}")
    screenshot(serial_a, "a_direct_received")
    # A adds B via its own forward (:10001 -> B) and replies
    tap_node_wait(serial_a, desc="添加成员")
    if not wait_text(serial_a, "IP:端口", 10):
        raise RuntimeError("add-member dialog did not appear on A")
    type_text(serial_a, 0, f"{JOIN_HOST}:10001")
    type_text(serial_a, 1, "NodeB")
    tap_node_wait(serial_a, text="添加")
    tap_node_wait(serial_a, contains=f"{JOIN_HOST}:10001")
    if not wait_text(serial_a, "在线", 60):
        log("WARN: A direct session not shown online")
    send_chat_message(serial_a, DIRECT_MSG_A)
    screenshot(serial_a, "a_direct_replied")
    # B: back to member list; the reply preview must appear
    back_to_member_list(serial_b)
    if not wait_text(serial_b, DIRECT_MSG_A, 60):
        raise RuntimeError(f"B never received direct reply {DIRECT_MSG_A}")
    screenshot(serial_b, "b_direct_reply")
    log("direct chat OK")


def test_file_transfer(serial_a: str, serial_b: str) -> None:
    """File offer over the group relay + graceful download failure.

    The offer message and the whole download pipeline are exercised end to
    end. The actual byte download cannot succeed between two emulators (the
    offer carries the sender's ANNOUNCED ip 10.0.2.15, which is the receiver's
    OWN address under emulator NAT) — so the assertion is the graceful
    '下载失败' bubble, not a successful transfer. On a real LAN this same
    pipeline downloads fine."""
    log("file transfer: A offers a file, B receives the offer, download fails gracefully")
    # both devices: home -> group list -> TestGroup -> chat
    back_to_member_list(serial_a)
    back_to_member_list(serial_b)
    tap_node_wait(serial_a, desc="群组")
    tap_node_wait(serial_a, contains=GROUP_NAME)
    tap_node_wait(serial_a, desc="聊天")
    tap_node_wait(serial_b, desc="群组")
    tap_node_wait(serial_b, contains=GROUP_NAME)
    tap_node_wait(serial_b, desc="聊天")
    # A: file picker -> Downloads -> testfile.txt (pushed early in --test so the
    # media index already knows it; wait for each picker state before tapping)
    tap_node_wait(serial_a, desc="发送文件")
    wait_node(serial_a, desc="Show roots", timeout=30)
    time.sleep(2)
    tap_node(serial_a, desc="Show roots")
    wait_node(serial_a, text="SDCARD", timeout=15)      # drawer fully open
    tap_node_wait(serial_a, text="Downloads")
    wait_node(serial_a, contains="Files in Downloads", timeout=15)
    tap_node_wait(serial_a, text="testfile.txt")        # auto-selects + opens
    if not wait_text(serial_a, "已发送", 30):
        raise RuntimeError("A did not render the sent file offer")
    screenshot(serial_a, "a_file_offered")
    # B: group chat -> the offer must arrive
    tap_node_wait(serial_b, desc="聊天")
    if not wait_text(serial_b, "testfile.txt", 60):
        raise RuntimeError("B never received the file offer")
    if not wait_text(serial_b, "点击下载", 15):
        raise RuntimeError("B offer bubble not actionable")
    screenshot(serial_b, "b_file_offer")
    # B taps the offer -> save dialog -> SAVE -> download must FAIL gracefully
    tap_node_wait(serial_b, text="testfile.txt")
    tap_node_wait(serial_b, text="SAVE", timeout=20)
    if not wait_text(serial_b, "下载失败", 30):
        raise RuntimeError("download did not fail gracefully (bubble shows no 下载失败)")
    screenshot(serial_b, "b_download_failed_gracefully")
    # back to the lobby on both
    sh(serial_a, "shell", "input", "keyevent", "4")
    time.sleep(1.5)
    sh(serial_b, "shell", "input", "keyevent", "4")
    time.sleep(1.5)
    log("file transfer OK (offer + graceful failure)")


def cleanup(serial: str, uninstall: bool) -> None:
    log(f"clearing app data on {serial}")
    sh_ok(serial, "shell", "am", "force-stop", "com.zqr.localchat")
    if uninstall:
        sh(serial, "uninstall", "com.zqr.localchat", timeout=60)
    else:
        sh(serial, "shell", "pm", "clear", "com.zqr.localchat")


# ------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boot", action="store_true", help="boot both emulators")
    ap.add_argument("--install", action="store_true", help="install APK + grant permissions")
    ap.add_argument("--test", action="store_true", help="run the E2E chat flows")
    ap.add_argument("--cleanup", action="store_true", help="clear app data (add --uninstall to remove the app)")
    ap.add_argument("--uninstall", action="store_true", help="with --cleanup: uninstall instead of pm clear")
    ap.add_argument("--all", action="store_true", help="boot + install + test")
    ap.add_argument("--keep-avd", action="store_true", help="attach to already-running emulators (default when no --boot)")
    ap.add_argument("--skip-file", action="store_true",
                    help="skip the file-transfer step (slowest/most brittle: drives the system file picker)")
    args = ap.parse_args()

    if not (args.boot or args.install or args.test or args.cleanup or args.all):
        ap.error("nothing to do: pass --boot/--install/--test/--cleanup/--all")

    if args.all:
        args.boot = args.install = args.test = True

    if args.boot:
        procs = [
            boot_emulator(5554, AVD_A, os.path.join(REPO, ".interop", "logs", "emu_A_5554.log")),
            boot_emulator(5556, AVD_B, os.path.join(REPO, ".interop", "logs", "emu_B_5556.log")),
        ]
        wait_boot(SERIAL_A)
        wait_boot(SERIAL_B)
        for p in procs:
            p.poll()  # leave running; they die with the shell or via --cleanup

    wait_boot(SERIAL_A, timeout=30)
    wait_boot(SERIAL_B, timeout=30)

    if args.install:
        install_and_grant(SERIAL_A)
        install_and_grant(SERIAL_B)

    if args.test:
        # deterministic run: wipe any data left by earlier sessions, then
        # re-grant (pm clear drops the grants) and dismiss any stray dialogs
        log("clearing app data on both devices for a clean run")
        cleanup(SERIAL_A, uninstall=False)
        cleanup(SERIAL_B, uninstall=False)
        grant_permissions(SERIAL_A)
        grant_permissions(SERIAL_B)
        # push the file-transfer test payload EARLY so the media index has
        # time to see it before the picker opens later in the run
        test_file = os.path.join(REPO, ".interop", "testfile.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("LocalChat E2E file-transfer test payload 1234567890")
        sh(SERIAL_A, "push", test_file, "/sdcard/Download/testfile.txt")
        sh_ok(SERIAL_A, "shell", "am", "broadcast",
              "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
              "-d", "file:///sdcard/Download/testfile.txt")
        # B reaches A's listener through the host loopback alias 10.0.2.2:
        #   adb forward on A: host:9999  -> A:9999   (B joins/chats to A)
        #   adb forward on B: host:10001 -> B:9999   (A's direct reply to B)
        log("setting up adb forwards")
        sh(SERIAL_A, "forward", f"tcp:{PORT}", f"tcp:{PORT}")
        sh(SERIAL_B, "forward", "tcp:10001", f"tcp:{PORT}")
        try:
            test_create_group(SERIAL_A, NICK_A)
            gid = extract_numeric_id(SERIAL_A)
            pwd = extract_password(SERIAL_A)
            log(f"group id={gid} password={pwd}")
            screenshot(SERIAL_A, "a_lobby")
            test_join_group(SERIAL_B, NICK_B, gid, pwd)
            test_group_chat(SERIAL_A, SERIAL_B)
            test_direct_chat(SERIAL_A, SERIAL_B)
            if not args.skip_file:
                test_file_transfer(SERIAL_A, SERIAL_B)
            log("ALL TESTS PASSED")
        except Exception as e:
            screenshot(SERIAL_A, "FAIL_A")
            screenshot(SERIAL_B, "FAIL_B")
            log(f"TEST FAILED: {e}")
            raise

    if args.cleanup:
        cleanup(SERIAL_A, args.uninstall)
        cleanup(SERIAL_B, args.uninstall)
        log("cleanup done")


if __name__ == "__main__":
    main()
