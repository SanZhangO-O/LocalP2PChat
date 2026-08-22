"""EMPTY-list GUI repro: fresh data dir (no contacts), add 192.168.0.191 via
the real MainWindow dialog, watch the member list update end-to-end."""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, r"C:\MyOutput\LocalP2PChat\windows")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from localchat.storage import ChatStore
from localchat.view_model import ChatViewModel
from localchat.ui.main_window import MainWindow, PAGE_MEMBERS

tmp = os.path.join(tempfile.gettempdir(), "lc_empty_repro")
if os.path.exists(tmp):
    shutil.rmtree(tmp)
os.makedirs(tmp)

app = QApplication([])
store = ChatStore(os.path.join(tmp, "a.db"))
vm = ChatViewModel(store, data_dir=tmp)
win = MainWindow(vm)
win.show()
app.processEvents()

lst = win.pages[PAGE_MEMBERS].list
empty = win.pages[PAGE_MEMBERS].empty_label
print("BEFORE: rowCount=", lst.count(), "list_visible=", lst.isVisible(),
      "empty_visible=", empty.isVisible())
print("empty text:", empty.text()[:60])

driven = {"ok": False}

def drive():
    dlg = next((w for w in QApplication.topLevelWidgets()
                if isinstance(w, QDialog) and w.isVisible()), None)
    if dlg is None:
        print("NO DIALOG"); return
    edits = dlg.findChildren(QLineEdit)
    print("dialog fields:", len(edits))
    edits[0].setText("192.168.0.191")
    edits[1].setText("")
    app.processEvents()
    ok_btn = next(b for b in dlg.findChildren(QPushButton) if b.text() == "添加")
    ok_btn.click()
    driven["ok"] = True

QTimer.singleShot(400, drive)
win.pages[PAGE_MEMBERS].add_btn.click()
app.processEvents()
print("dialog driven:", driven["ok"])

deadline = time.time() + 12
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.2)

print("\nAFTER dialog:")
print("rowCount=", lst.count(), "list_visible=", lst.isVisible(),
      "empty_visible=", empty.isVisible())
print("contacts:", [(c.id, c.name, c.ip_address, c.port) for c in vm.direct_contacts_list()])
print("sessions:", {k: v["alive"] for k, v in vm.direct._sessions.items()})
for i in range(lst.count()):
    w = lst.itemWidget(lst.item(i))
    labels = [x.text() for x in w.findChildren(type(w).mro() and object) if hasattr(x, "text")]
    print("  row", i, ":", labels)

for _ in range(3):
    app.processEvents()
    time.sleep(0.2)
vm.shutdown()
print("DONE")