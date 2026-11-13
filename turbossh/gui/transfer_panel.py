"""The Transfers panel: a table over a :class:`TransferLog` — what is queued,
what is moving (with progress, speed and time), and how each one ended — with a
status filter, retry, and a copyable report."""

from __future__ import annotations

import os

from PyQt5.QtCore import (QAbstractTableModel, QModelIndex, QSortFilterProxyModel,
                          Qt, QTimer, QUrl, pyqtSignal)
from PyQt5.QtGui import QColor, QDesktopServices
from PyQt5.QtWidgets import (QAbstractItemView, QApplication, QComboBox, QFileDialog,
                             QFrame, QHBoxLayout, QHeaderView, QLabel, QMenu,
                             QPushButton, QTableView, QVBoxLayout)

from . import transfer_log as tl

COLUMNS = ("Status", "Name", "Direction", "Size", "Progress", "Speed", "Time",
           "Details")
_STATUS_COLOR = {tl.RUNNING: "#4f9bea", tl.DONE: "#5fd38a", tl.FAILED: "#e25c52",
                 tl.CANCELLED: "#d4b86a", tl.QUEUED: "#8a9aa5"}
_FILTERS = (("All", None), ("Active", tl.ACTIVE), ("Failed", frozenset((tl.FAILED,))),
            ("Done", frozenset((tl.DONE,))))


def _runs(rows):
    """Sorted row numbers -> (first, last) of each consecutive run."""
    runs = []
    for row in rows:
        if runs and row == runs[-1][1] + 1:
            runs[-1][1] = row
        else:
            runs.append([row, row])
    return runs


class TransferTableModel(QAbstractTableModel):
    """Rows are the log's items. ``sync()`` re-reads the log and tells the view
    as little as it can: rows dropped from the history are removed, new ones
    appended, and only the rows that changed (or are running: their speed and
    time move with the clock) are repainted. Repainting every row on every tick
    made the view re-query all of them, several times over with columns sized
    to their contents — 430 ms per 200 ms tick with a full history."""

    def __init__(self, log: tl.TransferLog, parent=None):
        super().__init__(parent)
        self._log = log
        self._rows = log.items()
        self._ids = [i.id for i in self._rows]
        self._rev = log.revision         # changes up to here are on screen

    def rowCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802 - Qt API
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def item_at(self, row: int):
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def data(self, index, role=Qt.DisplayRole):
        item = self.item_at(index.row())
        if item is None:
            return None
        column = index.column()
        if role == Qt.DisplayRole:
            return self._display(item, column)
        if role == Qt.ForegroundRole and column == 0:
            return QColor(_STATUS_COLOR.get(item.status, "#8a9aa5"))
        if role == Qt.ToolTipRole:
            return (f"{tl.DIRECTION_LABELS[item.kind]}\nRemote: {item.remote}\n"
                    f"Local: {item.local}" + (f"\n\n{item.error}" if item.error else ""))
        if role == Qt.TextAlignmentRole and column in (3, 4, 5, 6):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role == Qt.UserRole:
            return item.status
        return None

    def _display(self, item, column):
        now = self._log._clock()
        if column == 0:
            return item.status.capitalize()
        if column == 1:
            return item.name + ("/" if item.is_dir else "")
        if column == 2:
            return tl.DIRECTION_LABELS[item.kind]
        if column == 3:
            return tl.human_size(item.total if item.total is not None else item.moved)
        if column == 4:
            return item.progress_text()
        if column == 5:
            if item.status == tl.RUNNING:
                return tl.human_speed(self._log.speed())
            return tl.human_speed(item.speed(now)) if item.status == tl.DONE else ""
        if column == 6:
            return tl.human_duration(item.elapsed(now))
        if item.error:
            return item.error
        if item.status == tl.RUNNING and item.is_dir and item.current:
            return item.current
        return item.local

    def sync(self) -> None:
        rows = self._log.items()
        ids = [i.id for i in rows]
        if ids != self._ids:
            self._restructure(rows, ids)
        seen, self._rev = self._rev, self._log.revision
        changed = [r for r, item in enumerate(self._rows)
                   if item.rev > seen or item.status == tl.RUNNING]
        last = len(COLUMNS) - 1
        for first, end in _runs(changed):
            self.dataChanged.emit(self.index(first, 0), self.index(end, last))

    def _restructure(self, rows, ids) -> None:
        """Rows were dropped and / or added: remove the dropped ones (bottom up,
        so the row numbers stay valid), then append what is new. Anything else
        (never done by the log today) resets the model."""
        keep = set(ids)
        gone = [r for r, item_id in enumerate(self._ids) if item_id not in keep]
        for first, end in reversed(_runs(gone)):
            self.beginRemoveRows(QModelIndex(), first, end)
            del self._rows[first:end + 1]
            del self._ids[first:end + 1]
            self.endRemoveRows()
        have = len(self._ids)
        if ids[:have] != self._ids:
            self.beginResetModel()
            self._rows, self._ids = rows, ids
            self.endResetModel()
        elif len(ids) > have:
            self.beginInsertRows(QModelIndex(), have, len(ids) - 1)
            self._rows, self._ids = rows, ids
            self.endInsertRows()


class _StatusFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._statuses = None

    def set_statuses(self, statuses) -> None:
        self._statuses = statuses
        self.invalidateFilter()

    def filterAcceptsRow(self, row, parent):  # noqa: N802 - Qt API
        if self._statuses is None:
            return True
        return self.sourceModel().index(row, 0, parent).data(Qt.UserRole) in self._statuses


class TransferPanel(QFrame):
    """``retry_requested`` carries the TransferItems to run again;
    ``cancel_requested`` asks the owner to stop the running transfer and queue."""
    retry_requested = pyqtSignal(list)
    cancel_requested = pyqtSignal()

    def __init__(self, log: tl.TransferLog, parent=None):
        super().__init__(parent)
        self._log = log
        self._seen = -1
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0); lay.setSpacing(2)

        head = QHBoxLayout()
        self.toggle = QPushButton("▾ Transfers"); self.toggle.setProperty("role", "ghost")
        self.toggle.setCheckable(True); self.toggle.setChecked(True)
        self.toggle.toggled.connect(self._set_expanded)
        self.summary = QLabel("No transfers yet")
        self.filter = QComboBox()
        for label, _statuses in _FILTERS:
            self.filter.addItem(label)
        self.filter.currentIndexChanged.connect(self._on_filter)
        self.cancel_btn = QPushButton("Cancel"); self.cancel_btn.setProperty("role", "danger")
        self.cancel_btn.setToolTip("Stop the running transfer and drop the queued ones")
        self.cancel_btn.clicked.connect(self.cancel_requested)
        self.retry_btn = QPushButton("Retry failed"); self.retry_btn.setProperty("role", "ghost")
        self.retry_btn.clicked.connect(self._retry_failed)
        self.clear_btn = QPushButton("Clear finished"); self.clear_btn.setProperty("role", "ghost")
        self.clear_btn.clicked.connect(self._clear_finished)
        self.more_btn = QPushButton("Report ▾"); self.more_btn.setProperty("role", "ghost")
        menu = QMenu(self.more_btn)
        menu.addAction("Copy report", self._copy_report)
        menu.addAction("Save report…", self._save_report)
        self.more_btn.setMenu(menu)
        head.addWidget(self.toggle); head.addWidget(self.summary, 1)
        head.addWidget(self.filter); head.addWidget(self.cancel_btn)
        head.addWidget(self.retry_btn); head.addWidget(self.clear_btn)
        head.addWidget(self.more_btn)
        lay.addLayout(head)

        self.model = TransferTableModel(log, self)
        self._proxy = _StatusFilter(self)
        self._proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self._proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        # Interactive columns, fitted to their contents when the rows are reset
        # or first appear — not ResizeToContents, which re-measures every row
        # each time any cell changes (i.e. several times a second while moving)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setResizeContentsPrecision(64)        # rows measured when fitting
        self.model.modelReset.connect(self._fit_columns)
        self.model.rowsInserted.connect(self._fit_if_first)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda _index: self._open_folder())
        self.table.setMinimumHeight(70)
        lay.addWidget(self.table, 1)

        # The log changes many times a second during a transfer; poll its revision
        # and redraw at most a few times a second, not at all while idle, and
        # not while nobody can see the panel (another tab is in front).
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(200)
        self.refresh()
        self._fit_columns()

    # ---- rendering ----
    def _tick(self) -> None:
        if self.isVisible():
            self.refresh()

    def showEvent(self, event):  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh()                   # catch up with what happened while hidden

    def refresh(self) -> None:
        active = self._log.active
        if self._log.revision == self._seen and not active:
            return
        self._seen = self._log.revision
        if not self.table.isHidden():    # collapsed: only the summary line shows
            self.model.sync()
        stats = self._log.stats()
        self.summary.setText(stats.summary())
        self.cancel_btn.setEnabled(stats.active)
        self.retry_btn.setEnabled(bool(stats.failed or stats.cancelled))
        self.clear_btn.setEnabled(bool(stats.done or stats.failed or stats.cancelled))

    def _fit_columns(self) -> None:
        self.table.resizeColumnsToContents()

    def _fit_if_first(self, _parent, first, _last) -> None:
        if first == 0:                   # the table was empty until now
            self._fit_columns()

    def _set_expanded(self, shown: bool) -> None:
        self.toggle.setText(("▾" if shown else "▸") + " Transfers")
        for widget in (self.table, self.filter, self.cancel_btn, self.retry_btn,
                       self.clear_btn, self.more_btn):
            widget.setVisible(shown)
        if shown:
            self._seen = -1              # the table was not kept up to date
            self.refresh()

    def _on_filter(self, index: int) -> None:
        self._proxy.set_statuses(_FILTERS[index][1])

    # ---- actions ----
    def selected_items(self) -> list:
        rows = sorted({self._proxy.mapToSource(i).row()
                       for i in self.table.selectionModel().selectedRows()})
        return [item for item in (self.model.item_at(r) for r in rows) if item]

    def _retry_failed(self) -> None:
        items = self._log.retryable()
        if items:
            self.retry_requested.emit(items)

    def _retry_selected(self) -> None:
        items = self._log.retryable([i.id for i in self.selected_items()])
        if items:
            self.retry_requested.emit(items)

    def _clear_finished(self) -> None:
        self._log.clear_finished()
        self.refresh()

    def _remove_selected(self) -> None:
        self._log.remove([i.id for i in self.selected_items()])
        self.refresh()

    def _copy_report(self) -> None:
        QApplication.clipboard().setText(self._log.report())

    def _save_report(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save transfer report",
                                              "transfers.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self._log.report())
            self.summary.setText(f"Report saved to {path}")
        except OSError as exc:
            self.summary.setText(f"Could not save the report: {exc}")

    def _open_folder(self) -> None:
        items = self.selected_items()
        if items:
            folder = tl.local_folder(items[0])
            if os.path.isdir(folder):
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def _context_menu(self, pos) -> None:
        items = self.selected_items()
        if not items:
            return
        menu = QMenu(self)
        if self._log.retryable([i.id for i in items]):
            menu.addAction("Retry", self._retry_selected)
        menu.addAction("Open local folder", self._open_folder)
        menu.addAction("Copy remote path", lambda: QApplication.clipboard().setText(
            "\n".join(i.remote for i in items)))
        if any(i.finished for i in items):
            menu.addSeparator()
            menu.addAction("Remove from list", self._remove_selected)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def close_panel(self) -> None:
        self._timer.stop()
