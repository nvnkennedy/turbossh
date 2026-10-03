"""Logging for the package.

Every handler logs under the ``turbossh`` logger hierarchy and none of them
attaches its own stream handler or changes a shared logger's level. The narration
you see by default comes from ONE console handler on the package logger, and it
is only installed when the application has not configured logging itself — so an
app that sets up ``logging`` gets our records through its own handlers, once.
"""

from __future__ import annotations

import logging
import sys

PACKAGE = "turbossh"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

logging.getLogger(PACKAGE).addHandler(logging.NullHandler())


def get_logger(suffix: str) -> logging.Logger:
    """A logger inside the package hierarchy, e.g. ``turbossh.10.0.0.5``."""
    return logging.getLogger(f"{PACKAGE}.{suffix}")


def ensure_console_handler() -> None:
    """Install the default console handler on the package logger, once, unless
    the application already handles logging (root or ``turbossh`` has a real
    handler) or there is no console (a windowed/frozen GUI has no stderr)."""
    pkg = logging.getLogger(PACKAGE)
    if any(not isinstance(h, logging.NullHandler) for h in pkg.handlers):
        return
    if logging.getLogger().handlers or sys.stderr is None:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(_FORMAT))
    pkg.addHandler(handler)
    if pkg.level == logging.NOTSET:
        pkg.setLevel(logging.INFO)
