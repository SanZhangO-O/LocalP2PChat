"""Drive the REAL MainWindow add-member flow on the user's data dir."""
import os
import sys
import time

sys.path.insert(0, r"C:\MyOutput\LocalP2PChat\windows")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from localchat.storage import ChatStore
from localchat.view_model import ChatViewModel
from localchat.ui.main_window import MainWindow, PAGE_MEMBERS

app = QApplication([])
DB = r"C:\MyOutput\LocalP2PChat\windows\data\localchat.db"
DATA = r"C:\MyOutput\LocalP2PChat\windows\data"

store = ChatStore(DB)
vm = ChatViewModel(store, data_dir=DATA)
win = MainWindow(vm)
win.show()
app.processEvents()

print("== member list BEFORE ==")
lst = win.pages[PAGE_MEMBERS].list
print("rowCount:", lst.count())
for i in range(lst.count()):
    w = lst.itemWidget(lst.item(i))
    print("  row:", w and w.findChildren(type(w).mro()[2]) and "widget" or "?", getattr(w, "findChildren", lambda *a: []) and [x.text() for x in w.findChildren(type(w).__mro__[1] if False else object) if hasattr(x, "text")])

driven = {"ok": False}

def drive():
    dlg = next((w for w in QApplication.topLevelWidgets()
                if isinstance(w, QDialog) and w.isVisible()), None)
    if dlg is None:
        print("NO DIALOG")
        return
    edits = dlg.findChildren(QLineEdit)
    print("dialog fields:", len(edits))
    edits[0].setText("192.168.0.191")
    edits[1].setText("MyPhone")
    app.processEvents()
    ok_btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "添加")
    ok_btn.click()
    driven["ok"] = True

QTimer.singleShot(400, drive)
win.pages[PAGE_MEMBERS].add_btn.click()  # opens modal dialog
app.processEvents()
print("dialog driven:", driven["ok"])

# wait for the contact row to appear / session
deadline = time.time() + 15
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.2)
    if vm.direct_contacts_list():
        break

print("\n== member list AFTER ==")
print("rowCount:", lst.count())
print("contacts:", [(c.id, c.name, c.ip_address, c.port) for c in vm.direct_contacts_list()])
print("sessions:", {k: v["alive"] for k, v in vm.direct._sessions.items()})
print("empty label visible:", win.pages[PAGE_MEMBERS].empty_label.isVisible())
print("list visible:", lst.isVisible())

for _ in range(3):
    app.processEvents()
    time.sleep(0.2)
vm.shutdown()
print("DONE")