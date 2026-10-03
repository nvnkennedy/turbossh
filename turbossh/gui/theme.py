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
    return ACCENT_DARK if is_light(name) else ACCENT


LOG_COLORS = {
    "ERROR": "#ff7a6e", "WARNING": "#ffc34d", "WARN": "#ffc34d", "stderr": "#ffb37a",
    "OK": "#5be39a", "SUCCESS": "#5be39a", "INFO": "#cfe3f7", "DEBUG": "#8a8a8a",
}
# darker, saturated variants that stay readable on the LIGHT theme's pale bg
LOG_COLORS_LIGHT = {
    "ERROR": "#c0392b", "WARNING": "#9a6700", "WARN": "#9a6700", "stderr": "#9a6700",
    "OK": "#1e7e45", "SUCCESS": "#1e7e45", "INFO": "#1f2937", "DEBUG": "#6b7280",
}


def log_colors(name: str = "dark") -> dict:
    """Log-level colours for the current theme (light needs darker hues)."""
    return LOG_COLORS_LIGHT if is_light(name) else LOG_COLORS

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
# More palettes on the same ten tokens — so the one stylesheet below renders all
# of them. Each dark theme has a light counterpart the ribbon toggle switches to.
_MIDNIGHT = {
    "win": "#0b1020", "panel": "#111833", "raised": "#18213f",
    "border": "#2a3558", "text": "#e3e9f7", "dim": "#8b96b3",
    "sel": "#22305a", "input": "#0d1429", "tab": "#141c38", "ribbon": "#0c1226",
}
_NORD = {
    "win": "#2e3440", "panel": "#343b49", "raised": "#3b4252",
    "border": "#4c566a", "text": "#eceff4", "dim": "#9aa5b8",
    "sel": "#434c5e", "input": "#2b303b", "tab": "#353c4a", "ribbon": "#2b303b",
}
_NORD_LIGHT = {
    "win": "#e5e9f0", "panel": "#eceff4", "raised": "#f4f6f9",
    "border": "#c2cad8", "text": "#2e3440", "dim": "#5b6577",
    "sel": "#d0dcec", "input": "#f8f9fb", "tab": "#d8dee9", "ribbon": "#d8dee9",
}
_SOLARIZED_DARK = {
    "win": "#002b36", "panel": "#04323d", "raised": "#073642",
    "border": "#1c4b57", "text": "#e4e0cf", "dim": "#839496",
    "sel": "#0d4250", "input": "#00252f", "tab": "#063540", "ribbon": "#00252f",
}
_SOLARIZED_LIGHT = {
    "win": "#eee8d5", "panel": "#f4efdc", "raised": "#fdf6e3",
    "border": "#cfc8b0", "text": "#28424a", "dim": "#657b83",
    "sel": "#dfe5c8", "input": "#fffbf0", "tab": "#e6dfc8", "ribbon": "#e6dfc8",
}
_DRACULA = {
    "win": "#21222c", "panel": "#282a36", "raised": "#303341",
    "border": "#44475a", "text": "#f8f8f2", "dim": "#9aa0bd",
    "sel": "#3a3d4f", "input": "#1e1f29", "tab": "#2b2d3a", "ribbon": "#1e1f29",
}
_PAPER = {
    "win": "#f3f1ec", "panel": "#f8f6f1", "raised": "#fffdf8",
    "border": "#d2cdc1", "text": "#26231d", "dim": "#6b665b",
    "sel": "#e3ecd9", "input": "#ffffff", "tab": "#e9e5dc", "ribbon": "#e9e5dc",
}
THEMES = {
    "dark": _DARK, "light": _LIGHT,
    "midnight": _MIDNIGHT, "paper": _PAPER,
    "nord": _NORD, "nord-light": _NORD_LIGHT,
    "solarized-dark": _SOLARIZED_DARK, "solarized-light": _SOLARIZED_LIGHT,
    "dracula": _DRACULA,
}
THEME_NAMES = list(THEMES)
_LIGHT_THEMES = frozenset(("light", "paper", "nord-light", "solarized-light"))
_PAIRS = {"dark": "light", "midnight": "paper", "nord": "nord-light",
          "solarized-dark": "solarized-light", "dracula": "light"}
_PAIRS.update({light: dark for dark, light in list(_PAIRS.items())
               if dark != "dracula"})


def is_light(name: str) -> bool:
    """Whether a theme has a light background (text / icon / log colours that
    stay readable depend on this, not on the theme's name)."""
    return name in _LIGHT_THEMES


def counterpart(name: str) -> str:
    """The theme the one-click toggle switches to: each dark theme's light
    partner and back."""
    return _PAIRS.get(name, "light" if not is_light(name) else "dark")


def _assets_dir():
    import os
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")


def _light_icon_path() -> str:
    """Generate (cached) a LIGHT-theme variant of the app icon: recolour the dark
    navy tile background to light while keeping the teal gauge / green prompt."""
    import os
    from PyQt5.QtGui import QImage, QColor
    src = os.path.join(_assets_dir(), "icon.png")
    from .. import paths
    cache = paths.state_path("cache", "icon-light.png")
    try:
        if os.path.exists(cache) and os.path.getmtime(cache) >= os.path.getmtime(src):
            return cache
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        img = QImage(src)
        if img.isNull():
            return ""
        img = img.convertToFormat(QImage.Format_ARGB32)
        for y in range(img.height()):
            for x in range(img.width()):
                px = img.pixelColor(x, y)
                a = px.alpha()
                if a < 8:
                    continue
                lum = 0.3 * px.red() + 0.59 * px.green() + 0.11 * px.blue()
                if lum < 92:                      # dark navy tile -> light
                    img.setPixelColor(x, y, QColor(238, 241, 245, a))
        img.save(cache, "PNG")
        return cache
    except Exception:
        return ""


def app_icon(name: str = "dark"):
    """QIcon for the app/taskbar in the given theme — dark uses the original
    artwork, light uses the generated light-background variant."""
    import os
    from PyQt5.QtGui import QIcon
    assets = _assets_dir()
    if is_light(name):
        lp = _light_icon_path()
        if lp:
            return QIcon(lp)
    ic = QIcon()
    for p in (os.path.join(assets, "icon.ico"), os.path.join(assets, "icon.png")):
        if os.path.exists(p):
            ic.addFile(p)
    return ic


# themes whose arrow image was written during this run (stylesheet() is called
# on every theme application; re-drawing and re-saving the PNG each time is waste)
_ARROWS_WRITTEN: set = set()
# rendered emoji icons, keyed by (glyph, colour): menus and ribbons ask for the
# same handful again and again
_ICON_CACHE: dict = {}


def _arrow_image(name: str) -> str:
    """Generate (cached) a filled down-arrow PNG in the theme's text colour and
    return a Qt-stylesheet url() path. CSS border-triangles were rendering as a
    tiny dot; a real image always shows, clearly, in both themes."""
    import os
    from PyQt5.QtCore import Qt, QPointF
    from PyQt5.QtGui import QPixmap, QPainter, QColor, QPolygonF
    c = THEMES.get(name, _DARK)
    try:
        from .. import paths
        cache = paths.state_path("cache")
        path = os.path.join(cache, f"arrow-{name}.png")
        if name in _ARROWS_WRITTEN and os.path.exists(path):
            return path.replace("\\", "/")     # drawn already in this run
        os.makedirs(cache, exist_ok=True)
        pm = QPixmap(16, 16); pm.fill(Qt.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen); p.setBrush(QColor(c["text"]))
        p.drawPolygon(QPolygonF([QPointF(3, 5.5), QPointF(13, 5.5), QPointF(8, 11.5)]))
        p.end()
        pm.save(path, "PNG")
        _ARROWS_WRITTEN.add(name)
        return path.replace("\\", "/")
    except Exception:
        return ""


def stylesheet(name: str = "dark") -> str:
    c = THEMES.get(name, _DARK)
    atext = accent_text(name)        # accent that's readable as text on this bg
    arrow = _arrow_image(name)
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
    QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
        width: 24px; border-left: 1px solid {c['border']};
        border-top-right-radius: 7px; border-bottom-right-radius: 7px; }}
    QComboBox::drop-down:hover {{ background: {c['raised']}; }}
    QComboBox::down-arrow {{ image: url('{arrow}'); width: 13px; height: 13px; }}
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
        background: {c['tab']}; color: {c['dim']}; padding: 3px 12px; min-width: 90px;
        border: 1px solid {c['border']}; border-bottom: none;
        border-top-left-radius: 7px; border-top-right-radius: 7px; margin-right: 2px;
    }}
    QTabBar::tab:selected {{ background: {c['panel']}; color: {atext}; font-weight: 700; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QTabBar::close-button {{ subcontrol-position: right; }}
    QTabBar::scroller {{ width: 28px; }}
    QSplitter::handle {{ background: {c['border']}; }}
    QSplitter::handle:hover {{ background: {ACCENT}; }}
    QSplitter::handle:horizontal {{ width: 6px; }}
    QSplitter::handle:vertical {{ height: 6px; }}
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


def _icon_pen_color(color):
    """Default tint for monochrome glyphs: an accent that's readable on the CURRENT
    theme's background (cyan-on-light was nearly invisible)."""
    if color:
        return color
    try:
        from . import settings as _s
        return accent_text(_s.get("theme") or "dark")
    except Exception:
        return ACCENT


def emoji_icon(ch: str, color: str = None):
    """Render an emoji/symbol to a QIcon. Colour emojis paint themselves; plain
    symbol glyphs (⚙ ⬆ ⏻ …) draw in the pen colour, which now follows the theme so
    they stay visible in light mode too."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QPixmap, QPainter, QFont, QIcon, QColor
    pen = _icon_pen_color(color)
    cached = _ICON_CACHE.get((ch, pen))
    if cached is not None:
        return cached
    pm = QPixmap(28, 28)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setPen(QColor(pen))
    p.setFont(QFont("Segoe UI Emoji", 14))
    p.drawText(pm.rect(), Qt.AlignCenter, ch)
    p.end()
    icon = _ICON_CACHE[(ch, pen)] = QIcon(pm)
    return icon


def split_icon(color: str = None):
    """A clear 'split view' icon: two side-by-side panes (the 🔲 emoji looked like
    a blank patch)."""
    from PyQt5.QtCore import Qt, QRectF
    from PyQt5.QtGui import QPixmap, QPainter, QIcon, QColor, QPen
    pm = QPixmap(28, 28); pm.fill(Qt.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(_icon_pen_color(color))); pen.setWidth(2); p.setPen(pen)
    p.drawRoundedRect(QRectF(4.5, 6.5, 7.5, 15), 2, 2)
    p.drawRoundedRect(QRectF(16, 6.5, 7.5, 15), 2, 2)
    p.end()
    return QIcon(pm)


def server_icon(color: str = None):
    """A clear 'SSH server' icon — a small rack with status LEDs (the globe emoji
    looked wrong)."""
    from PyQt5.QtCore import Qt, QRectF
    from PyQt5.QtGui import QPixmap, QPainter, QIcon, QColor, QPen
    pm = QPixmap(28, 28); pm.fill(Qt.transparent)
    p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
    col = QColor(_icon_pen_color(color))
    pen = QPen(col); pen.setWidth(2); p.setPen(pen)
    p.drawRoundedRect(QRectF(6, 5, 16, 7.5), 2, 2)        # upper rack unit
    p.drawRoundedRect(QRectF(6, 15.5, 16, 7.5), 2, 2)     # lower rack unit
    p.setPen(Qt.NoPen); p.setBrush(col)
    p.drawEllipse(QRectF(9, 7.7, 2.2, 2.2))               # LEDs
    p.drawEllipse(QRectF(9, 18.2, 2.2, 2.2))
    p.end()
    return QIcon(pm)
