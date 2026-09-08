#!/usr/bin/env python3
"""GUI-level E2E: drive the REAL Windows MainWindow through the add-contact
by IP flow against the Android app on the emulator.

Runs the actual PyQt6 UI classes (MemberListPage add-member dialog, member
list rows, DirectChatPage) offscreen, clicking real widgets, against the
Android app in emulator-5554 (adb-forwarded 127.0.0.1:10099 -> guest:9999).

Steps:
  1. member list -> 添加成员 dialog -> fill IP:端口 127.0.0.1:10099 -> 添加
  2. the new row appears in the member list; clicking it opens DirectChatPage
  3. the header flips to 在线 after the handshake (presence dial)
  4. type a message, click 发送 -> the Android UI must show it
  5. Android adds Windows by IP (10.0.2.2:9999) and replies -> the Windows
     chat page must render the reply

Screenshots land in .interop/shots/, logs in .interop/logs/win_emu_gui_e2e.log.
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

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from localchat.storage import ChatStore
from localchat.view_model import ChatViewModel
from localchat.ui.main_window import MainWindow, PAGE_MEMBERS, PAGE_DIRECT
from win_emu_e2e import (  # reuse the adb/UI helpers from the engine E2E
    adb, adb_ok, wait_text, screenshot, log, launch_app, wait_listener,
    type_text as emu_type_text, tap_node_wait, has_text, back_to_member_list,
    HOST_FWD_PORT, WIN_PORT, GUEST_ALIAS, WIN_NICK, EMU_NICK,
)

SHOTS = os.path.join(REPO, ".interop", "shots")
LOG = os.path.join(REPO, ".interop", "logs", "win_emu_gui_e2e.log")
GUI_MSG = "gui-message-from-win"
GUI_REPLY = "gui-reply-from-emu"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> int:
    log(f"GUI E2E: serial window -> emulator (forward host:{HOST_FWD_PORT} -> guest 9999)")
    adb("forward", f"tcp:{HOST_FWD_PORT}", "tcp:9999")

    app = QApplication([])

    tmp = os.path.join(tempfile.gettempdir(), "lc_win_emu_gui")
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

    # ---- Android app up and listening (CLEAN app data: a previous run's
    # contacts would add group-synced/announced-IP rows and derail the taps) ----
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
    screenshot("gui_emu_ready")

    # ---- 1) drive the REAL add-member dialog ----
    log("== 添加成员 dialog (real widgets) ==")
    add_btn = win.pages[PAGE_MEMBERS].add_btn
    driven = {"done": False, "ok": False}

    def drive_dialog() -> None:
        # find the modal dialog that just opened (exec() blocks until closed)
        dlg = None
        for w in QApplication.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                dlg = w
                break
        if dlg is None:
            log("NO DIALOG FOUND")
            return
        edits = dlg.findChildren(QLineEdit)
        log(f"dialog fields: {len(edits)}")
        edits[0].setText(f"127.0.0.1:{HOST_FWD_PORT}")   # IP:端口
        edits[1].setText(EMU_NICK)                        # 备注名
        app.processEvents()
        ok_btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "添加")
        ok_btn.click()
        driven["done"] = True
        driven["ok"] = True

    QTimer.singleShot(300, drive_dialog)
    add_btn.click()  # opens the modal dialog (exec)
    app.processEvents()
    if not driven["ok"]:
        raise RuntimeError("add-member dialog flow did not complete")
    log("dialog submitted")

    # The Android side parks the first-contact request in its message box
    # instead of auto-accepting: accept it on the emulator so the session
    # can come up.
    log("accepting the request in the emulator's request box")
    tap_node_wait(text="接受", timeout=30)
    screenshot("gui_request_accepted")

    # ---- 2) the new row appears; click it to open the chat ----
    log("== waiting for the contact row ==")
    peer_id = None
    deadline = time.time() + 30
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        contacts = vm.direct_contacts_list()
        if contacts:
            peer = contacts[0]
            if peer.id.startswith("ip:") and not peer.ip_address.startswith("127.0.0.1"):
                peer = contacts[-1] if len(contacts) > 1 else peer
            peer_id = peer.id
            break
    if peer_id is None:
        raise RuntimeError("no contact row appeared in the member list")
    log(f"contact row: {peer_id}")

    # the member list rendered rows; click the first one -> DirectChatPage
    app.processEvents()
    first_row = None
    for i in range(win.pages[PAGE_MEMBERS].list.count()):
        first_row = win.pages[PAGE_MEMBERS].list.itemWidget(
            win.pages[PAGE_MEMBERS].list.item(i)
        )
        if first_row is not None:
            break
    if first_row is None:
        raise RuntimeError("member list has no rendered row")
    first_row.mouseReleaseEvent(
        __import__("PyQt6.QtGui", fromlist=["QMouseEvent"]).QMouseEvent(
            __import__("PyQt6.QtCore", fromlist=["QEvent"]).QEvent.Type.MouseButtonRelease,
            __import__("PyQt6.QtCore", fromlist=["QPointF"]).QPointF(5, 5),
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.MouseButton.LeftButton,
            __import__("PyQt6.QtCore", fromlist=["Qt"]).Qt.KeyboardModifier.NoModifier,
        )
    )
    app.processEvents()
    log(f"current page: {win.stack.currentIndex()} (DIRECT={PAGE_DIRECT})")
    if win.stack.currentIndex() != PAGE_DIRECT:
        raise RuntimeError("clicking the row did not open DirectChatPage")

    # ---- 3) header flips to 在线 ----
    page = win.pages[PAGE_DIRECT]
    log("== waiting for 在线 in the chat header ==")
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if page.status_label.text() == "在线":
            break
        time.sleep(0.3)
    if page.status_label.text() != "在线":
        raise RuntimeError(f"chat header never showed 在线 (got {page.status_label.text()!r})")
    log("chat header shows 在线: PASS")
    screenshot("gui_win_online")

    # ---- 4) type + click 发送; Android UI must show the message ----
    log(f"== sending {GUI_MSG} via the chat input ==")
    page.input_edit.setPlainText(GUI_MSG)
    app.processEvents()
    page.send_btn.click()
    app.processEvents()
    if not wait_text(GUI_MSG, 90):
        raise RuntimeError(f"Android UI never showed {GUI_MSG}")
    screenshot("gui_emu_received")
    log(f"Android UI shows {GUI_MSG}: PASS")

    # ---- 5) Android adds Windows by IP and replies ----
    log(f"== Android adds Windows ({GUEST_ALIAS}:{WIN_PORT}) ==")
    log(f"[dbg] Windows chat _peer_id={page._peer_id!r}")
    log(f"[dbg] Windows sessions alive: {[(k, v['alive']) for k, v in vm.direct._sessions.items()]}")
    log(f"[dbg] Windows contacts: {[(c.id, c.ip_address, c.port) for c in vm.direct_contacts_list()]}")
    # Android: go back to the member list first (a previous step left it in
    # the chat), then add Windows by the forwarded address
    back_to_member_list()
    tap_node_wait(desc="添加成员", timeout=20)
    if not wait_text("IP:端口", 10):
        raise RuntimeError("add-member dialog did not appear on Android")
    emu_type_text(0, f"{GUEST_ALIAS}:{WIN_PORT}")
    emu_type_text(1, WIN_NICK)
    tap_node_wait(text="添加", timeout=10)
    time.sleep(2)
    # the manual add's dial-back handshake completes immediately (Windows
    # already knows us), merging the placeholder row into the real contact
    # row — tap by nickname, not the transient placeholder's endpoint
    tap_node_wait(contains=WIN_NICK, timeout=60)
    time.sleep(2)
    # chat input on Android: re-dump and type into the single EditText
    try:
        emu_type_text(0, GUI_REPLY)
    except Exception:
        emu_type_text(0, GUI_REPLY)
    tap_node_wait(desc="发送", timeout=10)
    time.sleep(2)
    log(f"[dbg] after reply: Windows chat _peer_id={page._peer_id!r}")
    log(f"[dbg] after reply: Windows sessions: {[(k, v['alive']) for k, v in vm.direct._sessions.items()]}")
    log(f"[dbg] after reply: Windows contacts: {[(c.id, c.ip_address, c.port) for c in vm.direct_contacts_list()]}")

    # the Windows chat page must render the reply
    log("== waiting for the reply in the Windows chat page ==")
    from localchat.ui.chat_page import MSG_ROLE
    got = None
    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.2)
        texts = []
        for i in range(page.model.rowCount()):
            m = page.model.item(i).data(MSG_ROLE)
            if m is not None and hasattr(m, "content"):
                texts.append(m.content)
        got = next((m for m in texts if m == GUI_REPLY), None)
        if got:
            break
    if not got:
        # dump the Android UI for diagnosis
        try:
            root = __import__("xml.etree.ElementTree", fromlist=["ElementTree"]).ElementTree()
            from win_emu_e2e import dump_ui, nodes as ui_nodes
            texts = [t for t, _, _ in ui_nodes(dump_ui()) if t]
            log(f"[dbg] Android UI texts: {texts[-30:]}")
        except Exception as e:
            log(f"[dbg] Android dump failed: {e}")
        raise RuntimeError(f"Windows chat page never rendered {GUI_REPLY}")
    log(f"Windows GUI chat page shows {GUI_REPLY}: PASS")
    screenshot("gui_win_received_reply")

    log("GUI E2E ALL PASS")
    for _ in range(3):
        app.processEvents()
        time.sleep(0.2)
    vm.shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"GUI E2E FAILED: {e}")
        try:
            screenshot("gui_fail_state")
        except Exception:
            pass
        sys.exit(1)