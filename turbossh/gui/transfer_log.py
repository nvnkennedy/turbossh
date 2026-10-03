"""The record behind the Transfers panel: what was queued, what is moving, how
fast, and how each one ended. No Qt in here — the file browser writes to it, the
panel renders it, and it is unit-testable on its own.

Byte counts drive everything (progress, speed, ETA). A folder's total is not
known up front, so a folder shows bytes and files moved rather than a percentage.
"""

from __future__ import annotations

import os
import posixpath
import time
from collections import deque
from typing import Callable, Iterable, List, Optional

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
FINISHED = frozenset((DONE, FAILED, CANCELLED))
ACTIVE = frozenset((QUEUED, RUNNING))

DIRECTION_LABELS = {"upload": "PC → Remote", "download": "Remote → PC"}
_PAST = {"upload": "Uploaded", "download": "Downloaded"}

_SPEED_WINDOW = 5.0          # seconds of samples the speed is averaged over
_HISTORY_LIMIT = 500         # finished items kept before the oldest are dropped


def human_size(num) -> str:
    try:
        n = float(num)
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"
    return ""


def human_speed(bytes_per_second) -> str:
    return f"{human_size(bytes_per_second)}/s" if bytes_per_second and bytes_per_second > 0 else ""


def human_duration(seconds) -> str:
    if seconds is None or seconds < 0:
        return ""
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class TransferItem:
    """One queued / running / finished transfer."""

    __slots__ = ("id", "kind", "remote", "local", "is_dir", "status", "error",
                 "total", "moved", "files", "current", "percent",
                 "added_at", "started_at", "finished_at")

    def __init__(self, item_id, kind, remote, local, is_dir, now):
        self.id = item_id
        self.kind = kind                 # "upload" | "download"
        self.remote = remote
        self.local = local
        self.is_dir = bool(is_dir)
        self.status = QUEUED
        self.error = ""
        self.total: Optional[int] = None  # bytes, when known (a single file)
        self.moved = 0                    # bytes moved so far
        self.files = 0                    # files completed (folders)
        self.current = ""                 # file being moved right now
        self.percent = 0                  # of the current file
        self.added_at = now
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None

    @property
    def name(self) -> str:
        return posixpath.basename(self.remote.rstrip("/")) or self.remote

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def job(self) -> tuple:
        """What is needed to run it again."""
        return self.kind, self.remote, self.local, self.is_dir

    def elapsed(self, now: float) -> Optional[float]:
        if self.started_at is None:
            return None
        return (self.finished_at if self.finished_at is not None else now) - self.started_at

    def speed(self, now: float) -> float:
        """Average over the item's whole run (the live figure is TransferLog.speed)."""
        elapsed = self.elapsed(now)
        return self.moved / elapsed if elapsed and elapsed > 0 else 0.0

    def progress_text(self) -> str:
        if self.status == QUEUED:
            return "waiting"
        if self.is_dir:
            files = f"{self.files} file{'s' if self.files != 1 else ''}"
            return f"{files}, {human_size(self.moved)}"
        if self.status == DONE:
            return "100%"
        return f"{self.percent}%"


class TransferStats:
    __slots__ = ("queued", "running", "done", "failed", "cancelled", "moved",
                 "speed", "eta")

    def __init__(self):
        self.queued = self.running = self.done = self.failed = self.cancelled = 0
        self.moved = 0
        self.speed = 0.0
        self.eta: Optional[float] = None

    @property
    def active(self) -> bool:
        return bool(self.queued or self.running)

    @property
    def total(self) -> int:
        return self.queued + self.running + self.done + self.failed + self.cancelled

    def summary(self) -> str:
        """One line for the panel header / status bar."""
        if not self.total:
            return "No transfers yet"
        parts = []
        for count, word in ((self.running, "running"), (self.queued, "queued"),
                            (self.done, "done"), (self.failed, "failed"),
                            (self.cancelled, "cancelled")):
            if count:
                parts.append(f"{count} {word}")
        text = " · ".join(parts)
        if self.running and self.speed:
            text += f" — {human_speed(self.speed)}"
            if self.eta is not None:
                text += f", about {human_duration(self.eta)} left"
        return text


class TransferLog:
    """``revision`` grows on every change, so a view can poll cheaply and redraw
    only when something actually moved."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 limit: int = _HISTORY_LIMIT):
        self._clock = clock
        self._limit = limit
        self._items: List[TransferItem] = []
        self._next_id = 1
        self._samples: deque = deque()   # (time, total bytes moved by all items)
        self._moved_total = 0
        self.revision = 0

    # ---- reading ----
    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def items(self) -> List[TransferItem]:
        return list(self._items)

    def get(self, item_id: int) -> Optional[TransferItem]:
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    @property
    def active(self) -> bool:
        return any(item.active for item in self._items)

    # ---- writing ----
    def _touch(self):
        self.revision += 1

    def add(self, kind: str, remote: str, local: str, is_dir: bool = False) -> int:
        item = TransferItem(self._next_id, kind, remote, local, is_dir, self._clock())
        self._next_id += 1
        self._items.append(item)
        self._trim()
        self._touch()
        return item.id

    def start(self, item_id: int) -> None:
        item = self.get(item_id)
        if item is None or item.finished:
            return
        item.status = RUNNING
        item.started_at = self._clock()
        # the baseline the first progress report is measured against
        self._samples.append((item.started_at, self._moved_total))
        self._touch()

    def progress(self, item_id: int, current: str, percent: int, moved: int,
                 total: Optional[int] = None, files: Optional[int] = None) -> None:
        """*moved* is the item's cumulative byte count across all its files."""
        item = self.get(item_id)
        if item is None or item.status != RUNNING:
            return
        delta = max(0, int(moved) - item.moved)
        item.moved = max(item.moved, int(moved))
        item.current = current or item.current
        item.percent = max(0, min(100, int(percent)))
        if total is not None and not item.is_dir:
            item.total = int(total)
        if files is not None:
            item.files = int(files)
        if delta:
            self._moved_total += delta
            self._samples.append((self._clock(), self._moved_total))
        self._touch()

    def finish(self, item_id: int, status: str, error: str = "",
               size: Optional[int] = None, files: Optional[int] = None) -> None:
        item = self.get(item_id)
        if item is None or item.finished:
            return
        item.status = status
        item.error = error or ""
        item.finished_at = self._clock()
        if item.started_at is None:
            item.started_at = item.finished_at
        if size is not None:
            delta = max(0, int(size) - item.moved)
            item.moved = max(item.moved, int(size))
            if delta:
                self._moved_total += delta
            if not item.is_dir:
                item.total = item.moved
        if files is not None:
            item.files = int(files)
        if status == DONE:
            item.percent = 100
        self._trim()
        self._touch()

    def cancel_queued(self) -> List[int]:
        """Mark everything still waiting as cancelled; returns their ids."""
        cancelled = []
        for item in self._items:
            if item.status == QUEUED:
                self.finish(item.id, CANCELLED)
                cancelled.append(item.id)
        return cancelled

    def remove(self, ids: Iterable[int]) -> int:
        """Drop finished items from the history (active ones are kept)."""
        wanted = set(ids)
        before = len(self._items)
        self._items = [i for i in self._items if not (i.id in wanted and i.finished)]
        if len(self._items) != before:
            self._touch()
        return before - len(self._items)

    def clear_finished(self) -> int:
        return self.remove([i.id for i in self._items if i.finished])

    def _trim(self) -> None:
        finished = [i for i in self._items if i.finished]
        excess = len(finished) - self._limit
        if excess > 0:
            drop = {i.id for i in finished[:excess]}
            self._items = [i for i in self._items if i.id not in drop]

    # ---- figures ----
    def speed(self) -> float:
        """Bytes per second over the last few seconds, 0 when nothing is moving."""
        now = self._clock()
        samples = self._samples
        while samples and now - samples[0][0] > _SPEED_WINDOW:
            samples.popleft()
        if len(samples) < 2 or not any(i.status == RUNNING for i in self._items):
            return 0.0
        span = samples[-1][0] - samples[0][0]
        return (samples[-1][1] - samples[0][1]) / span if span > 0 else 0.0

    def stats(self) -> TransferStats:
        stats = TransferStats()
        remaining, unknown = 0, False
        for item in self._items:
            setattr(stats, item.status, getattr(stats, item.status) + 1)
            stats.moved += item.moved
            if item.active:
                if item.total is None:
                    unknown = True       # a folder, or not started: no honest ETA
                else:
                    remaining += max(0, item.total - item.moved)
        stats.speed = self.speed()
        if stats.active and not unknown and stats.speed > 0:
            stats.eta = remaining / stats.speed
        return stats

    def retryable(self, ids: Optional[Iterable[int]] = None) -> List[TransferItem]:
        """Failed / cancelled items (optionally only among *ids*) worth re-running."""
        wanted = None if ids is None else set(ids)
        return [i for i in self._items
                if i.status in (FAILED, CANCELLED) and (wanted is None or i.id in wanted)]

    def report(self) -> str:
        """A plain-text account of every transfer, for the clipboard or a file."""
        now = self._clock()
        lines = [f"TurboSSH transfers — {time.strftime('%Y-%m-%d %H:%M:%S')}",
                 self.stats().summary(), ""]
        for item in self._items:
            took = human_duration(item.elapsed(now))
            line = (f"[{item.status.upper():9}] {DIRECTION_LABELS[item.kind]:11} "
                    f"{item.remote}  <->  {item.local}  "
                    f"({human_size(item.moved) or '0 B'}"
                    + (f", {item.files} files" if item.is_dir else "")
                    + (f", {took}" if took else "") + ")")
            if item.error:
                line += f"\n            {item.error}"
            lines.append(line)
        return "\n".join(lines) + "\n"


def past_tense(item: TransferItem) -> str:
    """"Downloaded notes.txt (1.2 MB in 3s)" — for a log line or a toast."""
    took = human_duration(item.elapsed(item.finished_at or 0))
    detail = human_size(item.moved)
    if item.is_dir:
        detail = f"{item.files} files, {detail}"
    return (f"{_PAST[item.kind]} {item.name} ({detail}"
            + (f" in {took}" if took else "") + ")")


def local_folder(item: TransferItem) -> str:
    """The local folder to reveal for an item."""
    return item.local if (item.is_dir and os.path.isdir(item.local)) \
        else os.path.dirname(item.local)
