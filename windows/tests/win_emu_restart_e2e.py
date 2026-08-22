#!/usr/bin/env python3
"""Restart persistence check: a contact added by IP survives a Windows app
restart, and presence re-dials it automatically (no re-add, no chat open).

Flow:
  1. Windows instance #1: add the emulator by IP (127.0.0.1:10099), wait for
     the handshake (real id learned), send a message, then SHUT DOWN.
  2. Windows instance #2 (same data dir): restore contacts, wait for presence
     to re-establish the session, and confirm the old message + a new one.
"""
import os
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WINDOWS = os.path.join(REPO, "windows")
sys.path.insert(0, WINDOWS)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from localchat.storage import ChatStore
from localchat.view_model import ChatViewModel

HOST_FWD_PORT = int(os.environ.get("HOST_FWD_PORT", "10099"))
WIN_PORT = 9999
EMU_NICK = "EmuPhone"
MSG1 = "persist-msg-before-restart"
MSG2 = "persist-msg-after-restart"


def pump(app, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.1)


def make_vm(store, data_dir, app):
    vm = ChatViewModel(store, data_dir=data_dir)
    toasts = []
    vm.status_message.connect(lambda t: toasts.append(t) or print("[toast]", t, flush=True))
    return vm


def main() -> int:
    tmp = os.path.join(tempfile.gettempdir(), "lc_win_emu_restart")
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
    store.set_setting("nickname", "WinPC")

    app = QApplication([])

    # ---------- instance #1: add by IP, verify, shut down ----------
    print("== instance #1: add by IP ==", flush=True)
    vm1 = make_vm(store, data_dir, app)
    print("win id:", vm1.direct.my_id_value, flush=True)
    ok = vm1.add_direct_contact(f"127.0.0.1:{HOST_FWD_PORT}", EMU_NICK)
    print("add ->", ok, flush=True)
    real = None
    deadline = time.time() + 60
    while time.time() < deadline:
        pump(app, 0.2)
        real = next((c.id for c in vm1.direct_contacts_list()
                     if not c.id.startswith("ip:") and c.id != vm1.direct.my_id_value), None)
        if real and vm1.direct_chat_alive(real):
            break
    if not real:
        raise RuntimeError("instance #1 never learned the real id")
    print("real id:", real, "alive:", vm1.direct_chat_alive(real), flush=True)
    vm1.send_direct_message(real, MSG1)
    pump(app, 1)
    print("sent", MSG1, flush=True)
    print("saved contacts before shutdown:", [(c.id, c.ip_address, c.port) for c in vm1.direct_contacts_list()], flush=True)
    pump(app, 0.5)
    vm1.shutdown()
    pump(app, 0.5)
    print("instance #1 shut down", flush=True)

    # ---------- instance #2: same store, presence re-dials ----------
    print("== instance #2: restart, presence re-dial ==", flush=True)
    # a fresh ChatStore over the SAME db file (the old one closed it on
    # shutdown, matching a real process restart)
    store2 = ChatStore(db)
    vm2 = make_vm(store2, data_dir, app)
    contacts = vm2.direct_contacts_list()
    print("restored contacts:", [(c.id, c.ip_address, c.port) for c in contacts], flush=True)
    if not contacts:
        raise RuntimeError("no contacts restored after restart")
    if not any(c.id == real for c in contacts):
        raise RuntimeError("real-id contact not persisted")
    # presence announce should re-establish without opening any chat
    alive = False
    deadline = time.time() + 90
    while time.time() < deadline:
        pump(app, 0.3)
        if vm2.direct_chat_alive(real):
            alive = True
            break
    if not alive:
        raise RuntimeError("presence did not re-dial after restart")
    print("presence re-dialed, session alive: PASS", flush=True)
    # the pre-restart message must be in history, and a new message delivers
    msgs = vm2.direct_messages(real)
    print("history contents:", [m.content for m in msgs], flush=True)
    if not any(m.content == MSG1 for m in msgs):
        raise RuntimeError("pre-restart message missing from history")
    ok2 = vm2.send_direct_message(real, MSG2)
    print("send", MSG2, "->", ok2, flush=True)
    delivered = False
    deadline = time.time() + 60
    while time.time() < deadline:
        pump(app, 0.3)
        pend = [m for m in vm2.direct_messages(real) if m.content == MSG2 and m.pending]
        if not pend and vm2.direct_chat_alive(real):
            delivered = True
            break
    if not delivered:
        raise RuntimeError("post-restart message never delivered")
    print("post-restart message delivered: PASS", flush=True)

    for _ in range(3):
        pump(app, 0.2)
    vm2.shutdown()
    print("RESTART E2E ALL PASS", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"RESTART E2E FAILED: {e}", flush=True)
        sys.exit(1)