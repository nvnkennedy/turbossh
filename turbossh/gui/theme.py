"""TurboSSH themes — a clean BLACK dark theme (default, neutral grays, no blue
tint) and a soft, not-glaring light theme. Applied at the QApplication level so
every window and dialog is styled. Accent is a clean cyan-teal with an
automotive coral redline. Styling matches TurboADB for a consistent toolkit."""

from __future__ import annotations

# brand accents (clean cyan-teal + speedometer amber/coral redline)
ACCENT = "#28c2d6"        # cyan-teal (clean, not greenish)
ACCENT_2 = "#48d6e8"      # brighter cyan
ACCENT_DARK = "#0c7a88"   # darker cyan — readable as TEXT on a light background
DANGER = "#ff6b5e"        # coral/redline
WARN = "#ffc34d"          # amber
TERM_BG = "#000000"       # true black terminal


def accent_text(name: str = "dark") -> str:
    """Accent colour to use for TEXT (group titles, section labels…). The bright
    cyan is fine on dark, but needs a darker shade to be readable on light."""
    return ACCENT if name != "light" else ACCENT_DARK


LOG_COLORS = {
    "ERROR": "#ff7a6e", "WARNING": "#ffc34d", "stderr": "#ffb37a",
    "OK": "#5be39a", "INFO": "#cfe3f7",
}

# dark = near-black, neutral grays (no blue tint), cyan accent
_DARK = {
    "win": "#0a0a0a", "panel": "#131313", "raised": "#1b1b1b",
    "border": "#2c2c2c", "text": "#e8e8e8", "dim": "#8a8a8a",
    "sel": "#262626", "input": "#0e0e0e", "tab": "#161616", "ribbon": "#0c0c0c",
}
# light = soft, not glaring
_LIGHT = {
    "win": "#dcdfe2", "panel": "#e7eaed", "raised": "#f2f4f6",
    "border": "#bcc3cb", "text": "#1d2329", "dim": "#5a636c",
    "sel": "#cdeadd", "input": "#f7f9fb", "tab": "#d4d9de", "ribbon": "#d4d9de",
}
THEMES = {"dark": _DARK, "light": _LIGHT}


def stylesheet(name: str = "dark") -> str:
    c = THEMES.get(name, _DARK)
    atext = accent_text(name)        # accent that's readable as text on this bg
    return f"""
    QWidget {{ background: {c['win']}; color: {c['text']}; font-size: 9.5pt; }}
    QMainWindow::separator {{ background: {c['border']}; width: 1px; height: 1px; }}
    QToolBar {{
        background: {c['ribbon']}; border: none;
        border-bottom: 1px solid {c['border']}; spacing: 3px; padding: 5px;
    }}
    QToolButton {{
        background: transparent; border: 1px solid transparent; border-radius: 8px;
        padding: 5px 12px; color: {c['text']};
    }}
    QToolButton:hover {{ background: {c['raised']}; border: 1px solid {c['border']}; }}
    QToolButton:pressed {{ background: {ACCENT}; color: #042830; }}
    QToolButton[role="ok"] {{ background: {ACCENT}; color: #042830; border-radius: 8px;
        padding: 6px 12px; font-weight: 700; }}
    QToolButton[role="ok"]:hover {{ background: {ACCENT_2}; border: 1px solid {ACCENT_2}; }}
    QToolButton[role="ghost"] {{ background: {c['raised']}; color: {c['text']};
        border: 1px solid {c['border']}; border-radius: 8px; padding: 6px 12px; font-weight: 600; }}
    QToolButton[role="ghost"]:hover {{ border: 1px solid {ACCENT}; }}
    QToolButton[role="danger"] {{ background: {DANGER}; color: white; border-radius: 8px;
        padding: 6px 12px; font-weight: 700; }}
    QToolButton[role="ok"]:disabled, QToolButton[role="ghost"]:disabled {{
        background: {c['border']}; color: {c['dim']}; border-color: {c['border']}; }}
    QToolButton::menu-indicator {{ subcontrol-position: right center;
        subcontrol-origin: padding; right: 5px; }}
    QDockWidget {{ color: {c['dim']}; }}
    QDockWidget::title {{
        background: {c['ribbon']}; padding: 6px 10px;
        border-bottom: 1px solid {c['border']}; font-weight: 600;
    }}
    QListWidget, QTreeWidget {{
        background: {c['raised']}; border: 1px solid {c['border']}; border-radius: 8px;
        outline: 0;
    }}
    QListWidget::item {{ padding: 5px 8px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {ACCENT}; color: #042830; }}
    QTableWidget, QTableView {{
        background: {c['raised']}; alternate-background-color: {c['panel']};
        color: {c['text']}; gridline-color: {c['border']};
        border: 1px solid {c['border']}; border-radius: 8px; outline: 0;
    }}
    QTableWidget::item, QTableView::item {{ padding: 4px 6px; }}
    QTableWidget::item:selected, QTableView::item:selected {{
        background: {ACCENT}; color: #042830; }}
    QHeaderView::section {{
        background: {c['ribbon']}; color: {atext}; padding: 6px 8px; border: none;
        border-right: 1px solid {c['border']}; border-bottom: 1px solid {c['border']};
        font-weight: 700;
    }}
    QTableCornerButton::section {{ background: {c['ribbon']}; border: none; }}
    QLineEdit, QSpinBox, QComboBox {{
        background: {c['input']}; border: 1px solid {c['border']}; border-radius: 7px;
        padding: 6px 9px; color: {c['text']}; selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
        background: {c['panel']}; color: {c['dim']}; border-color: {c['border']};
    }}
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right;
        border: none; width: 20px; }}
    QComboBox::down-arrow {{ width: 0; height: 0; margin-right: 7px;
        border-left: 5px solid transparent; border-right: 5px solid transparent;
        border-top: 6px solid {atext}; }}
    QComboBox QAbstractItemView, QListView {{
        background: {c['raised']}; color: {c['text']}; border: 1px solid {c['border']};
        selection-background-color: {ACCENT}; selection-color: #042830; outline: 0;
    }}
    QComboBox QAbstractItemView::item {{ min-height: 22px; padding: 2px 6px; color: {c['text']}; }}
    QLabel {{ background: transparent; color: {c['dim']}; }}
    QPushButton {{
        background: {ACCENT}; color: #042830; border: none; border-radius: 7px;
        padding: 7px 16px; font-weight: 700;
    }}
    QPushButton:hover {{ background: {ACCENT_2}; }}
    QPushButton:disabled {{ background: {c['border']}; color: {c['dim']}; }}
    QPushButton[role="ok"] {{ background: {ACCENT_2}; }}
    QPushButton[role="danger"] {{ background: {DANGER}; color: white; }}
    QPushButton[role="ghost"] {{
        background: {c['raised']}; color: {c['text']}; border: 1px solid {c['border']};
        font-weight: 600;
    }}
    QPushButton[role="ghost"]:hover {{ border-color: {ACCENT}; background: {c['sel']}; }}
    QPushButton[role="ghost"]:pressed {{ background: {ACCENT}; color: #042830; }}
    QGroupBox {{
        background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px;
        margin-top: 13px; padding: 10px; font-weight: 600;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {atext}; }}
    QTabWidget::pane {{ border: 1px solid {c['border']}; background: {c['panel']}; border-radius: 6px; }}
    QTabBar::tab {{
        background: {c['tab']}; color: {c['dim']}; padding: 7px 16px;
        border: 1px solid {c['border']}; border-bottom: none;
        border-top-left-radius: 8px; border-top-right-radius: 8px; margin-right: 3px;
    }}
    QTabBar::tab:selected {{ background: {c['panel']}; color: {atext}; font-weight: 700; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QCheckBox, QRadioButton {{ background: transparent; color: {c['text']}; spacing: 7px; }}
    QRadioButton {{ padding-right: 14px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 17px; height: 17px;
        border: 2px solid {c['dim']}; background: {c['input']}; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 9px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {ACCENT}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT};
        image: none; }}
    QRadioButton::indicator:checked {{ background: {ACCENT}; border: 4px solid {c['input']};
        outline: 2px solid {ACCENT}; }}
    QStatusBar {{ background: {c['ribbon']}; color: {c['dim']}; border-top: 1px solid {c['border']}; }}
    QProgressBar {{ border: 1px solid {c['border']}; border-radius: 6px; background: {c['input']};
        text-align: center; color: {c['text']}; height: 14px; }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
    QScrollBar:vertical {{ background: {c['panel']}; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QScrollBar:horizontal {{ background: {c['panel']}; height: 11px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 5px; min-width: 24px; }}
    QMenuBar {{ background: {c['ribbon']}; color: {c['text']};
        border-bottom: 1px solid {c['border']}; }}
    QMenuBar::item {{ background: transparent; padding: 5px 11px; }}
    QMenuBar::item:selected {{ background: {c['raised']}; color: {atext}; border-radius: 5px; }}
    QMenuBar::item:pressed {{ background: {ACCENT}; color: #042830; border-radius: 5px; }}
    QMenu {{ background: {c['raised']}; border: 1px solid {c['border']}; }}
    QMenu::item {{ padding: 5px 22px 5px 14px; }}
    QMenu::item:selected {{ background: {ACCENT}; color: #042830; }}
    QToolTip {{ background: {c['raised']}; color: {c['text']}; border: 1px solid {ACCENT};
        padding: 4px 7px; }}
    QDialog {{ background: {c['win']}; }}
    """


def attach_eye(line_edit):
    """Add a show/hide-password eye toggle inside a QLineEdit."""
    from PyQt5.QtWidgets import QLineEdit
    act = line_edit.addAction(emoji_icon("👁"), QLineEdit.TrailingPosition)
    act.setToolTip("Show / hide password")

    def _toggle():
        normal = line_edit.echoMode() == QLineEdit.Normal
        line_edit.setEchoMode(QLineEdit.Password if normal else QLineEdit.Normal)
    act.triggered.connect(_toggle)
    return act


def emoji_icon(ch: str, color: str = None):
    """Render an emoji/symbol to a QIcon. Color emojis paint themselves; plain
    symbol glyphs (⚙ ⟳ ⏻ …) would otherwise draw in black and vanish on the dark
    ribbon. Pass color (hex) to tint monochrome glyphs (e.g. a red power symbol
    for Exit); otherwise they default to the accent colour."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QFont, QIcon, QColor
    pm = QPixmap(28, 28)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setPen(QColor(color or ACCENT))
    p.setFont(QFont("Segoe UI Emoji", 14))
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    return QIcon(pm)
