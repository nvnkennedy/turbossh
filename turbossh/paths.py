"""Where TurboSSH keeps its per-user state — one resolver used by the library,
the CLI and the GUI, so the location is defined in exactly one place.

Set ``TURBOSSH_HOME`` to relocate it (portable installs, tests).
"""

from __future__ import annotations

import os


def state_dir() -> str:
    """``$TURBOSSH_HOME`` if set, otherwise ``~/.turbossh``. Not created here."""
    override = os.environ.get("TURBOSSH_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.expanduser("~"), ".turbossh")


def state_path(*parts: str) -> str:
    """A path inside :func:`state_dir`."""
    return os.path.join(state_dir(), *parts)
