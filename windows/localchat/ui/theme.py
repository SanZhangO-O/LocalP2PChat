from PyQt6.QtCore import QRect
from PyQt6.QtGui import QColor, QFont, QPainterPath

PRIMARY = "#6750A4"
PRIMARY_HOVER = "#7965AF"
PRIMARY_PRESSED = "#59418E"
ON_PRIMARY = "#FFFFFF"
PRIMARY_CONTAINER = "#D0BCFF"
ON_PRIMARY_CONTAINER = "#381E72"
SURFACE = "#FFFFFF"
SURFACE_VARIANT = "#E9E7EE"
BACKGROUND = "#F7F6FA"
TEXT = "#1C1B1F"
TEXT_SUBTLE = "#6B6875"
TEXT_FAINT = "#99949E"
ERROR = "#B3261E"
ERROR_CONTAINER = "#F9DEDC"
ON_ERROR_CONTAINER = "#410E0B"
OUTLINE = "#C4C0D0"
BUBBLE_OTHER = "#E9E7EE"
BUBBLE_MINE = "#6750A4"
BUBBLE_TEXT_OTHER = "#1C1B1F"
BUBBLE_NAME = "#49454F"

FONT_FAMILY = "Microsoft YaHei UI"

APP_QSS = f"""
QMainWindow, QDialog {{
    background: {BACKGROUND};
}}
QWidget {{
    color: {TEXT};
    font-family: {FONT_FAMILY};
    font-size: 14px;
}}
QLabel#pageTitle {{
    font-size: 24px;
    font-weight: 600;
    color: {PRIMARY};
}}
QLabel#pageSubtitle {{
    font-size: 13px;
    color: {TEXT_SUBTLE};
}}
QLabel#appTitle {{
    font-size: 30px;
    font-weight: 700;
    color: {PRIMARY};
}}
QLabel#hint {{
    font-size: 13px;
    color: {TEXT_SUBTLE};
}}
QLabel#faint {{
    font-size: 12px;
    color: {TEXT_FAINT};
}}
QLabel#errorLabel {{
    font-size: 13px;
    color: {ERROR};
}}
QLabel#successLabel {{
    font-size: 13px;
    color: #2E7D32;
}}
QPushButton {{
    background: {PRIMARY};
    color: {ON_PRIMARY};
    border: none;
    border-radius: 10px;
    padding: 10px 20px;
    font-size: 15px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {PRIMARY_HOVER};
}}
QPushButton:pressed {{
    background: {PRIMARY_PRESSED};
}}
QPushButton:disabled {{
    background: #C8C4D2;
    color: #FEF7FF;
}}
QPushButton#outline {{
    background: transparent;
    color: {TEXT};
    border: 1px solid {OUTLINE};
}}
QPushButton#outline:hover {{
    background: rgba(103, 80, 164, 0.08);
    border-color: {PRIMARY};
}}
QPushButton#outline:disabled {{
    background: transparent;
    border-color: #E2DFE9;
    color: {TEXT_FAINT};
}}
QPushButton#ghost {{
    background: transparent;
    color: {PRIMARY};
}}
QPushButton#ghost:hover {{
    background: rgba(103, 80, 164, 0.08);
}}
QPushButton#ghost:disabled {{
    background: transparent;
    color: {TEXT_FAINT};
}}
QPushButton#danger {{
    background: transparent;
    color: {ERROR};
}}
QPushButton#danger:hover {{
    background: rgba(179, 38, 30, 0.08);
}}
QPushButton#fab {{
    background: {PRIMARY};
    border-radius: 28px;
    font-size: 26px;
    padding: 0px;
    min-width: 56px;
    max-width: 56px;
    min-height: 56px;
    max-height: 56px;
}}
QPushButton#fab:hover {{
    background: {PRIMARY_HOVER};
}}
QPushButton#fab:disabled {{
    background: #C8C4D2;
}}
QLineEdit {{
    background: {SURFACE};
    border: 1px solid {OUTLINE};
    border-radius: 10px;
    padding: 8px 12px;
    selection-background-color: {PRIMARY};
    selection-color: white;
}}
QLineEdit:focus {{
    border: 2px solid {PRIMARY};
    padding: 7px 11px;
}}
QLineEdit:disabled {{
    background: #F0EFF4;
    color: {TEXT_FAINT};
}}
QTextEdit {{
    background: {SURFACE};
    border: 1px solid {OUTLINE};
    border-radius: 16px;
    padding: 8px 12px;
    selection-background-color: {PRIMARY};
    selection-color: white;
}}
QTextEdit:focus {{
    border: 2px solid {PRIMARY};
    padding: 7px 11px;
}}
QFrame#card {{
    background: {SURFACE_VARIANT};
    border-radius: 12px;
}}
QFrame#infoCard {{
    background: {SURFACE_VARIANT};
    border-radius: 12px;
}}
QFrame#warnCard {{
    background: {ERROR_CONTAINER};
    border-radius: 12px;
}}
QFrame#hostCard {{
    background: {PRIMARY_CONTAINER};
    border-radius: 12px;
}}
QListWidget {{
    background: transparent;
    border: none;
    outline: 0;
}}
QListWidget::item {{
    border: none;
}}
QListWidget::item:hover {{
    background: rgba(103, 80, 164, 0.05);
    border-radius: 12px;
}}
QListView {{
    background: transparent;
    border: none;
    outline: 0;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #CBC7D4;
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: #B3AEC2;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QMenu {{
    background: {SURFACE};
    border: 1px solid {OUTLINE};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 24px 7px 14px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background: rgba(103, 80, 164, 0.12);
}}
QToolTip {{
    background: #323232;
    color: white;
    border: none;
    padding: 4px 8px;
}}
QMessageBox {{
    background: {BACKGROUND};
}}
QDialog#confirmDialog {{
    background: {SURFACE};
}}
"""


def bubble_path(rect: QRect, radius: int, mine: bool) -> QPainterPath:
    r = float(radius)
    tl, tr = r, r
    br = 4.0 if mine else r
    bl = r if mine else 4.0
    x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
    path = QPainterPath()
    path.moveTo(x + tl, y)
    path.lineTo(x + w - tr, y)
    path.quadTo(x + w, y, x + w, y + tr)
    path.lineTo(x + w, y + h - br)
    path.quadTo(x + w, y + h, x + w - br, y + h)
    path.lineTo(x + bl, y + h)
    path.quadTo(x, y + h, x, y + h - bl)
    path.lineTo(x, y + tl)
    path.quadTo(x, y, x + tl, y)
    path.closeSubpath()
    return path


def app_font(weight: int = QFont.Weight.Normal, size: int = 10, pixel: bool = False) -> QFont:
    f = QFont(FONT_FAMILY)
    f.setWeight(weight)
    if pixel:
        f.setPixelSize(size)
    else:
        f.setPointSize(size)
    return f


def avatar_color(seed: str) -> QColor:
    palette = [
        QColor("#D0BCFF"),
        QColor("#A9C7FF"),
        QColor("#B9E4C9"),
        QColor("#FFD8A8"),
        QColor("#F4B8C4"),
        QColor("#BFC6DC"),
        QColor("#DEBBE0"),
    ]
    if not seed:
        return palette[0]
    return palette[abs(hash(seed)) % len(palette)]
