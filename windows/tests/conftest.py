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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import QObject  # noqa: F401  (load QtCore before OpenCV)
except ImportError:
    pass
