"""A dedicated live-log viewer tab (journalctl / syslog / dmesg / slog2info):
pick or type a follow command, filter by regex, pause, clear, save."""

from __future__ import annotations

import re
import threading
from collections import deque

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import (QColor, QFont, QKeySequence, QSyntaxHighlighter,
                         QTextCharFormat, QTextDocument)
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                             QLineEdit, QPushButton, QPlainTextEdit, QFileDialog,
                             QShortcut, QCheckBox)

from ..results import strip_ansi
from . import qtutil

_PRESETS = ["journalctl", "slog2info", "dmesg", "journalctl -f", "slog2info -w",
            "dmesg -w", "tail -f /var/log/messages"]
# every line received is kept (unfiltered) up to this many, so changing the
# filter re-filters what already arrived instead of only what comes next
_KEEP_LINES = 200000


# Level colouring: the first rule that matches a line decides its colour. The
# view is always dark (like the terminal), so one palette serves every theme.
_LEVEL_RULES = (
    (re.compile(r"\b(fatal|panic|critical|emerg|alert|error|err|fail(ed|ure)?|"
                r"exception|traceback|denied|refused|segfault)\b", re.I), "#ff7a6e"),
    (re.compile(r"\b(warn(ing)?|deprecated|timeout|timed out|retry(ing)?)\b",
                re.I), "#ffc34d"),
    (re.compile(r"\b(debug|trace|verbose)\b", re.I), "#7d8794"),
    (re.compile(r"\b(success(ful)?|passed|started|connected|ready|ok)\b", re.I),
     "#5be39a"),
)


class _LevelHighlighter(QSyntaxHighlighter):
    """Colours each log line by its level. Qt only re-runs it for lines that
    change, so appending to a long log costs one line's worth of work."""

    def __init__(self, document):
        super().__init__(document)
        self._formats = []
        for rx, colour in _LEVEL_RULES:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(colour))
            self._formats.append((rx, fmt))

    def highlightBlock(self, text):  # noqa: N802 - Qt API
        for rx, fmt in self._formats:
            if rx.search(text):
                self.setFormat(0, len(text), fmt)
                return


class _LogThread(QThread):
    """Buffers log lines; the GUI pulls them on a timer so a flood never freezes
    the UI thread."""
    stopped = pyqtSignal()

    def __init__(self, handler, command, stop_event, get_pty=True, cap=200000):
        super().__init__()
        self.handler, self.command, self.stop_event = handler, command, stop_event
        self._get_pty = get_pty
        self._lines = []
        self._lock = threading.Lock()
        self._cap = cap

    def run(self):
        try:
            # get_pty=True: tools like slog2info line-buffer on a TTY but
            # FULLY buffer on a pipe (so nothing shows until ~4 KB fills). A PTY
            # makes them stream line-by-line. Pagers are disabled in the command
            # (SYSTEMD_PAGER/PAGER=cat) so journalctl doesn't wait on `less`.
            for ln in self.handler.iter_lines(self.command, stop_event=self.stop_event,
                                              get_pty=self._get_pty):
                with self._lock:
                    self._lines.append(strip_ansi(ln))
                    if len(self._lines) > self._cap:
                        del self._lines[:-self._cap]
        except Exception as exc:
            with self._lock:
                self._lines.append(f"[ERROR] {exc}")
        self.stopped.emit()

    def pull(self, maxn=3000):
        with self._lock:
            if not self._lines:
                return []
            out = self._lines[:maxn]
            del self._lines[:maxn]
            return out


class LogsTab(QWidget):
    title = "Logs"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.handler = None
        self._thread = None            # NB: not `thread` — that is QObject.thread()
        self.stop_event = threading.Event()
        self._paused = False
        self._lines: deque = deque(maxlen=_KEEP_LINES)
        self._rx = None                # compiled filter, or None = show everything
        self._rx_note = ""

        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.cmd = QComboBox(); self.cmd.setEditable(True); self.cmd.addItems(_PRESETS)
        self.filter = QLineEdit(); self.filter.setPlaceholderText("filter (regex, optional)")
        self.start = QPushButton("Start"); self.start.setProperty("role", "ok")
        self.stop = QPushButton("Stop"); self.stop.setProperty("role", "danger")
        self.pause = QPushButton("Pause"); self.pause.setProperty("role", "ghost")
        self.clear = QPushButton("Clear"); self.clear.setProperty("role", "ghost")
        self.save = QPushButton("Save…"); self.save.setProperty("role", "ghost")
        self.start.clicked.connect(self._start)
        self.stop.clicked.connect(self._stop)
        self.pause.clicked.connect(self._toggle_pause)
        self.clear.clicked.connect(self._clear)
        self.save.clicked.connect(self._save)
        row.addWidget(QLabel("Cmd")); row.addWidget(self.cmd, 2)
        row.addWidget(self.filter, 2)
        row.addWidget(self.start); row.addWidget(self.stop)
        row.addWidget(self.pause); row.addWidget(self.clear); row.addWidget(self.save)

        self.statusrow = QLabel("○ stopped")
        self._count = 0

        self.view = QPlainTextEdit(); self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(500000)
        self.view.setStyleSheet("background:#000000; color:#cfe3f7;")
        self._highlighter = _LevelHighlighter(self.view.document())

        # find-in-log (Ctrl+F): Enter / F3 = next, Shift+Enter / Shift+F3 = previous
        find_row = QHBoxLayout()
        self.find = QLineEdit(); self.find.setPlaceholderText("find in the log (Ctrl+F)")
        self.find.returnPressed.connect(self.find_next)
        self.find.textChanged.connect(lambda _t: self._find_status.setText(""))
        self.find_case = QCheckBox("Match case")
        prev_btn = QPushButton("◀"); prev_btn.setProperty("role", "ghost")
        prev_btn.setToolTip("Previous match (Shift+F3)")
        prev_btn.clicked.connect(self.find_previous)
        next_btn = QPushButton("▶"); next_btn.setProperty("role", "ghost")
        next_btn.setToolTip("Next match (F3)")
        next_btn.clicked.connect(self.find_next)
        self._find_status = QLabel("")
        find_row.addWidget(QLabel("Find")); find_row.addWidget(self.find, 1)
        find_row.addWidget(prev_btn); find_row.addWidget(next_btn)
        find_row.addWidget(self.find_case); find_row.addWidget(self._find_status)

        lay.addLayout(row); lay.addWidget(self.statusrow); lay.addWidget(self.view, 1)
        lay.addLayout(find_row)

        # Scoped to THIS tab. The default (window) context made them fire while
        # typing in another pane's terminal in split view, and Ctrl+S clashed
        # with the File menu's shortcut so neither one fired.
        for seq, slot in (("Ctrl+S", self._save), ("Ctrl+L", self._clear),
                          ("Ctrl+R", self._start), ("Ctrl+F", self._focus_find),
                          ("F3", self.find_next), ("Shift+F3", self.find_previous),
                          ("Shift+Return", self.find_previous)):
            shortcut = QShortcut(QKeySequence(seq), self, activated=slot)
            shortcut.setContext(Qt.WidgetWithChildrenShortcut)

        # drain buffered lines on a timer (bounded work -> never freezes). It
        # only runs while a stream is live — not for every tab ever opened.
        self._timer = QTimer(self); self._timer.timeout.connect(self._drain)
        # re-filter a moment after the user stops typing, not on every keystroke
        self._filter_timer = QTimer(self); self._filter_timer.setSingleShot(True)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.filter.textChanged.connect(lambda _t: self._filter_timer.start(300))

    def set_handler(self, handler):
        self.handler = handler

    # ---- stream control ----
    def _start(self):
        if not self.handler:
            self.statusrow.setText("○ not connected")
            return
        self._stop()
        self._count = 0
        self.stop_event = threading.Event()
        raw = self.cmd.currentText().strip()
        if getattr(self.handler, "known_os", None) == "windows":
            # cmd.exe / PowerShell: run the command as typed. (The POSIX wrapper
            # below is a syntax error there, so nothing could ever be followed
            # on a Windows host.)
            cmd, get_pty = raw, False
        else:
            # The log runs over a NON-login exec channel, which (unlike the
            # terminal's login PTY) has a bare PATH — so QNX tools like slog2info
            # show up as "No such file or directory". Source the login profile to
            # inherit the real PATH, AND prepend the usual QNX/embedded bin dirs
            # as a fallback.
            qnx_path = ("/ifs/bin:/ifs/usr/bin:/ifs/usr/sbin:/ifs/sbin:/proc/boot:"
                        "/system/bin:/system/xbin:/usr/bin:/usr/sbin:/bin:/sbin")
            # disable pagers (we run on a PTY now) so journalctl etc. stream
            # instead of piping into `less` and hanging.
            cmd = (f'. /etc/profile 2>/dev/null; . $HOME/.profile 2>/dev/null; '
                   f'export PATH="$PATH:{qnx_path}"; '
                   f'export SYSTEMD_PAGER=cat PAGER=cat GIT_PAGER=cat; {raw}')
            get_pty = True
        self._thread = _LogThread(self.handler, cmd, self.stop_event, get_pty)
        self._thread.stopped.connect(self._on_stream_ended)
        self._thread.start()
        self._timer.start(80)
        self.statusrow.setText(f"● live — {raw}")

    def _stop(self):
        """Stop the stream WITHOUT waiting for the worker on the GUI thread. It is
        parked: a worker stuck on a dead link finishes on its own later, and its
        QThread is never dropped while still running (which aborts the app)."""
        self.stop_event.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            self._take(thread)              # what it had already buffered
            qtutil.park_thread(thread)
        self._timer.stop()
        self._set_stopped_status()

    def _on_stream_ended(self):
        # the command ended on its own (or failed): collect the tail, then idle
        thread, self._thread = self._thread, None
        if thread is not None:
            self._take(thread)
        self._timer.stop()
        self._set_stopped_status()

    def _set_stopped_status(self):
        self.statusrow.setText(f"○ stopped — {self._count} lines{self._rx_note}")

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause.setText("Resume" if self._paused else "Pause")
        if not self._paused:
            self._render_all()              # show what arrived while paused

    # ---- lines ----
    def _take(self, thread):
        """Move everything the worker has buffered into our store and the view."""
        while True:
            lines = thread.pull(3000)
            if not lines:
                return
            self._accept(lines)

    def _drain(self):
        if self._thread is None:
            return
        lines = self._thread.pull(3000)
        if lines:
            self._accept(lines)

    def _accept(self, lines):
        self._lines.extend(lines)           # kept even when filtered out / paused
        if self._paused:
            return
        if self._rx is not None:
            lines = [ln for ln in lines if self._rx.search(ln)]
        if lines:
            self._count += len(lines)
            self.view.appendPlainText("\n".join(lines))
            if self._thread is not None:
                self.statusrow.setText(f"● live — {self._count} lines{self._rx_note}")

    def _apply_filter(self):
        pattern = self.filter.text().strip()
        self._rx, self._rx_note = None, ""
        if pattern:
            try:
                self._rx = re.compile(pattern)
            except re.error as exc:
                # say so — silently showing everything looked like "no matches
                # were filtered out"
                self._rx_note = f"   ⚠ filter ignored: not a valid regex ({exc})"
        self._render_all()

    def _render_all(self):
        """Re-render the view from every stored line under the current filter."""
        rx = self._rx
        shown = [ln for ln in self._lines if rx.search(ln)] if rx else list(self._lines)
        self._count = len(shown)
        self.view.setPlainText("\n".join(shown))
        self.view.moveCursor(self.view.textCursor().End)
        live = self._thread is not None
        self.statusrow.setText(f"{'● live' if live else '○ stopped'} — "
                               f"{self._count} lines{self._rx_note}")

    # ---- find ----
    def _focus_find(self):
        self.find.setFocus()
        self.find.selectAll()

    def _find(self, backward: bool) -> bool:
        """Select the next / previous occurrence, wrapping round at the ends."""
        needle = self.find.text()
        if not needle:
            return False
        flags = QTextDocument.FindFlags()
        if backward:
            flags |= QTextDocument.FindBackward
        if self.find_case.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        if self.view.find(needle, flags):
            self._find_status.setText("")
            return True
        # not found from here: wrap round once
        cursor = self.view.textCursor()
        cursor.movePosition(cursor.End if backward else cursor.Start)
        self.view.setTextCursor(cursor)
        found = self.view.find(needle, flags)
        self._find_status.setText("wrapped" if found else "not found")
        return found

    def find_next(self) -> bool:
        return self._find(False)

    def find_previous(self) -> bool:
        return self._find(True)

    def _clear(self):
        self._lines.clear()
        self._count = 0
        self.view.clear()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "log.txt")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.view.toPlainText())
            self.statusrow.setText(f"saved {self._count} lines to {path}")
        except OSError as exc:
            self.statusrow.setText(f"⚠ could not save: {exc}")

    def close_tab(self):
        self._stop()
        self._filter_timer.stop()
