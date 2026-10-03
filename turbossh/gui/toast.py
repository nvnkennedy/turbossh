"""Toasts: short, typed notifications that appear in the bottom-right corner of
the main window and go away by themselves.

They replace modal boxes for news that needs no answer (connected, transfer
done, connection lost). With several sessions open, a modal box per event
stacked dialogs the user had to click away one by one; a toast informs and gets
out of the way. Errors stay longer, and a click dismisses any toast early.
"""

from __future__ import annotations

from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel

# kind -> (text colour, background, seconds on screen)
KINDS = {
    "info":  ("#ffffff", "#1f6feb", 4.0),
    "ok":    ("#ffffff", "#238636", 4.0),
    "warn":  ("#241a00", "#d29922", 7.0),
    "error": ("#ffffff", "#cf222e", 10.0),
}
_MAX_VISIBLE = 4
_MARGIN = 14
_MAX_WIDTH = 460


class _Toast(QFrame):
    def __init__(self, host, text, kind):
        super().__init__(host.window)
        self._host = host
        self.kind = kind if kind in KINDS else "info"
        fg, bg, _seconds = KINDS[self.kind]
        self.setObjectName("toast")
        self.setStyleSheet(f"QFrame#toast {{ background:{bg}; border-radius:8px; }}"
                           f"QLabel {{ color:{fg}; background:transparent; }}")
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to dismiss")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setMaximumWidth(_MAX_WIDTH)
        lay.addWidget(self.label)

    def mousePressEvent(self, event):
        self._host.dismiss(self)


class ToastHost(QObject):
    """Owns the toasts of one window; create it once and call :meth:`show`."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.toasts: list = []
        window.installEventFilter(self)          # keep them in the corner on resize

    def show(self, text: str, kind: str = "info"):
        if not text:
            return None
        toast = _Toast(self, text, kind)
        self.toasts.append(toast)
        while len(self.toasts) > _MAX_VISIBLE:   # the oldest makes room
            self.dismiss(self.toasts[0])
        toast.adjustSize()
        toast.show()
        toast.raise_()
        self._layout()
        seconds = KINDS[toast.kind][2]
        QTimer.singleShot(int(seconds * 1000), lambda t=toast: self.dismiss(t))
        return toast

    def dismiss(self, toast) -> None:
        if toast in self.toasts:
            self.toasts.remove(toast)
            toast.hide()
            toast.deleteLater()
            self._layout()

    def clear(self) -> None:
        for toast in list(self.toasts):
            self.dismiss(toast)

    def _layout(self) -> None:
        """Stack them upwards from the bottom-right corner, above the status bar."""
        window = self.window
        bottom = window.height() - _MARGIN
        try:
            bar = window.statusBar()
            if bar is not None and bar.isVisible():
                bottom -= bar.height()
        except Exception:
            pass
        for toast in reversed(self.toasts):
            toast.adjustSize()
            bottom -= toast.height()
            toast.move(max(_MARGIN, window.width() - toast.width() - _MARGIN), bottom)
            bottom -= 8

    def eventFilter(self, obj, event):
        if obj is self.window and event.type() == QEvent.Resize and self.toasts:
            self._layout()
        return False
