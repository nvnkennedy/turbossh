"""Durable JSON files for user state (sessions, settings).

Two rules the plain ``open(path, "w")`` + ``json.dump`` pattern breaks:

* a crash or power loss mid-write must never leave a half-written file — so
  writes go to a temp file in the same folder, are fsync'd, then swapped in with
  :func:`os.replace` (atomic on every supported platform);
* a file that cannot be parsed must never be silently treated as "empty" and
  then overwritten — so it is copied aside first and the caller is told.
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


def read_json(path: str, expect: Optional[type] = None) -> Tuple[Any, Optional[str]]:
    """Read *path*. Returns ``(value, problem)``.

    * missing file            -> ``(None, None)``
    * parsed, right type      -> ``(value, None)``
    * unreadable / wrong type -> ``(None, "<what went wrong>")`` after copying the
      file aside (the message names the backup), so the caller can warn the user
      instead of quietly starting from nothing.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as exc:
        backup = set_aside(path)
        kept = f" A copy was kept at {backup}." if backup else ""
        return None, f"{os.path.basename(path)} could not be read ({exc}).{kept}"
    if expect is not None and not isinstance(value, expect):
        backup = set_aside(path)
        kept = f" A copy was kept at {backup}." if backup else ""
        return None, (f"{os.path.basename(path)} has an unexpected layout "
                      f"(expected {expect.__name__}).{kept}")
    return value, None
