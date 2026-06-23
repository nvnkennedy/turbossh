"""Rich, structured result objects returned by every action."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ANSI/VT escape sequences (CSI like ESC[23;80H, OSC like ESC]0;title BEL, etc.)
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
# control chars except tab(09), newline(0a), carriage-return(0d handled separately)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape codes, carriage returns, and other control chars,
    so streamed console output is clean to save, match, and read."""
    if not text:
        return text
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    return _CTRL_RE.sub("", text)


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0:
            return f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}PB"


@dataclass
class CommandResult:
    """Result of a remote command execution."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    host: str = ""
    started_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def text(self) -> str:
        """stdout with surrounding whitespace/newlines stripped - print-ready."""
        return self.stdout.strip()

    def __bool__(self) -> bool:
        return self.ok

    def as_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        status = "ok" if self.ok else f"FAILED (exit {self.exit_code})"
        return f"$ {self.command}  [{status}, {self.duration:.2f}s]"


@dataclass
class TransferResult:
    """Result of a file/dir transfer (push/pull via SFTP or SCP)."""

    source: str
    dest: str
    direction: str           # "push" or "pull"
    protocol: str            # "sftp" or "scp"
    size_bytes: int
    duration: float
    files: int = 1

    @property
    def speed_bps(self) -> float:
        return self.size_bytes / self.duration if self.duration > 0 else 0.0

    @property
    def human_speed(self) -> str:
        return f"{_human_size(self.speed_bps)}/s"

    @property
    def human_size(self) -> str:
        return _human_size(self.size_bytes)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(speed_bps=self.speed_bps, human_speed=self.human_speed)
        return d

    def __str__(self) -> str:
        verb = "Uploaded" if self.direction == "push" else "Downloaded"
        files = f"{self.files} files, " if self.files != 1 else ""
        return (f"{verb} {self.source} -> {self.dest}  "
                f"({files}{self.human_size} in {self.duration:.2f}s, "
                f"{self.human_speed})")


@dataclass
class ShellResult:
    """Result of an interactive shell read/expect operation."""

    output: str
    matched: Optional[str]
    timed_out: bool
    duration: float

    def __bool__(self) -> bool:
        return not self.timed_out

    def __str__(self) -> str:
        return (
            f"<ShellResult matched={self.matched!r} "
            f"timed_out={self.timed_out} dur={self.duration:.2f}s>"
        )


@dataclass
class OperationResult:
    """
    Returned by safe-mode operations. Wraps a value or an error so GUI handlers
    never have to try/except. Falsy on failure.
    """

    success: bool
    action: str = ""
    value: object = None
    error: Optional[Exception] = None

    def __bool__(self) -> bool:
        return self.success

    def unwrap(self):
        """Return value on success, else re-raise the captured error."""
        if self.success:
            return self.value
        raise self.error if self.error else RuntimeError(f"{self.action} failed")

    def __str__(self) -> str:
        if self.success:
            return f"<OperationResult {self.action} ok value={self.value!r}>"
        return f"<OperationResult {self.action} error={self.error!r}>"
