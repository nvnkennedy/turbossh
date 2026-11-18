"""Check boxes and radio buttons that show, in a host application's look.

Embedded with ``theme="inherit"``, TurboSSH applies no stylesheet: it takes
the host's look. Some styles draw a radio button's ring and a check box's box
from the palette in a colour close to a dark background — Qt's Fusion with a
dark palette drew no ring at all, only the dot of the checked one.

So TurboSSH's own check boxes and radio buttons get :class:`IndicatorStyle`,
a proxy style that draws just those two marks, from the widget's palette at
paint time (so a host switching between dark and light is followed), and
leaves everything else to the base style. No stylesheet is involved: a
stylesheet on TurboSSH's window would cut its widgets off from the palette
and font the host gives its window. Where the host's stylesheet styles
``::indicator`` itself, Qt still draws the host's rule.
"""

from __future__ import annotations

import weakref

from PyQt5.QtCore import QEvent, QObject, QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPalette, QPen
from PyQt5.QtWidgets import (QApplication, QCheckBox, QProxyStyle, QRadioButton,
                             QStyle, QStyleFactory)

from . import theme

try:
    from PyQt5 import sip
except ImportError:                      # PyQt5 < 5.11 ships it on its own
    import sip

_MARK = "turbossh_indicator"   # property set on a widget given the style
_STYLES = {}                   # base style key -> IndicatorStyle (owned by the app)
_WINDOWS = {}                  # id -> TurboSSH window that keeps the host's look
_filter = None                 # the application's event filter (one)...
_installed = False             # ...installed while a window needs it
_WATCHED = "turbossh_indicator_window"   # property: its destroyed is connected
# the styles that draw these marks from the palette (dark: no ring); a native
# style, or the host's own (a proxy, Breeze…), is left as it is
_PALETTE_STYLES = ("Fusion", "Windows")


def _behind(widget=None) -> QColor:
    """What a see-through background lies on: the first solid background of
    the widget's parents (a host may be dark by its stylesheet alone, its
    application palette left light), else the application's window colour."""
    current = widget.parentWidget() if widget is not None else None
    while current is not None:
        brush = current.palette().brush(QPalette.Active, current.backgroundRole())
        if brush.style() == Qt.SolidPattern and brush.color().alpha() == 255:
            return QColor(brush.color())
        if current.isWindow():           # a dialog does not lie on its owner
            break
        current = current.parentWidget()
    app = QApplication.instance()
    colour = app.palette().color(QPalette.Window) if app is not None else QColor("#808080")
    colour.setAlpha(255)
    return colour


def _surface(palette, role, group=QPalette.Active, widget=None):
    """``(opaque name, colour to fill with or None)`` of a background role. A
    host stylesheet's ``background:`` ends up here: a see-through colour is
    judged as it looks over the window (and filled as it is, see-through); a
    gradient or picture is judged as the window colour and not filled over."""
    brush = palette.brush(group, role)
    if brush.style() != Qt.SolidPattern:
        return _behind(widget).name(), None
    colour = QColor(brush.color())
    alpha = colour.alphaF()
    if alpha >= 1.0:
        return colour.name(), colour
    under = _behind(widget)
    seen = QColor(round(colour.red() * alpha + under.red() * (1 - alpha)),
                  round(colour.green() * alpha + under.green() * (1 - alpha)),
                  round(colour.blue() * alpha + under.blue() * (1 - alpha)))
    return seen.name(), colour


def indicator_colors(palette, widget=None) -> dict:
    """The colours of the marks, from *palette*: an edge that stands out from
    both the field and the window (else the text colour, else black or
    white), the accent and a readable colour on it, and muted ones for
    disabled marks. ``base_fill`` / ``window_fill``: what to fill an empty
    mark with (None: nothing, the background shows). *widget*: the one
    painted, whose parents say what a see-through background lies on."""
    def color(role, group=QPalette.Active):
        return palette.color(group, role).name()
    base, base_fill = _surface(palette, QPalette.Base, widget=widget)
    window, window_fill = _surface(palette, QPalette.Window, widget=widget)
    text = color(QPalette.Text)
    accent, on_accent = color(QPalette.Highlight), color(QPalette.HighlightedText)
    def stands_out(colour):
        return min(theme.contrast(colour, window), theme.contrast(colour, base)) >= 3.0

    def black_or_white(against):
        return ("#ffffff" if theme.contrast("#ffffff", against)
                > theme.contrast("#000000", against) else "#000000")
    edge = theme._mix(base, text, 0.6)
    if not stands_out(edge):
        edge = text if stands_out(text) else black_or_white(window)
    if theme.contrast(on_accent, accent) < 3.0:
        on_accent = black_or_white(accent)
    muted = theme._mix(accent, window, 0.5)
    return {"base": base, "text": text, "window": window, "edge": edge,
            "base_fill": base_fill, "window_fill": window_fill,
            "accent": accent, "on_accent": on_accent,
            "dim_edge": theme._mix(base, text, 0.35), "muted": muted,
            "dim_mark": theme._mix(color(QPalette.Text, QPalette.Disabled), muted, 0.3)}


def paint_indicator(painter, option, radio: bool, widget=None) -> None:
    """Draw a radio button's ring (or a check box's box) into option.rect: a
    ring when off, filled with the accent and marked when on."""
    c = indicator_colors(option.palette, widget)
    state = option.state
    enabled = bool(state & QStyle.State_Enabled)
    on = bool(state & QStyle.State_On)
    partial = bool(state & QStyle.State_NoChange) and not radio
    if not enabled:
        edge, mark = c["dim_edge"], c["dim_mark"]
        fill = QColor(c["muted"]) if on or partial else c["window_fill"]
        if on or partial:
            edge = c["muted"]
    elif on or partial:
        edge, fill, mark = c["accent"], QColor(c["accent"]), c["on_accent"]
    else:
        edge = c["accent"] if state & QStyle.State_MouseOver else c["edge"]
        fill, mark = c["base_fill"], c["on_accent"]
    rect = QRectF(option.rect)
    side = min(rect.width(), rect.height())
    if side <= 2:
        return
    width = max(1.5, side / 8.0)
    box = QRectF(rect.center().x() - side / 2.0, rect.center().y() - side / 2.0, side, side)
    box = box.adjusted(width / 2.0, width / 2.0, -width / 2.0, -width / 2.0)
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(edge))
    pen.setWidthF(width)
    painter.setPen(pen)
    painter.setBrush(fill if fill is not None else Qt.NoBrush)
    if radio:
        painter.drawEllipse(box)
        if on:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(mark))
            painter.drawEllipse(box.center(), side * 0.2, side * 0.2)
    else:
        painter.drawRoundedRect(box, side * 0.18, side * 0.18)
        mark_pen = QPen(QColor(mark))
        mark_pen.setWidthF(max(1.5, side / 7.0))
        mark_pen.setCapStyle(Qt.RoundCap)
        mark_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(mark_pen)
        x, y, s = box.x(), box.y(), box.width()
        if partial:
            painter.drawLine(QPointF(x + s * 0.25, y + s * 0.5), QPointF(x + s * 0.75, y + s * 0.5))
        elif on:
            painter.drawPolyline(QPointF(x + s * 0.2, y + s * 0.52),
                                 QPointF(x + s * 0.42, y + s * 0.74),
                                 QPointF(x + s * 0.8, y + s * 0.28))
    painter.restore()


class IndicatorStyle(QProxyStyle):
    """The base style, but radio buttons' rings and check boxes' boxes are
    drawn by :func:`paint_indicator`."""

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802 - Qt API
        if element == QStyle.PE_IndicatorRadioButton:
            paint_indicator(painter, option, radio=True, widget=widget)
        elif element == QStyle.PE_IndicatorCheckBox:
            paint_indicator(painter, option, radio=False, widget=widget)
        else:
            super().drawPrimitive(element, option, painter, widget)


def _base_key() -> str:
    """The key of the style to draw the rest with, or "" to leave the widget
    alone: only a style that draws these marks from the palette needs the
    help. Behind an application stylesheet, the style it wraps (its child)
    decides; Fusion stands in if it cannot be found."""
    style = QApplication.style()
    name = style.metaObject().className()
    if name == "QStyleSheetStyle":
        inner = next((c for c in style.children() if isinstance(c, QStyle)), None)
        if inner is None:
            return "Fusion"
        name = inner.metaObject().className()
    for key in _PALETTE_STYLES:
        if name.lower() == f"q{key.lower()}style":
            return key
    return ""


def _style(key) -> IndicatorStyle:
    style = _STYLES.get(key)
    if style is None or sip.isdeleted(style):            # (a new application)
        style = IndicatorStyle(QStyleFactory.create(key))     # it owns its base
        style.setParent(QApplication.instance())              # the app owns it
        _STYLES[key] = style
    return style


def _ours(widget) -> bool:
    """Is *widget* inside a TurboSSH window that keeps the host's look — its
    dialogs too, which are windows of their own with that window as parent?"""
    current = widget
    while current is not None:
        if id(current) in _WINDOWS:
            return True
        current = current.parentWidget()
    return False


def _give(widget, again=False) -> None:
    """Give *widget* the style (*again*: anew, the application's look changed:
    the base, and the stylesheet that wraps it, are worked out afresh) — or
    take it back where the application's style needs no help now. A widget
    given a style of its own by someone else is left as it is."""
    marked = bool(widget.property(_MARK))
    if marked and not again:
        return
    if not marked and widget.testAttribute(Qt.WA_SetStyle):
        return                           # the host styled it: theirs
    key = _base_key()
    if key:
        widget.setProperty(_MARK, True)
        widget.setStyle(_style(key))
    elif marked:
        widget.setProperty(_MARK, None)
        widget.setStyle(None)


def _restyle(window) -> None:
    for widget in window.findChildren((QRadioButton, QCheckBox)):
        if widget.property(_MARK) or not widget.testAttribute(Qt.WA_SetStyle):
            _give(widget, again=True)


class _Polisher(QObject):
    """Gives each check box and radio button of those windows the style as
    it is first shown (Qt polishes a widget once, before that), and again
    when the application's style or stylesheet changes (Qt leaves a widget
    with a style of its own out of a new application stylesheet)."""

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API
        kind = event.type()
        if (kind in (QEvent.Polish, QEvent.ParentChange)
                and isinstance(obj, (QRadioButton, QCheckBox))
                and not obj.property(_MARK) and _ours(obj)):
            _give(obj)                   # (ParentChange: polished before it came)
        elif kind == QEvent.StyleChange and id(obj) in _WINDOWS:
            _restyle(obj)
        return False


def _forget(key) -> None:
    """A window that kept the host's look is gone (or has its own look now):
    the application's filter is taken out once no such window is left (and
    kept, for the next one)."""
    global _installed
    _WINDOWS.pop(key, None)
    if not _WINDOWS and _installed:
        app = QApplication.instance()
        if app is not None and _filter is not None and not sip.isdeleted(_filter):
            app.removeEventFilter(_filter)
        _installed = False


def _gone(key, ref) -> None:
    """A watched window was destroyed: forget it — if the entry under its id is
    still that window (after release() the id may name another one now)."""
    window = ref()
    if window is not None and _WINDOWS.get(key) is window:
        _forget(key)


def follow(window) -> None:
    """*window* keeps the host's look: its check boxes and radio buttons, and
    those of its dialogs, get :class:`IndicatorStyle` — the ones there now,
    and the ones to come."""
    global _filter, _installed
    app = QApplication.instance()
    if app is None:
        return
    if _filter is None or sip.isdeleted(_filter):        # (or a new application)
        _filter, _installed = _Polisher(app), False
    if not _installed:
        app.installEventFilter(_filter)
        _installed = True
    key = id(window)
    _WINDOWS[key] = window
    if not window.property(_WATCHED):                     # once per window
        window.setProperty(_WATCHED, True)
        ref = weakref.ref(window)
        window.destroyed.connect(lambda _obj=None, k=key, r=ref: _gone(k, r))
    _restyle(window)


def release(window) -> None:
    """*window* is shut down: stop watching for it (its widgets keep the
    style until they go). Safe for a window Qt has deleted already."""
    _forget(id(window))


def leave(window) -> None:
    """*window* has a look of its own again (a TurboSSH theme): its check
    boxes and radio buttons go back to the application's style."""
    if id(window) not in _WINDOWS:
        return
    _forget(id(window))
    for widget in window.findChildren((QRadioButton, QCheckBox)):
        if widget.property(_MARK):
            widget.setProperty(_MARK, None)
            widget.setStyle(None)
