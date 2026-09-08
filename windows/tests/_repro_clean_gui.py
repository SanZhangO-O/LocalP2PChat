"""Clean-state repro on a COPY of the user's data: add 192.168.0.191 via the
real GUI, capture every intermediate state. Does NOT touch the user's data."""
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

# fresh copy of the user's data dir (backup of the polluted state)
USER = r"C:\MyOutput\LocalP2PChat\windows\data.bak_user"  # = user's ORIGINAL data
tmp = os.path.join(tempfile.gettempdir(), "lc_clean_repro")
if os.path.exists(tmp):
    shutil.rmtree(tmp)
os.makedirs(tmp)
shutil.copy(os.path.join(USER, "localchat.db"), os.path.join(tmp, "localchat.db"))
if os.path.exists(os.path.join(USER, "identity.json")):
    shutil.copy(os.path.join(USER, "identity.json"), os.path.join(tmp, "identity.json"))

app = QApplication([])
store = ChatStore(os.path.join(tmp, "localchat.db"))
vm = ChatViewModel(store, data_dir=tmp)

print("== user's ORIGINAL contacts BEFORE ==")
print(vm.direct_contacts_list(), [(c.id, c.name, c.ip_address, c.port) for c in vm.direct_contacts_list()])

win = MainWindow(vm)
win.show()
app.processEvents()

lst = win.pages[PAGE_MEMBERS].list
print("member list rowCount BEFORE:", lst.count())

driven = {"ok": False, "toast": None}

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

toasts = []
vm.status_message.connect(lambda t: toasts.append(t))

QTimer.singleShot(400, drive)
win.pages[PAGE_MEMBERS].add_btn.click()
app.processEvents()
print("dialog driven:", driven["ok"])

deadline = time.time() + 10
while time.time() < deadline:
    app.processEvents()
    time.sleep(0.2)

print("rowCount AFTER:", lst.count())
print("contacts:", [(c.id, c.name, c.ip_address, c.port) for c in vm.direct_contacts_list()])
print("toasts:", toasts)
print("list visible:", lst.isVisible(), "empty visible:", win.pages[PAGE_MEMBERS].empty_label.isVisible())

for _ in range(3):
    app.processEvents()
    time.sleep(0.2)
vm.shutdown()
print("DONE")