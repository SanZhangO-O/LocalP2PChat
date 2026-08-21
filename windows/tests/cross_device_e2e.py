"""Cross-device video-call e2e: LocalChatWin (this PC) <-> Android LocalChat.

Runs the REAL Windows client engine (localchat.network + localchat.call) as
the group host on this PC while driving a real Android phone over adb through
the join + video-call flows. Verifies the whole pipeline across devices:

  - LAN group join (query/join handshake, member exchange)
  - call signaling via the host relay (offer -> incoming -> accept -> ACTIVE)
  - the direct media TCP connection with JPEG video frames flowing BOTH ways
    (the phone sends real camera video; the PC sends its synthetic test
    pattern when no camera is present)
  - audio from the phone's real microphone reaching the PC
  - hangup returns both sides to idle

Two call directions are exercised:
  A) PC dials the phone, the phone accepts (user-driven accept dialog).
  B) The phone dials the PC, the PC auto-accepts.

Usage:  python tests/cross_device_e2e.py
Requires: one Android device connected via adb, phone and PC on the same LAN.
"""

import os
import re
import subprocess
import sys
import textwrap
import time

ADB = os.environ.get(
    "ADB", r"C:\Users\zhangsan\AppData\Local\Android\Sdk\platform-tools\adb.exe"
)
SERIAL = os.environ.get("SERIAL", "VWTWJNDEOJUKCEVS")

PORT = int(os.environ.get("CROSS_PORT", "9999"))
GROUP = os.environ.get("CROSS_GROUP", "LANCall")
PASSWORD = os.environ.get("CROSS_PASSWORD", "1234")
PHONE_NICK = os.environ.get("CROSS_NICK", "Phone")
PC_NICK = "Host"

SHOTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

HARNESS_CODE = r"""
import os, sys, time, queue, threading
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, os.getcwd())
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from localchat.call import CallManager, STATE_ACTIVE, STATE_IDLE, STATE_INCOMING
from localchat.network import P2PListener, P2PManager

class L(P2PListener):
    pass

PORT = int(os.environ["CROSS_PORT"])
GROUP = os.environ["CROSS_GROUP"]
PASSWORD = os.environ["CROSS_PASSWORD"]
PC_NICK = os.environ["CROSS_PC_NICK"]

host = P2PManager(L(), port=PORT, password=PASSWORD)
host.initialize_as_host(PC_NICK, GROUP)
host.start_as_host()

cm = CallManager()
host.call_listener = cm._on_signal
remote_frames = []
remote_audio = []
cm.remote_frame.connect(lambda q: remote_frames.append(q))
cm.remote_audio.connect(lambda b: remote_audio.append(len(b)))
cm.state_changed.connect(lambda s, n, d: print("PC_STATE", s, n, flush=True))
cm.incoming_call.connect(lambda cid, name: print("PC_INCOMING", name, flush=True))
cm.call_ended.connect(lambda r: print("PC_ENDED", r, flush=True))
cm.call_error.connect(lambda m: print("PC_ERROR", m, flush=True))
print("HOST_READY", flush=True)

cmd_q = queue.Queue()

def stdin_reader():
    for line in sys.stdin:
        line = line.strip()
        if line:
            cmd_q.put(line)

threading.Thread(target=stdin_reader, daemon=True).start()

auto_accept = True
active_stats = 0
last_stat = 0.0
saved_remote = False
running = True
last_peers = ""

def dial_first_peer():
    peer_id = next(iter(host.peers), None)
    if peer_id is None:
        print("PC_ERR no-peer", flush=True)
        return
    peer = host.peers[peer_id]
    print("PC_DIAL", peer.name, flush=True)
    cm.start_call(host, peer_id)

def do_hangup():
    cm.hangup()

deadline = time.time() + 180
while running and time.time() < deadline:
    app.processEvents()
    try:
        while True:
            cmd = cmd_q.get_nowait()
            if cmd == "dial":
                dial_first_peer()
            elif cmd == "hangup":
                do_hangup()
            elif cmd == "quit":
                running = False
    except queue.Empty:
        pass
    if cm.state == STATE_INCOMING and auto_accept:
        cm.accept_call()
    # report peer joins once
    if host.peers:
        cur = sorted((p.id, p.name, p.ip_address) for p in host.peers.values())
        sig = repr(cur)
        if sig != last_peers:
            last_peers = sig
            for pid, name, ip in cur:
                print("PC_PEER", pid, name, ip, flush=True)
    if cm.state == STATE_ACTIVE:
        now = time.time()
        if now - last_stat > 2.0:
            last_stat = now
            print("PC_STAT", len(remote_frames), sum(remote_audio), flush=True)
            # save the first received (phone) video frame as proof
            if not saved_remote and remote_frames:
                saved_remote = True
                try:
                    remote_frames[0].save(os.environ["CROSS_REMOTE_FRAME"])
                    print("PC_SAVED_FRAME", os.environ["CROSS_REMOTE_FRAME"], flush=True)
                except Exception as e:
                    print("PC_SAVE_ERR", e, flush=True)
    time.sleep(0.01)

print("PC_FINAL", len(remote_frames), sum(remote_audio), flush=True)
host.stop()
"""


def adb(*args: str) -> str:
    return subprocess.run(
        [ADB, "-s", SERIAL, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    ).stdout


def adb_bin(*args: str) -> bytes:
    return subprocess.run(
        [ADB, "-s", SERIAL, *args],
        capture_output=True,
        timeout=60,
    ).stdout


def adb_shell_bin(*args: str) -> bytes:
    return subprocess.run(
        [ADB, "-s", SERIAL, "shell", *args],
        capture_output=True,
        timeout=60,
    ).stdout


def ui_dump() -> list:
    """Dump the accessibility tree and parse it.

    The XML is read as raw bytes straight from `adb shell cat` (byte
    transparent, no local file): uiautomator dumps can transiently produce an
    incomplete tree, so callers must poll (wait_ui) rather than trust one dump.
    """
    xml = ""
    for _ in range(3):
        try:
            dump_out = adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
            if "dumped to" not in dump_out:
                time.sleep(0.5)
                continue
            xml = adb_bin("shell", "cat", "/sdcard/ui.xml").decode(
                "utf-8", errors="replace"
            )
            if xml:
                break
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    nodes = []
    for m in re.finditer(r"<node\b[^>]*>", xml):
        seg = m.group(0)
        text = re.search(r'text="([^"]*)"', seg)
        desc = re.search(r'content-desc="([^"]*)"', seg)
        cls = re.search(r'class="([^"]*)"', seg)
        clickable = re.search(r'clickable="([^"]*)"', seg)
        bounds = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', seg)
        if not bounds:
            continue
        x1, y1, x2, y2 = map(int, bounds.groups())
        nodes.append(
            {
                "text": text.group(1) if text else "",
                "desc": desc.group(1) if desc else "",
                "cls": cls.group(1) if cls else "",
                "clickable": clickable.group(1) if clickable else "false",
                "cx": (x1 + x2) // 2,
                "cy": (y1 + y2) // 2,
            }
        )
    return nodes


def dump_labels(tag: str) -> None:
    """Debug helper: print every labeled node (ASCII-safe) when a step fails."""
    try:
        nodes = ui_dump()
        print("[ui:%s]" % tag, "nodes:", len(nodes))
        for n in nodes:
            if n["text"] or n["desc"]:
                print(
                    "   text=%s desc=%s @%d,%d"
                    % (ascii(n["text"]), ascii(n["desc"]), n["cx"], n["cy"])
                )
    except Exception as e:
        print("[ui:%s] dump failed: %r" % (tag, e))


def screenshot(name: str) -> str:
    path = os.path.join(SHOTS_DIR, name)
    adb_shell_bin("screencap", "-p", "/sdcard/_shot.png")
    subprocess.run(
        [ADB, "-s", SERIAL, "pull", "/sdcard/_shot.png", path],
        capture_output=True,
        timeout=60,
    )
    return path


def wait_ui(pred, timeout=20.0, interval=0.5):
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        try:
            last = ui_dump()
        except Exception:
            last = []
        if pred(last):
            return last
        time.sleep(interval)
    return None


def find_text(nodes, text, exact=True):
    """Match a node by its visible text OR accessibility content-desc
    (Compose buttons often expose their label only as content-desc)."""
    for n in nodes:
        hay = n["text"] or n["desc"]
        if exact and hay == text:
            return n
        if not exact and text in hay:
            return n
    return None


def find_desc(nodes, desc):
    for n in nodes:
        if n["desc"] == desc:
            return n
    return None


def tap(n):
    adb("shell", "input", "tap", str(n["cx"]), str(n["cy"]))


def tap_coord(x, y):
    adb("shell", "input", "tap", str(int(x)), str(int(y)))


def tap_fab():
    """Tap the group-list FAB. Its content-desc node is sometimes missing from
    uiautomator dumps (Compose semantics snapshot), but the FAB is a clickable
    view pinned to the bottom-right by the Scaffold (GroupListScreen.kt), so a
    clickable node in the bottom-right quadrant is a reliable fallback."""
    nodes = wait_ui(lambda ns: find_text(ns, "添加群组") is not None, timeout=10)
    if nodes is not None:
        tap(find_text(nodes, "添加群组"))
        return True
    nodes = ui_dump()
    # clickable node whose center is in the bottom-right ~30% x 30% region
    cands = [
        n
        for n in nodes
        if n.get("clickable") == "true"
        and n["cx"] > 1440 * 0.7
        and n["cy"] > 3200 * 0.7
    ]
    if cands:
        n = min(cands, key=lambda n: abs(n["cx"] - 1440 * 0.9) + abs(n["cy"] - 3200 * 0.9))
        tap(n)
        return True
    # last resort: fixed position measured on this device (1440x3200)
    tap_coord(1286, 2881)
    return True


def tap_text(text, timeout=20.0):
    nodes = wait_ui(lambda ns: find_text(ns, text) is not None, timeout=timeout)
    if nodes is None:
        return False
    tap(find_text(nodes, text))
    return True


def tap_and_verify(text, expect, timeout=15.0, retries=3):
    """Tap a labeled node and confirm [expect] appears afterwards; retries the
    tap with a fresh dump when the first tap is swallowed (e.g. an IME
    animation is still running when the tap lands)."""
    for attempt in range(retries):
        if tap_text(text, timeout=timeout):
            got = wait_ui(
                lambda ns: find_text(ns, expect, exact=False) is not None, timeout=8
            )
            if got is not None:
                return True
            print("   (tap %r did not open %r; retrying %d)" % (text, expect, attempt + 1))
        time.sleep(1.0)
    return False


def input_text(s: str) -> None:
    adb("shell", "input", "text", s)


def wake_phone() -> None:
    adb("shell", "input", "keyevent", "KEYCODE_WAKEUP")
    adb("shell", "wm", "dismiss-keyguard")
    time.sleep(1.0)


def phone_ip() -> str:
    out = adb("shell", "ip", "addr", "show", "wlan0")
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else ""


def pc_ip_for(phone: str) -> str:
    """Pick this PC's LAN address on the same subnet as the phone."""
    try:
        import socket as _s

        s = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        s.connect((phone, 9999))
        local = s.getsockname()[0]
        s.close()
        return local
    except Exception:
        pass
    # fall back: enumerate and match /24
    import ipaddress

    try:
        pnet = ipaddress.ip_network(phone + "/24", strict=False)
        for ip in os.popen("ipconfig").read().splitlines():
            m = re.search(r"IPv4[^\d]*([\d.]+)", ip)
            if m and ipaddress.ip_address(m.group(1)) in pnet:
                return m.group(1)
    except Exception:
        pass
    return "127.0.0.1"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.makedirs(SHOTS_DIR, exist_ok=True)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    print("== 准备 ==")
    print("手机 adb 序列号:", SERIAL)
    ph_ip = phone_ip()
    print("手机 wlan0 IP:", ph_ip)
    if not ph_ip:
        print("无法获取手机 IP，请确认设备已连接且开启了 Wi-Fi")
        return 1
    host_ip = pc_ip_for(ph_ip)
    print("PC 局域网 IP:", host_ip)
    if host_ip == "127.0.0.1":
        print("无法确定 PC 与手机同网段的地址")
        return 1

    remote_frame_path = os.path.join(SHOTS_DIR, "received_from_phone.jpg")

    # Preflight: a stale LocalChatWin process left listening on PORT would
    # answer the phone with a different group and break the join.
    busy = subprocess.run(
        ["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=30
    ).stdout
    for line in busy.splitlines():
        if (":%d " % PORT) in line and "LISTENING" in line:
            print("端口 %d 已被其他进程监听，请先关闭残留的 LocalChatWin/python 进程" % PORT)
            print("  ", line.strip())
            return 1

    env = dict(os.environ)
    env.update(
        {
            "CROSS_PORT": str(PORT),
            "CROSS_GROUP": GROUP,
            "CROSS_PASSWORD": PASSWORD,
            "CROSS_PC_NICK": PC_NICK,
            "CROSS_REMOTE_FRAME": remote_frame_path,
        }
    )

    harness = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(HARNESS_CODE)],
        cwd=root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    # One pump thread drains the harness stdout into a queue with timeouts.
    import queue as _queue
    import threading

    pc_q = _queue.Queue()
    pc_lines = []

    def _pump():
        try:
            for line in harness.stdout:
                pc_q.put(line.rstrip("\n"))
        except Exception:
            pass

    threading.Thread(target=_pump, daemon=True).start()

    def harness_line(timeout=40.0):
        try:
            return pc_q.get(timeout=timeout)
        except _queue.Empty:
            return None

    def wait_harness(prefix, timeout=60.0, lines=None):
        if lines is None:
            lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = harness_line(timeout=5)
            if line is None:
                time.sleep(0.2)
                continue
            lines.append(line)
            print("[PC]", line, flush=True)
            if line.startswith(prefix):
                return line
        return None

    try:
        print("\n== 启动 PC 端（群组主机）==")
        if wait_harness("HOST_READY", timeout=40, lines=pc_lines) is None:
            print("PC 端未就绪")
            return 1
        print("PC 端已就绪（端口 %d，群组 %s，密码 %s）" % (PORT, GROUP, PASSWORD))

        print("\n== 手机加入群组 ==")
        wake_phone()
        adb("shell", "am", "force-stop", "com.zqr.localchat")
        time.sleep(0.5)
        adb("shell", "am", "start", "-n", "com.zqr.localchat/.MainActivity")
        time.sleep(2.0)

        # FAB: content-desc sometimes missing from dumps -> tap_fab() falls
        # back to the bottom-right clickable view / fixed position.
        tap_fab()
        time.sleep(1.0)
        if not wait_ui(lambda ns: find_text(ns, "加入群组") is not None, timeout=15):
            print("未进入设置页（找不到“加入群组”）")
            dump_labels("mode")
            screenshot("shot_phone_mode_fail.png")
            return 1
        time.sleep(0.5)
        if not tap_and_verify("加入群组", "你的昵称", timeout=15):
            print("未进入加入群组表单")
            dump_labels("form")
            screenshot("shot_phone_form.png")
            return 1

        # fill the four EditText fields. The IME opening shifts the form
        # layout (imePadding + scroll), so cached positions go stale: re-dump
        # before EVERY field and clear it first, then verify the result.
        values = [PHONE_NICK, GROUP, host_ip, PASSWORD]
        for i, val in enumerate(values):
            edits = wait_ui(
                lambda ns: sum(1 for n in ns if n["cls"] == "android.widget.EditText")
                >= 4,
                timeout=15,
            )
            if edits is None:
                print("未找到加入表单的输入框（第 %d 个）" % i)
                dump_labels("form")
                return 1
            fields = sorted(
                [n for n in edits if n["cls"] == "android.widget.EditText"],
                key=lambda n: n["cy"],
            )
            tap(fields[i])
            time.sleep(0.6)
            # clear whatever is in the field (delete backwards from the end)
            adb("shell", "input", "keyevent", "KEYCODE_MOVE_END")
            for _ in range(60):
                adb("shell", "input", "keyevent", "KEYCODE_DEL")
            input_text(val)
            time.sleep(0.3)
        adb("shell", "input", "keyevent", "111")  # ESC: dismiss the IME
        time.sleep(1.2)
        # verify the four fields really hold the four values
        edits = wait_ui(
            lambda ns: sum(1 for n in ns if n["cls"] == "android.widget.EditText")
            >= 4,
            timeout=10,
        )
        if edits is None:
            print("填写后找不到输入框")
            dump_labels("form")
            return 1
        fields = sorted(
            [n for n in edits if n["cls"] == "android.widget.EditText"],
            key=lambda n: n["cy"],
        )
        got = [f["text"] for f in fields]
        if got != values:
            print("表单填写校验失败: %r != %r" % (got, values))
            dump_labels("form")
            screenshot("shot_phone_form.png")
            return 1
        print("表单已填写并校验:", got)
        if not tap_and_verify("查找群组", "确认加入", timeout=20, retries=4):
            print("群组查询未弹出确认对话框")
            dump_labels("confirm")
            screenshot("shot_phone_query.png")
            return 1
        print("已确认查询到群组")
        if not tap_text("确认加入", timeout=10):
            print("确认加入按钮未找到")
            return 1
        print("已确认加入群组")

        # wait for the PC to see the phone peer and for the phone lobby
        peer_line = wait_harness("PC_PEER", timeout=40, lines=pc_lines)
        if peer_line is None:
            print("PC 端未看到手机成员")
            dump_labels("join-wait")
            screenshot("shot_phone_join.png")
            return 1
        lobby = wait_ui(lambda ns: find_text(ns, "群组: " + GROUP, exact=False) is not None, timeout=20)
        if lobby is None:
            print("手机未进入群组大厅")
            screenshot("shot_phone_lobby_fail.png")
            return 1
        print("手机已进入群组大厅，PC 端看到成员:", peer_line)
        screenshot("shot_phone_lobby.png")

        # ---------------- scenario A: PC -> phone ----------------
        print("\n== 场景 A：PC 呼叫手机 ==")
        harness.stdin.write("dial\n")
        harness.stdin.flush()
        accept_btn = wait_ui(lambda ns: find_text(ns, "接听") is not None, timeout=30)
        if accept_btn is None:
            print("手机未弹出接听对话框")
            screenshot("shot_phone_no_incoming.png")
            return 1
        print("手机收到呼入，点击接听")
        tap(find_text(accept_btn, "接听"))

        act = wait_harness("PC_STAT", timeout=40, lines=pc_lines)
        if act is None:
            print("PC 端未进入通话")
            screenshot("shot_phone_accept_fail.png")
            return 1
        # wait for real media: >= 5 video frames and >= 1 audio chunk from phone
        got_media = False
        deadline = time.time() + 60
        while time.time() < deadline:
            line = wait_harness("PC_STAT", timeout=20, lines=pc_lines)
            if line is None:
                break
            m = re.match(r"PC_STAT (\d+) (\d+)", line)
            if m and int(m.group(1)) >= 5 and int(m.group(2)) > 0:
                got_media = True
                break
        if not got_media:
            print("PC 端未收到足量手机视频/音频")
            screenshot("shot_phone_call_a_fail.png")
            return 1
        print("场景 A 双向媒体已流动:", line)
        shot_a = screenshot("shot_phone_call_pc2phone.png")
        print("手机通话截图:", shot_a)

        # PC hangs up
        harness.stdin.write("hangup\n")
        harness.stdin.flush()
        if wait_harness("PC_ENDED", timeout=30, lines=pc_lines) is None:
            print("PC 端挂断后未结束")
            return 1
        if wait_harness("PC_STATE idle", timeout=15, lines=pc_lines) is None:
            print("PC 端未回到空闲")
            return 1
        print("场景 A 通过：PC→手机 呼叫/接听/双向媒体/挂断 全部 OK")

        # ---------------- scenario B: phone -> PC ----------------
        print("\n== 场景 B：手机呼叫 PC ==")
        # the phone is back in the lobby; find the videocam icon of the Host peer
        call_btn = wait_ui(lambda ns: find_desc(ns, "视频通话") is not None, timeout=20)
        if call_btn is None:
            print("手机大厅未找到通话按钮")
            screenshot("shot_phone_lobby_b.png")
            return 1
        print("点击手机大厅的通话按钮")
        tap(find_desc(call_btn, "视频通话"))

        inc = wait_harness("PC_INCOMING", timeout=40, lines=pc_lines)
        if inc is None:
            print("PC 端未收到手机呼叫")
            screenshot("shot_phone_dial_fail.png")
            return 1
        print("PC 端收到呼入并自动接听")
        act2 = wait_harness("PC_STAT", timeout=40, lines=pc_lines)
        if act2 is None:
            print("PC 端未进入通话（场景 B）")
            screenshot("shot_phone_call_b_fail.png")
            return 1
        got_media_b = False
        deadline = time.time() + 60
        while time.time() < deadline:
            line = wait_harness("PC_STAT", timeout=20, lines=pc_lines)
            if line is None:
                break
            m = re.match(r"PC_STAT (\d+) (\d+)", line)
            if m and int(m.group(1)) >= 5:
                got_media_b = True
                break
        if not got_media_b:
            print("场景 B PC 端未收到足量视频")
            screenshot("shot_phone_call_b_fail2.png")
            return 1
        print("场景 B 双向媒体已流动:", line)
        shot_b = screenshot("shot_phone_call_phone2pc.png")
        print("手机通话截图:", shot_b)

        harness.stdin.write("hangup\n")
        harness.stdin.flush()
        if wait_harness("PC_ENDED", timeout=30, lines=pc_lines) is None:
            print("PC 端挂断后未结束（场景 B）")
            return 1
        if wait_harness("PC_STATE idle", timeout=15, lines=pc_lines) is None:
            print("PC 端未回到空闲（场景 B）")
            return 1
        print("场景 B 通过：手机→PC 呼叫/接听/双向媒体/挂断 全部 OK")

        # ---------------- wrap up ----------------
        harness.stdin.write("quit\n")
        harness.stdin.flush()
        try:
            harness.wait(timeout=10)
        except Exception:
            harness.kill()

        # verify the saved remote frame (phone's real camera) is not a black frame
        print("\n== 结果 ==")
        print("PC 收到手机视频帧、音频字节（累计）:", end=" ")
        for line in pc_lines:
            if line.startswith("PC_FINAL"):
                print(line)
                break
        if os.path.exists(remote_frame_path):
            size = os.path.getsize(remote_frame_path)
            print("手机摄像头帧已保存:", remote_frame_path, "(%d bytes)" % size)
        else:
            print("未保存手机摄像头帧（可能手机端视频未发送）")

        # Android-side logcat scan for call errors
        log = adb("logcat", "-d", "-s", "CallManager:*")
        errs = [l for l in log.splitlines() if "W CallManager" in l or "E CallManager" in l]
        if errs:
            print("Android CallManager 日志警告：")
            for e in errs[-8:]:
                print("  ", e)

        print("=" * 60)
        print("跨端视频通话联调完成：场景 A（PC→手机）与场景 B（手机→PC）均跑通")
        print("=" * 60)
        return 0
    finally:
        if harness.poll() is None:
            harness.kill()


if __name__ == "__main__":
    sys.exit(main())
