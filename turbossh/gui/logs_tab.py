"""A dedicated live-log viewer tab (journalctl / syslog / dmesg / slog2info):
pick or type a follow command, filter by regex, pause, clear, save."""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import (QColor, QFont, QKeySequence, QSyntaxHighlighter,
                         QTextCharFormat, QTextCursor, QTextDocument)
from PyQt5.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
                             QLineEdit, QPushButton, QPlainTextEdit, QFileDialog,
                             QShortcut, QCheckBox)

from ..results import strip_ansi
from . import qtutil

_PRESETS = ["journalctl", "slog2info", "dmesg", "journalctl -f", "slog2info -w",
            "dmesg -w", "tail -f /var/log/messages"]
# every line received is kept (unfiltered) up to this many, so changing the
# filter re-filters what already arrived instead of only what comes next, and
# Save writes them all
_KEEP_LINES = 200000
# ...but the view holds the latest this many: a line in a QPlainTextEdit costs
# ~0.6 KB (the view's old 500k-line cap was ~315 MB per tab) and ~25 us to lay
# out and colour, so the view is built a timed step at a time (_STEP_BUDGET of
# GUI time per tick), never all at once
_VIEW_LINES = 50000
_PULL_LINES = 20000         # lines taken from the worker per tick (filtering is cheap)
_SCAN_LINES = 20000         # stored lines re-filtered per step
_CHUNK = 500                # lines added to the view per call
_STEP_BUDGET = 0.015
_TICK_MS = 50
# how long a Save result stays in the status row before the live line count may
# take it back (it was re-set every tick, so the result showed for < 50 ms)
_NOTE_SECONDS = 6.0


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


class _Marker(str):
    """A line TurboSSH adds itself (output was skipped): shown under any filter."""


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
    the UI thread. Past *cap* undrained lines the oldest are dropped — and
    counted, so the view says so instead of silently leaving a gap."""
    stopped = pyqtSignal()

    def __init__(self, handler, command, stop_event, get_pty=True, cap=200000):
        super().__init__()
        self.handler, self.command, self.stop_event = handler, command, stop_event
        self._get_pty = get_pty
        self._lines: deque = deque()
        self._lock = threading.Lock()
        self._cap = cap
        self._dropped = 0

    def _add(self, line: str):
        with self._lock:
            if len(self._lines) >= self._cap:
                self._lines.popleft()
                self._dropped += 1
            self._lines.append(line)

    def run(self):
        try:
            # get_pty=True: tools like slog2info line-buffer on a TTY but
            # FULLY buffer on a pipe (so nothing shows until ~4 KB fills). A PTY
            # makes them stream line-by-line. Pagers are disabled in the command
            # (SYSTEMD_PAGER/PAGER=cat) so journalctl doesn't wait on `less`.
            for ln in self.handler.iter_lines(self.command, stop_event=self.stop_event,
                                              get_pty=self._get_pty):
                self._add(strip_ansi(ln))
        except Exception as exc:
            self._add(f"[ERROR] {exc}")
        self.stopped.emit()

    def pull(self, maxn=3000) -> list:
        """Up to *maxn* buffered lines (removed), led by a marker line if lines
        were dropped since the last pull."""
        with self._lock:
            out = []
            if self._dropped:
                out.append(_Marker(f"[… {self._dropped} lines skipped: they arrived "
                                   f"faster than this tab could take them …]"))
                self._dropped = 0
            for _ in range(min(maxn, len(self._lines))):
                out.append(self._lines.popleft())
            return out


class LogsTab(QWidget):
    title = "Logs"
    _save_done = pyqtSignal(str)            # status text, from the save worker
    _find_done = pyqtSignal(int, int)       # search id, matches: from the find worker

    def __init__(self, parent=None):
        super().__init__(parent)
        self.handler = None
        self._thread = None            # NB: not `thread` — that is QObject.thread()
        self._sources: list = []       # stopped workers still holding lines, oldest first
        self.stop_event = threading.Event()
        self._paused = False
        self._lines: deque = deque(maxlen=_KEEP_LINES)   # everything received
        # the view is fed from these, a step per tick: new lines go below what is
        # shown, a re-filter's older lines above it (newest first)
        self._pending: deque = deque(maxlen=_VIEW_LINES)
        self._older: deque = deque()   # lists of lines, the newest list first
        self._older_count = 0
        self._scan = None              # re-filter in progress: [snapshot, end index]
        self._view_reset = False       # the pending lines replace the whole view
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
        self._save_done.connect(self._show_note)
        self._save_gate = qtutil.SignalGate(self._save_done)
        # while a note (a Save result) is up, the automatic status updates wait
        self._note_timer = QTimer(self); self._note_timer.setSingleShot(True)
        self._note_timer.timeout.connect(self._note_expired)
        self._status_stale = False      # an update waited: show it when the note goes

        self.view = QPlainTextEdit(); self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 9))
        self.view.setMaximumBlockCount(_VIEW_LINES)
        self.view.setStyleSheet("background:#000000; color:#cfe3f7;")
        self._highlighter = _LevelHighlighter(self.view.document())

        # find-in-log (Ctrl+F): Enter / F3 = next, Shift+Enter / Shift+F3 = previous
        find_row = QHBoxLayout()
        self.find = QLineEdit(); self.find.setPlaceholderText("find in the log (Ctrl+F)")
        self.find.returnPressed.connect(self.find_next)
        self.find.textChanged.connect(lambda _t: self._reset_find())
        self.find_case = QCheckBox("Match case")
        self.find_case.toggled.connect(lambda _on: self._reset_find())
        prev_btn = QPushButton("◀"); prev_btn.setProperty("role", "ghost")
        prev_btn.setToolTip("Previous match (Shift+F3)")
        prev_btn.clicked.connect(self.find_previous)
        next_btn = QPushButton("▶"); next_btn.setProperty("role", "ghost")
        next_btn.setToolTip("Next match (F3)")
        next_btn.clicked.connect(self.find_next)
        self._find_status = QLabel("")
        self._find_status.setToolTip(
            f"Find searches the view, which holds the latest {_VIEW_LINES:,} lines; "
            f"without a match there it looks in the older lines kept (up to "
            f"{_KEEP_LINES:,}, what Save writes)")
        # Find searches the view (the latest _VIEW_LINES lines); a miss there is
        # looked for in the older lines kept, and this shows them
        self._find_show = QPushButton("Filter for it"); self._find_show.setProperty("role", "ghost")
        self._find_show.setToolTip("Show only the lines with it (replaces the filter)")
        self._find_show.clicked.connect(self._filter_for_find)
        self._find_show.hide()
        self._find_seq = 0              # bumped per search: a late answer is dropped
        self._find_done.connect(self._on_find_done)
        self._find_gate = qtutil.SignalGate(self._find_done)
        find_row.addWidget(QLabel("Find")); find_row.addWidget(self.find, 1)
        find_row.addWidget(prev_btn); find_row.addWidget(next_btn)
        find_row.addWidget(self.find_case); find_row.addWidget(self._find_status)
        find_row.addWidget(self._find_show)

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

        # drain buffered lines and build the view on a timer (bounded work ->
        # never freezes). It only runs while there is something to do — not for
        # every tab ever opened.
        self._timer = QTimer(self); self._timer.timeout.connect(self._on_timer)
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
        self._reset_find()
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
        self._timer.start(_TICK_MS)
        self.statusrow.setText(f"● live — {raw}")

    def _stop(self):
        """Stop the stream WITHOUT waiting for the worker on the GUI thread. It is
        parked: a worker stuck on a dead link finishes on its own later, and its
        QThread is never dropped while still running (which aborts the app).
        What it had already buffered is still taken, a step per tick."""
        self.stop_event.set()
        self._retire_thread()
        self._show_status()

    def _on_stream_ended(self):
        # The command ended on its own (or failed). run() emits `stopped` as its
        # LAST step, so the thread is usually still running here: dropping the
        # only reference to it then destroyed a running QThread, and Qt aborted
        # the whole app — after every one-shot preset (journalctl, dmesg …).
        self._retire_thread()
        self._show_status(auto=True)

    def _retire_thread(self):
        thread, self._thread = self._thread, None
        if thread is not None:
            qtutil.park_thread(thread)      # kept alive until it really ends
            self._sources.append(thread)    # its buffered tail is still taken
            self._on_timer()
            self._kick()

    def _toggle_pause(self):
        self._paused = not self._paused
        self.pause.setText("Resume" if self._paused else "Pause")
        if not self._paused:
            self._render_all()              # show what arrived while paused

    # ---- lines ----
    def _on_timer(self):
        self._drain()
        self._work()
        if self._thread is None and not self._busy():
            self._timer.stop()

    def _drain(self):
        """Take what the workers buffered (bounded per tick), oldest first."""
        budget = _PULL_LINES
        shown = self._count
        sources = self._sources + ([self._thread] if self._thread is not None else [])
        for source in sources:
            lines = source.pull(budget)
            if lines:
                self._store(lines)
            if len(lines) < budget and source is not self._thread:
                self._sources.remove(source)   # a stopped worker, now emptied
            budget -= len(lines)
            if budget <= 0:
                break
        # only when lines were shown: re-set every tick, it wiped a Save result.
        # Also once the worker has stopped: what it had left (a one-shot
        # command's backlog) is drained after that and must be counted too.
        if self._count != shown:
            self._show_status(auto=True)

    def _accept(self, lines):
        """Take *lines* as if they had just arrived."""
        self._store(lines)
        self._work()
        self._kick()

    def _store(self, lines):
        self._lines.extend(lines)           # kept even when filtered out / paused
        if self._paused:
            return
        shown = self._matching(lines)
        if shown:
            self._count += len(shown)
            if len(self._pending) + len(shown) >= _VIEW_LINES:
                # these alone push everything shown now out of the view: start
                # it afresh rather than lay out lines that are dropped at once
                self._view_reset = True
                self._older.clear()
                self._older_count = 0
            self._pending.extend(shown)     # (bounded: the oldest fall off)

    def _matching(self, lines):
        rx = self._rx
        if rx is None:
            return list(lines)
        return [ln for ln in lines if type(ln) is _Marker or rx.search(ln)]

    def _busy(self) -> bool:
        return bool(self._sources or self._pending or self._older
                    or self._scan is not None or self._view_reset)

    def _kick(self):
        if not self._timer.isActive() and (self._thread is not None or self._busy()):
            self._timer.start(_TICK_MS)

    def _apply_filter(self):
        pattern = self.filter.text().strip()
        self._reset_find()                   # a search still running is for the old view
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
        """Re-show the stored lines under the current filter. Re-filtering and
        laying out up to 200k lines at once froze the window for 6-12 s, so it
        is done in timed steps: the newest lines first (the bottom of the view,
        where the user looks), then older ones above them."""
        self.view.clear()
        self._pending.clear()
        self._older.clear()
        self._older_count = 0
        self._view_reset = False
        self._count = 0
        self._scan = [list(self._lines), len(self._lines)] if self._lines else None
        self._work()
        self._kick()
        self._show_status()

    def _show_status(self, auto: bool = False):
        """Live / stopped and the line count. *auto*: an update nobody asked for
        (lines came in, the stream ended, a re-filter finished) — it waits while
        a note is up, and is shown when the note goes."""
        if auto and self._note_timer.isActive():
            self._status_stale = True
            return
        self._note_timer.stop()
        self._status_stale = False
        live = self._thread is not None
        self.statusrow.setText(f"{'● live' if live else '○ stopped'} — "
                               f"{self._count} lines{self._rx_note}")

    def _show_note(self, text: str):
        """A Save result: it stays up _NOTE_SECONDS, and after that until the
        status has something new to say — a failed save is not wiped unseen."""
        self.statusrow.setText(text)
        self._status_stale = False
        self._note_timer.start(int(_NOTE_SECONDS * 1000))

    def _note_expired(self):
        if self._status_stale:
            self._show_status()

    def _work(self):
        """One step of view work, within _STEP_BUDGET of GUI time: new lines
        first, then a re-filter's older lines, then more re-filtering — so the
        newest matches show at once and the rest fills in above them."""
        deadline = time.perf_counter() + _STEP_BUDGET
        if self._view_reset:
            self.view.clear()
            self._view_reset = False
        while time.perf_counter() < deadline:
            if self._pending:
                n = min(_CHUNK, len(self._pending))
                self.view.appendPlainText("\n".join([self._pending.popleft()
                                                     for _ in range(n)]))
            elif self._older:
                self._insert_older()
            elif self._scan is not None:
                self._scan_step()
            else:
                break

    def _scan_step(self):
        snapshot, end = self._scan
        start = max(0, end - _SCAN_LINES)
        matched = self._matching(snapshot[start:end])
        self._count += len(matched)
        room = _VIEW_LINES - self._older_count - len(self._pending)
        if matched and room > 0:
            matched = matched[-room:]
            self._older.append(matched)           # older than all before it
            self._older_count += len(matched)
        self._scan[1] = start
        if start == 0:
            self._scan = None
            self._show_status(auto=True)

    def _insert_older(self):
        room = _VIEW_LINES - self.view.document().blockCount()
        if room <= 0:                             # the view is full: drop the rest
            self._older.clear()
            self._older_count = 0
            return
        newest = self._older[0]
        n = min(_CHUNK, len(newest), room)
        piece = newest[-n:]
        del newest[-n:]
        if not newest:
            self._older.popleft()
        self._older_count -= n
        self._insert_above(piece)

    def _insert_above(self, lines):
        doc = self.view.document()
        if doc.isEmpty():
            self.view.appendPlainText("\n".join(lines))
            return
        bar = self.view.verticalScrollBar()
        following = bar.value() >= bar.maximum()
        value = bar.value()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.Start)
        cursor.insertText("\n".join(lines) + "\n")
        # stay at the bottom, or keep the lines the user scrolled to in view
        bar.setValue(bar.maximum() if following else value + len(lines))

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
        self._reset_find()
        if self.view.find(needle, flags):
            return True
        # not found from here: wrap round once
        cursor = self.view.textCursor()
        cursor.movePosition(cursor.End if backward else cursor.Start)
        self.view.setTextCursor(cursor)
        if self.view.find(needle, flags):
            self._find_status.setText("wrapped")
            return True
        self._find_older(needle, self.find_case.isChecked())
        return False

    def _find_older(self, needle: str, case: bool):
        """Not in the view, which holds only the latest _VIEW_LINES lines: look
        in every stored line that passes the filter (what Save writes) — an
        error 60k lines back was "not found" although Save had it. On a worker:
        that is up to 200k lines."""
        lines = self._lines
        if len(lines) <= self.view.document().blockCount():
            self._find_status.setText("not found")    # the view has every line
            return
        self._find_status.setText("searching older lines…")
        seq, snapshot, rx, gate = self._find_seq, list(lines), self._rx, self._find_gate

        def work():
            want = needle if case else needle.lower()
            hits = 0
            for line in snapshot:
                if (want in (line if case else line.lower())
                        and (rx is None or type(line) is _Marker or rx.search(line))):
                    hits += 1
            gate.emit(seq, hits)

        threading.Thread(target=work, name="logs-find", daemon=True).start()

    def _on_find_done(self, seq: int, hits: int):
        if seq != self._find_seq:
            return                                    # searched for something else since
        if hits:
            self._find_status.setText(f"found in {hits:,} older line"
                                      f"{'s' if hits > 1 else ''} (not shown)")
            self._find_show.show()
        else:
            self._find_status.setText("not found")

    def _filter_for_find(self):
        """Show the lines with what was found: the find text becomes the filter."""
        needle = self.find.text()
        if not needle:
            return
        # grouped: the filter text is stripped, and an escaped trailing space
        # ("error\ ") would end the pattern with a lone backslash
        pattern = "(?:" + re.escape(needle) + ")"
        if not self.find_case.isChecked():
            pattern = "(?i)" + pattern
        self.filter.setText(pattern)
        self._filter_timer.stop()                     # (setText started it)
        self._apply_filter()
        self._reset_find()

    def _reset_find(self):
        self._find_seq += 1
        self._find_status.setText("")
        self._find_show.hide()

    def find_next(self) -> bool:
        return self._find(False)

    def find_previous(self) -> bool:
        return self._find(True)

    def _clear(self):
        self._reset_find()
        self._lines.clear()
        self._pending.clear()
        self._older.clear()
        self._older_count = 0
        self._scan = None
        self._view_reset = False
        self._count = 0
        self.view.clear()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save log", "log.txt")
        if path:
            self._save_to(path)

    def _save_to(self, path: str) -> threading.Thread:
        """Write every stored line that passes the filter (not just the view's
        latest ones) to *path*, on a worker thread."""
        lines, rx, gate = list(self._lines), self._rx, self._save_gate

        def work():
            try:
                count = 0
                with open(path, "w", encoding="utf-8") as fh:
                    for line in lines:
                        if rx is None or type(line) is _Marker or rx.search(line):
                            fh.write(line + "\n")
                            count += 1
                gate.emit(f"saved {count} lines to {path}")
            except OSError as exc:
                gate.emit(f"⚠ could not save: {exc}")

        worker = threading.Thread(target=work, name="logs-save", daemon=True)
        worker.start()
        return worker

    def close_tab(self):
        """Stop the stream and drop the backlog — rendering it into a view about
        to be deleted froze the window for seconds."""
        self.stop_event.set()
        self._save_gate.close()
        self._find_gate.close()
        self._note_timer.stop()
        thread, self._thread = self._thread, None
        qtutil.park_thread(thread)
        self._sources.clear()
        self._timer.stop()
        self._filter_timer.stop()
        self._clear()
