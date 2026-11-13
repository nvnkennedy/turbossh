"""Durable JSON files for user state (sessions, settings).

Two rules the plain ``open(path, "w")`` + ``json.dump`` pattern breaks:

* a crash or power loss mid-write must never leave a half-written file — so
  writes go to a temp file in the same folder, are fsync'd, then swapped in with
  :func:`os.replace` (atomic on every supported platform);
* a file that cannot be parsed must never be silently treated as "empty" and
  then overwritten — so it is copied aside first and the caller is told;
* a file that merely cannot be opened right now (another program holds it) is
  not corrupt: the read is retried, and the caller is told not to save over it.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Any, Optional, Tuple


def _replace_with_retry(src: str, dst: str, attempts: int = 5) -> None:
    """``os.replace`` that rides out a short-lived lock on Windows (an antivirus
    or indexer briefly holding the destination open)."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_json(path: str, value: Any) -> None:
    """Replace *path* with *value* as JSON without ever leaving it half-written."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".turbossh-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_aside(path: str) -> Optional[str]:
    """Copy an unreadable file to ``<path>.corrupt-<timestamp>`` so its contents
    survive the next save. Returns the backup path, or None if it could not be made."""
    backup = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        with open(path, "rb") as src, open(backup, "wb") as dst:
            dst.write(src.read())
        return backup
    except OSError:
        return None


class ReadProblem(str):
    """What :func:`read_json` says went wrong: the message, plus whether the file
    may be saved over.

    ``transient`` is True when the file could not be opened or read at all (held
    open by another program, a share that dropped). Its contents may be perfectly
    good and nothing was copied aside, so the caller must NOT write its defaults
    over it; reading again later may well work. False when the file was read but
    is corrupt or of the wrong layout; then ``backup`` names the copy kept aside
    (None if none could be made)."""

    transient: bool = False
    backup: Optional[str] = None

    def __new__(cls, message: str, *, transient: bool = False,
                backup: Optional[str] = None):
        self = super().__new__(cls, message)
        self.transient, self.backup = transient, backup
        return self


def _read_text(path: str, attempts: int = 5) -> str:
    """The file's text, riding out a short-lived lock on Windows (an antivirus
    scan, an indexer, another TurboSSH window saving) the way
    :func:`_replace_with_retry` does for writes."""
    attempt = 0
    while True:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except PermissionError:
            attempt += 1
            if attempt >= attempts:
                raise
            time.sleep(0.05 * attempt)


def _corrupt(path: str, what: str) -> ReadProblem:
    backup = set_aside(path)
    kept = f" A copy was kept at {backup}." if backup else ""
    return ReadProblem(f"{os.path.basename(path)} {what}.{kept}", backup=backup)


def read_json(path: str, expect: Optional[type] = None) -> Tuple[Any, Optional[str]]:
    """Read *path*. Returns ``(value, problem)``; *problem* is a
    :class:`ReadProblem` (a str).

    * missing file            -> ``(None, None)``
    * parsed, right type      -> ``(value, None)``
    * corrupt / wrong type    -> ``(None, problem)`` after copying the file aside
      (the message names the backup), so the caller can warn the user instead of
      quietly starting from nothing;
    * cannot be read now      -> ``(None, problem)`` with ``problem.transient``:
      the file is left alone and must not be saved over. A sharing violation
      used to count as corruption, so settings fell back to defaults and the
      next save replaced the user's file.
    """
    try:
        value = json.loads(_read_text(path))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, ReadProblem(
            f"{os.path.basename(path)} could not be read right now ({exc}); it "
            f"was left as it is.", transient=True)
    except ValueError as exc:               # bad JSON, or not UTF-8 at all
        return None, _corrupt(path, f"could not be read ({exc})")
    if expect is not None and not isinstance(value, expect):
        return None, _corrupt(path, f"has an unexpected layout "
                                    f"(expected {expect.__name__})")
    return value, None
