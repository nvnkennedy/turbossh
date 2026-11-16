"""Small outline icons for the file browser, drawn with QPainter.

Each glyph is a few strokes on a 24 x 24 grid, scaled to the size asked for, in
a colour looked up when it is painted. One ``QIcon`` so follows a live theme
switch (the app restyles, the buttons repaint, the new colour is used); nothing
is cached per theme. Needs no QtSvg.

Tones: ``"blue"`` (this PC), ``"green"`` (the remote host), ``"accent"``,
``"text"``, ``"dim"``; see :func:`tone_color`.
"""

from __future__ import annotations

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap

from . import theme

# (dark theme, light theme) for each colour tone: light enough to read on the
# dark backgrounds, deep enough on the light ones
_HUES = {
    "blue": ("#7ab0f0", "#2a62a8"),
    "green": ("#7fcf8e", "#297539"),
}


def tone_color(tone: str, name: str = None) -> str:
    """The colour of *tone* in theme *name* (default: the theme in force)."""
    name = name or theme.current_name()
    palette = theme.THEMES.get(name, theme.THEMES["dark"])
    light = theme.is_light(name)
    if tone in _HUES:
        return _HUES[tone][1 if light else 0]
    if tone == "accent":
        return theme.accent_text(name)
    return palette.get(tone, palette["dim"])


def _line(*points):
    return ("line", points)


# name -> strokes: ("line", points) a polyline; ("rect", x, y, w, h, radius);
# ("arc", cx, cy, r, start degrees, span degrees); ("fill", points) a closed
# shape washed with the colour and outlined; ("dot", x, y, r) a filled circle
_GLYPHS = {
    "house": (_line((3, 11), (12, 3.5), (21, 11)),
              _line((5.5, 9.5), (5.5, 20.5), (10, 20.5), (10, 14.5), (14, 14.5),
                    (14, 20.5), (18.5, 20.5), (18.5, 9.5))),
    "monitor": (("rect", 2, 3.5, 20, 13.5, 2), _line((8, 21), (16, 21)),
                _line((12, 17), (12, 21))),
    "download": (_line((3, 15), (3, 19.5), (21, 19.5), (21, 15)),
                 _line((12, 3), (12, 15)), _line((7, 10), (12, 15), (17, 10))),
    "folder": (("fill", ((3, 6), (9.5, 6), (11.5, 8), (21, 8), (21, 19), (3, 19))),),
    "plus": (_line((12, 5), (12, 19)), _line((5, 12), (19, 12))),
    "arrow-up": (_line((12, 20), (12, 4)), _line((5, 11), (12, 4), (19, 11))),
    "arrow-left": (_line((20, 12), (4, 12)), _line((11, 5), (4, 12), (11, 19))),
    "arrow-right": (_line((4, 12), (20, 12)), _line((13, 5), (20, 12), (13, 19))),
    "refresh": (("arc", 12, 12, 8.5, 60, 290), _line((20.5, 3.5), (20.5, 9), (15, 9))),
    "server": (("rect", 3, 3.5, 18, 7, 2), ("rect", 3, 13.5, 18, 7, 2),
               ("dot", 7, 7, 1.3), ("dot", 7, 17, 1.3)),
}


def _paint(painter, rect, name, color):
    """Draw glyph *name* into *rect* (a QRect / QRectF) in *color*."""
    side = min(rect.width(), rect.height())
    if side <= 0:
        return
    scale = side / 24.0
    left = rect.x() + (rect.width() - side) / 2.0
    top = rect.y() + (rect.height() - side) / 2.0

    def at(x, y):
        return QPointF(left + x * scale, top + y * scale)

    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.0, 1.9 * scale))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    for stroke in _GLYPHS.get(name, ()):
        kind = stroke[0]
        if kind == "line":
            points = [at(*p) for p in stroke[1]]
            for a, b in zip(points, points[1:]):
                painter.drawLine(a, b)
        elif kind == "rect":
            _, x, y, w, h, r = stroke
            painter.drawRoundedRect(QRectF(at(x, y), at(x + w, y + h)), r * scale, r * scale)
        elif kind == "arc":
            _, cx, cy, r, start, span = stroke
            box = QRectF(at(cx - r, cy - r), at(cx + r, cy + r))
            painter.drawArc(box, int(start * 16), int(span * 16))
        elif kind == "fill":
            path = QPainterPath(at(*stroke[1][0]))
            for p in stroke[1][1:]:
                path.lineTo(at(*p))
            path.closeSubpath()
            wash = QColor(color)
            wash.setAlphaF(0.28)
            painter.fillPath(path, wash)
            painter.drawPath(path)
        elif kind == "dot":
            _, x, y, r = stroke
            painter.save()
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(at(x, y), r * scale, r * scale)
            painter.restore()
    painter.restore()


class _GlyphEngine(QIconEngine):
    """Paints a glyph in its tone's colour of the moment (disabled: dim)."""

    def __init__(self, name, tone):
        super().__init__()
        self.name, self.tone = name, tone

    def paint(self, painter, rect, mode, state):
        tone = "dim" if mode == QIcon.Disabled else self.tone
        _paint(painter, rect, self.name, tone_color(tone))

    def pixmap(self, size, mode, state):
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        self.paint(painter, pm.rect(), mode, state)
        painter.end()
        return pm

    def clone(self):
        return _GlyphEngine(self.name, self.tone)


def icon(name: str, tone: str = "dim") -> QIcon:
    """Glyph *name* as an icon in *tone*, following theme switches."""
    return QIcon(_GlyphEngine(name, tone))


class _CloseEngine(QIconEngine):
    """The tab close glyph (theme.close_glyph) in the colour of the moment:
    white when hovered (on the red the theme gives a hovered close button)."""

    def paint(self, painter, rect, mode, state):
        side = min(rect.width(), rect.height())
        if side <= 0:
            return
        color = theme.close_color(hover=mode in (QIcon.Active, QIcon.Selected))
        if mode == QIcon.Disabled:
            color = tone_color("dim")
        image = theme.close_glyph(side, color)
        painter.drawImage(rect.x() + (rect.width() - side) // 2,
                          rect.y() + (rect.height() - side) // 2, image)

    def pixmap(self, size, mode, state):
        pm = QPixmap(size)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        self.paint(painter, pm.rect(), mode, state)
        painter.end()
        return pm

    def clone(self):
        return _CloseEngine()


def close_icon() -> QIcon:
    """The close glyph of the tabs, for a close button of our own."""
    return QIcon(_CloseEngine())


def names():
    return tuple(_GLYPHS)
