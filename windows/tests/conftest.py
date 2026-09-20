"""Shared pytest setup.

Pre-load PyQt6 (QtCore) BEFORE localchat.call is imported so OpenCV's bundled
DLLs cannot shadow Qt6Core: call.py imports cv2 before PyQt at module top, but
the running app imports PyQt first (QApplication in main.py). Under pytest the
first import of call.py is cv2-first, which on Windows breaks Qt's DLL
resolution. Imports in the same order the app uses to keep the media tests
runnable here (the app never hits this because it builds QApplication first).

The import is best-effort: the Qt-free suites (protocol/direct/mesh/numeric/
sponsor) must still be collectable on machines without PyQt6.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QObject  # noqa: F401  (load QtCore before OpenCV)
except ImportError:
    pass


def _reset_group_identity_bindings():
    """Drop the process-run group TOFU state between test cases.

    localchat.groupauth keeps two pieces of state across a real app run
    (by design): the "sender already signed, so unsigned is now rejected"
    upgrade set and the persisted DeviceIdentity bindings under
    "group|<groupId>|<senderId>". Tests reuse the same small set of group and
    sender ids, so a binding/flag left by one case would reject the next
    case's unsigned traffic (exactly the seq-style enforcement working as
    intended, but not in isolation). Clear both around every case.
    """
    try:
        from localchat import groupauth
        from localchat.securewire import DeviceIdentity
    except Exception:
        return
    groupauth.reset_enforcement_for_tests()
    with DeviceIdentity._lock:
        for key in [k for k in DeviceIdentity._peers if k.startswith("group|")]:
            DeviceIdentity._peers.pop(key, None)


@pytest.fixture(autouse=True)
def _isolate_group_identity():
    _reset_group_identity_bindings()
    yield
    _reset_group_identity_bindings()
