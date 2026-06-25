"""Small shared widgets."""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox


class HostCombo(QComboBox):
    """An editable host field with a drop-down of saved machines.

    It deliberately quacks like a ``QLineEdit`` — ``text()`` / ``setText()`` /
    ``setPlaceholderText()`` — so it drops straight into the existing session and
    camera forms with no other changes. Each visible item is the bare host (so
    ``text()`` is always clean); the friendly name, if any, is the tooltip.

    IMPORTANT: populating the drop-down must NEVER change the typed/edited text.
    (Adding the first item to an empty editable combo otherwise auto-selects it,
    which silently pre-filled the SSH Host field with a stale host.)
    """

    def __init__(self, parent=None, *, with_saved: bool = True):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self._by_host: dict[str, dict] = {}
        self._extra: list = []           # extra hosts (e.g. past-session hosts)
        if with_saved:
            self.reload()
        self.setCurrentIndex(-1)
        self.setEditText("")

    def reload(self):
        """(Re)load the saved-machine list from settings, keeping current text and
        any extra hosts. Called on popup too, so 'Manage machines' edits show up
        immediately in every open dropdown."""
        from . import settings as _s
        cur = self.currentText()
        self.blockSignals(True)
        self.clear()
        self._by_host = {}
        seen = set()
        for m in _s.machines():
            host = m["host"]
            self._by_host[host] = m
            self.addItem(host); seen.add(host)
            if m["name"]:
                self.setItemData(self.count() - 1, f'{m["name"]} — {host}',
                                 Qt.ToolTipRole)
        for h in self._extra:            # re-apply previously-added extra hosts
            if h and h not in seen:
                self.addItem(h); seen.add(h)
        self.setCurrentIndex(-1)         # don't let item 0 hijack the editor…
        self.setEditText(cur)            # …keep whatever was typed
        self.blockSignals(False)

    def add_extra(self, hosts):
        """Append extra hosts (e.g. from previously-saved sessions) to the drop-down
        WITHOUT altering the current text."""
        cur = self.currentText()
        existing = {self.itemText(i) for i in range(self.count())}
        self.blockSignals(True)
        for h in hosts:
            h = (h or "").strip()
            if h and h not in existing:
                self.addItem(h); existing.add(h)
                if h not in self._extra:
                    self._extra.append(h)
        self.setCurrentIndex(-1)
        self.setEditText(cur)
        self.blockSignals(False)

    def showPopup(self):
        # refresh from settings first so machines added via 'Manage machines…'
        # appear right away, without reopening the dialog.
        try:
            self.reload()
        except Exception:
            pass
        super().showPopup()

    def machine_for(self, host):
        """The saved-machine record for *host*, or None."""
        return self._by_host.get((host or "").strip())

    # --- QLineEdit-ish API so this is a drop-in replacement -----------------
    def text(self) -> str:
        return self.currentText().strip()

    def setText(self, value):
        self.setEditText(value or "")

    def setPlaceholderText(self, value):
        le = self.lineEdit()
        if le is not None:
            le.setPlaceholderText(value or "")
