"""Minimal repro on the USER's real data dir: add 192.168.0.191, watch list."""
import os
import sys
import time

sys.path.insert(0, r"C:\MyOutput\LocalP2PChat\windows")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication
from localchat.storage import ChatStore
from localchat.view_model import ChatViewModel

app = QApplication([])
DB = r"C:\MyOutput\LocalP2PChat\windows\data\localchat.db"
DATA = r"C:\MyOutput\LocalP2PChat\windows\data"

store = ChatStore(DB)
vm = ChatViewModel(store, data_dir=DATA)

print("== before ==")
print("contacts:", [(c.id, c.name, c.ip_address, c.port) for c in vm.direct_contacts_list()])
print("my id:", vm.direct.my_id_value)

events = []
vm.direct_contacts_signal.connect(lambda: events.append("contacts_changed"))
vm.status_message.connect(lambda t: events.append("toast:" + t))

print("\n== add 192.168.0.191 ==")
ok = vm.add_direct_contact("192.168.0.191", "")
app.processEvents()
time.sleep(0.5)
app.processEvents()
print("add ->", ok)
print("events:", events)
print("\n== after ==")
print("contacts:", [(c.id, c.name, c.ip_address, c.port) for c in vm.direct_contacts_list()])
print("sessions:", {k: v["alive"] for k, v in vm.direct._sessions.items()})

for _ in range(3):
    app.processEvents()
    time.sleep(0.2)
vm.shutdown()
print("DONE")