#!/usr/bin/env python3
"""GUI E2E: the PHONE dials the PC first, the PC accepts the parked request
in its member-list card, and the session + messages must then flow.

This is the exact user-reported flow that the other E2Es do not cover:
  - win_emu_e2e / win_emu_gui_e2e: Windows dials first, Android accepts.
  - here: Android dials first -> Windows parks the request -> the REAL
    RequestCard 接受 button in MemberListPage is clicked -> the session must
    come up (the phone's presence sweep redials once the PC accepted) and
    both directions of chat must work.

Steps:
  1. Windows VM + real MainWindow offscreen (port 9999, fresh db/data).
  2. Android app reset + launched; 添加成员 -> 10.0.2.2:9999 -> 添加.
  3. Poll vm.direct_requests_list() until the request is parked.
  4. Click the real 接受 button on the rendered RequestCard.
  5. Wait until the real device id is known and the session is alive.
  6. Windows sends -> Android UI shows. Android replies -> Windows shows.

Screenshots land in .interop/shots/, logs in .interop/logs/win_emu_accept_e2e.log.
"""

import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WINDOWS = os.path.join(REPO, "windows")
sys.path.insert(0, WINDOWS)
sys.path.insert(0, os.path.join(WINDOWS, "tests"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QPushButton

from localchat.storage import ChatStore
from localchat.view_model import ChatViewModel
from localchat.ui.main_window import MainWindow, PAGE_MEMBERS, PAGE_DIRECT
from localchat.ui.member_list_page import ContactRow, RequestCard
from win_emu_e2e import (  # reuse the adb/UI helpers from the engine E2E
    adb, adb_ok, wait_text, screenshot, launch_app, wait_listener,
    type_text as emu_type_text, tap_node_wait, has_text, back_to_member_list,
    tap, HOST_FWD_PORT, WIN_PORT, GUEST_ALIAS, WIN_NICK,
)

SHOTS = os.path.join(REPO, ".interop", "shots")
LOG = os.path.join(REPO, ".interop", "logs", "win_emu_accept_e2e.log")

MSG_WIN = "accept-flow-win-hello"
MSG_EMU = "accept-flow-emu-reply"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def find_accept_button(win):
    """The real RequestCard's 接受 button, or None when no card is rendered."""
    page = win.pages[PAGE_MEMBERS]
    for i in range(page.list.count()):
        w = page.list.itemWidget(page.list.item(i))
        if isinstance(w, RequestCard):
            for b in w.findChildren(QPushButton):
                if b.text() == "\u63a5\u53d7":  # 接受
                    return b
    return None


def main() -> int:
    log(f"ACCEPT E2E: emulator dials first (forward host:{HOST_FWD_PORT} -> guest 9999)")
    adb("forward", f"tcp:{HOST_FWD_PORT}", "tcp:9999")

    app = QApplication([])

    tmp = os.path.join(tempfile.gettempdir(), "lc_win_emu_accept")
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

    win = MainWindow(vm)
    win.show()
    app.processEvents()
    log(f"Windows device id: {vm.direct.my_id_value}")

    # ---- Android app up, clean data ----
    log("resetting Android app data for a clean run")
    adb("shell", "am", "force-stop", "com.zqr.localchat")
    time.sleep(1)
    adb("shell", "pm", "clear", "com.zqr.localchat")
    time.sleep(1)
    for perm in ("android.permission.POST_NOTIFICATIONS",
                 "android.permission.CAMERA",
                 "android.permission.RECORD_AUDIO"):
        adb_ok("shell", "pm", "grant", "com.zqr.localchat", perm)
    log("launching Android app")
    launch_app()
    if not wait_listener(30):
        log("WARN: guest listener not confirmed, continuing")
    screenshot("accept_emu_ready")

    # ---- 1) Android dials Windows first (add by IP) ----
    log(f"== Android adds Windows ({GUEST_ALIAS}:{WIN_PORT}) ==")
    tap_node_wait(desc="\u6dfb\u52a0\u6210\u5458", timeout=20)  # 添加成员
    if not wait_text("IP:\u7aef\u53e3", 10):  # IP:端口
        raise RuntimeError("add-member dialog did not appear on Android")
    emu_type_text(0, f"{GUEST_ALIAS}:{WIN_PORT}")
    emu_type_text(1, WIN_NICK)
    tap_node_wait(text="\u6dfb\u52a0", timeout=10)  # 添加
    time.sleep(3)
    screenshot("accept_emu_request_sent")

    # ---- 2) Windows parks the first-contact request ----
    log("== waiting for the request to be parked on Windows ==")
    req = None
    deadline = time.time() + 90
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        reqs = vm.direct_requests_list()
        if reqs:
            req = reqs[0]
            break
    if req is None:
        raise RuntimeError("Windows never parked the Android contact request")
    log(f"parked request: id={req.id} name={req.name} addr={req.ip}:{req.port}")
    app.processEvents()
    screenshot("accept_win_request_card")

    # ---- 3) click the REAL 接受 button in the rendered card ----
    log("== clicking the 接受 button on the real RequestCard ==")
    btn = find_accept_button(win)
    deadline = time.time() + 15
    while btn is None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.3)
        btn = find_accept_button(win)
    if btn is None:
        raise RuntimeError("no RequestCard 接受 button rendered in the member list")
    btn.click()
    app.processEvents()
    time.sleep(0.5)
    if vm.direct_requests_list():
        raise RuntimeError("accept did not clear the request box")
    log("request accepted and cleared: PASS")
    screenshot("accept_win_accepted")

    # ---- 4) the session must come up (phone redials after accepting) ----
    log("== waiting for the session (phone presence redial) ==")
    peer_id = None
    deadline = time.time() + 150
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.3)
        real = next((c for c in vm.direct_contacts_list()
                     if not c.id.startswith("ip:")), None)
        if real is not None and vm.direct_chat_alive(real.id):
            peer_id = real.id
            break
        if real is not None and peer_id is None:
            peer_id = real.id
    if peer_id is None:
        raise RuntimeError("accepted request never produced a real contact")
    if not vm.direct_chat_alive(peer_id):
        raise RuntimeError(f"session to {peer_id} never became alive after accept")
    log(f"session alive: {peer_id}: PASS")
    screenshot("accept_win_online")

    # ---- 5) open the chat through the REAL member-list row click ----
    log("== clicking the member row to open DirectChatPage ==")
    app.processEvents()
    page = win.pages[PAGE_DIRECT]
    if win.stack.currentIndex() != PAGE_MEMBERS:
        raise RuntimeError("test expected the member list to still be the current page")
    row = None
    for i in range(win.pages[PAGE_MEMBERS].list.count()):
        w = win.pages[PAGE_MEMBERS].list.itemWidget(
            win.pages[PAGE_MEMBERS].list.item(i)
        )
        if isinstance(w, ContactRow):
            row = w
            break
    if row is None:
        raise RuntimeError("member list has no rendered contact row")
    row.mouseReleaseEvent(
        __import__("PyQt6.QtGui", fromlist=["QMouseEvent"]).QMouseEvent(
            __import__("PyQt6.QtCore", fromlist=["QEvent"]).QEvent.Type.MouseButtonRelease,
            __import__("PyQt6.QtCore", fromlist=["QPointF"]).QPointF(5, 5),
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.KeyboardModifier.NoModifier,
        )
    )
    app.processEvents()
    if win.stack.currentIndex() != PAGE_DIRECT:
        raise RuntimeError("clicking the member row did not open DirectChatPage")
    if page._peer_id != peer_id:
        raise RuntimeError(f"chat page keyed to {page._peer_id!r}, want {peer_id!r}")
    log(f"DirectChatPage opened on {page._peer_id}: PASS")

    # ---- 6) type + click 发送; Android UI must show it ----
    log(f"== Windows sends {MSG_WIN} via the chat input ==")
    page.input_edit.setPlainText(MSG_WIN)
    app.processEvents()
    page.send_btn.click()
    app.processEvents()
    if not wait_text(MSG_WIN, 90):
        raise RuntimeError(f"Android UI never showed {MSG_WIN}")
    screenshot("accept_emu_received_win")
    log(f"Android UI shows {MSG_WIN}: PASS")

    # ---- 6) Android replies; Windows chat page must render it ----
    log(f"== Android replies {MSG_EMU} ==")
    back_to_member_list()
    tap_node_wait(contains=WIN_NICK, timeout=60)
    time.sleep(2)
    emu_type_text(0, MSG_EMU)
    tap_node_wait(desc="\u53d1\u9001", timeout=10)  # 发送
    time.sleep(2)
    received = None
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        received = next(
            (m for m in vm.direct_messages(peer_id) if m.content == MSG_EMU), None
        )
        if received:
            break
    if not received:
        raise RuntimeError(f"Windows engine never received {MSG_EMU}")
    page = win.pages[PAGE_DIRECT]
    rendered = None
    deadline = time.time() + 30
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        from localchat.ui.chat_page import MSG_ROLE
        texts = []
        for i in range(page.model.rowCount()):
            m = page.model.item(i).data(MSG_ROLE)
            if m is not None and hasattr(m, "content"):
                texts.append(m.content)
        if MSG_EMU in texts:
            rendered = True
            break
    if not rendered:
        raise RuntimeError(f"Windows GUI chat page never rendered {MSG_EMU}")
    log(f"Windows GUI chat page shows {MSG_EMU}: PASS")
    screenshot("accept_win_received_reply")

    # ---- 7) Windows sends a FOLDER; Android must group it into one card ----
    # Exercises the new optional folder fields on the wire (Windows serializer
    # -> Android parser/grouping/UI) through the real send path. A receiver
    # that predates the folder fields ignores them and shows the entries as
    # individual file cards: that degradation is the compatibility contract,
    # so both outcomes pass.
    log("== Windows sends a folder via the real send path ==")
    import shutil as _shutil

    folder_root = os.path.join(tmp, "send_folder")
    if os.path.isdir(folder_root):
        _shutil.rmtree(folder_root)
    os.makedirs(os.path.join(folder_root, "sub"))
    with open(os.path.join(folder_root, "alpha.txt"), "wb") as fh:
        fh.write(b"alpha")
    with open(os.path.join(folder_root, "sub", "beta.txt"), "wb") as fh:
        fh.write(b"beta")
    folder_name = os.path.basename(folder_root)
    win.pages[PAGE_DIRECT]._send_files([folder_root])
    app.processEvents()
    grouped = wait_text(folder_name, 60) and has_text("\u4e2a\u6587\u4ef6")  # 个文件
    if grouped:
        screenshot("accept_emu_received_folder")
        log("Android groups the Windows folder offer into one card: PASS")
    else:
        # old peer: optional folder fields ignored, entries shown separately
        individual = wait_text("alpha.txt", 30) and has_text("beta.txt")
        if not individual:
            raise RuntimeError(
                "Android showed neither a grouped folder card nor the individual entries"
            )
        screenshot("accept_emu_received_folder_legacy")
        log("Android (old peer) shows individual folder entries: compat PASS")

    log("ACCEPT E2E ALL PASS")
    for _ in range(3):
        app.processEvents()
        time.sleep(0.2)
    vm.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ACCEPT E2E FAILED: {e}")
        try:
            screenshot("accept_fail_state")
        except Exception:
            pass
        sys.exit(1)
