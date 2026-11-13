"""Saved-session store (tabbed multi-session profiles).

Sessions are kept as JSON in ``<state dir>/sessions.json``; passwords are stored
separately in the OS credential vault (keyring), never in the JSON.

The file is written atomically, and a file that cannot be read is copied aside
and reported (``load_problem``) instead of being treated as "no sessions" and
then overwritten by the next save.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from .. import paths
from .._jsonstore import atomic_write_json, read_json

_DIR = paths.state_dir()
_FILE = paths.state_path("sessions.json")
_KR_SERVICE = "turbossh-sessions"


def _signature():
    """What identifies the file's current version without reading it (an atomic
    save replaces the file, so its id changes too), or None when there is none."""
    try:
        st = os.stat(_FILE)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


class SessionStoreError(OSError):
    """sessions.json could not be read, so a change was not written: writing it
    would have replaced every saved session with this process's (stale, or at
    start-up empty) list."""


def _transient(problem) -> bool:
    """A read problem that says the file is there but could not be read just now
    (``_jsonstore`` marks it ``transient``): nothing was copied aside, so the file
    is the only copy of what it holds."""
    return bool(getattr(problem, "transient", False))


class SessionStore:
    def __init__(self):
        self.sessions: list[dict] = []
        self.load_problem: Optional[str] = None   # set when the file was unreadable
        self._seen = None                         # _signature() of what was loaded
        self.load()

    # ---- file ----
    def _read(self):
        """``(the sessions on disk, None)``; ``(None, None)`` when there is no
        file, ``(None, problem)`` when it could not be read."""
        value, problem = read_json(_FILE, list)
        if problem:
            self.load_problem = problem
        if value is None:
            return None, problem
        return [s for s in value if isinstance(s, dict) and s.get("name")], None

    def load(self) -> bool:
        """Read the file. False when it is there but could not be read just now:
        what was loaded before stays (nothing, at start-up), to be tried again."""
        seen = _signature()            # before reading: a save meanwhile is seen next time
        data, problem = self._read()
        if _transient(problem):
            self._seen = None
            return False
        self._seen = seen
        self.sessions = data if data is not None else []
        return True

    def reload_if_changed(self) -> bool:
        """Re-read the file only if it changed since it was last read; True when
        it did. A host that polls the saved names must not rebuild the lists
        (losing the user's selection) every time, nor copy an unreadable file
        aside again and again."""
        if _signature() == self._seen:
            return False
        return self.load()

    def _commit(self, change: Callable[[list], None]) -> None:
        """Apply *change* to the newest list on disk, then write it atomically.

        Re-reading first makes the file the source of truth: a session added in
        a second TurboSSH window is not wiped out when this window (with its
        older in-memory copy) saves. A file that is there but cannot be read
        right now is not written at all (``SessionStoreError``).
        """
        fresh, problem = self._read()
        if _transient(problem):
            raise SessionStoreError(f"{problem} Nothing was changed, so the saved "
                                    f"sessions were not written over: try again.")
        if fresh is not None:
            self.sessions = fresh
        change(self.sessions)
        atomic_write_json(_FILE, self.sessions)

    # ---- queries ----
    def names(self) -> list[str]:
        return [s.get("name", "?") for s in self.sessions]

    def exists(self, name: str) -> bool:
        return any(s.get("name") == name for s in self.sessions)

    def unique_name(self, base: str) -> str:
        """*base*, or ``base (2)``, ``base (3)`` … — the first name not in use."""
        if not self.exists(base):
            return base
        n = 2
        while self.exists(f"{base} ({n})"):
            n += 1
        return f"{base} ({n})"

    def get(self, name: str) -> Optional[dict]:
        for s in self.sessions:
            if s.get("name") == name:
                return dict(s)
        return None

    # ---- changes ----
    def save(self, session: dict, password: Optional[str] = None,
             jump_password: Optional[str] = None):
        """Add *session*, or replace the one with the same name IN PLACE (so
        editing a session doesn't move it to the end of the list)."""
        name = session["name"]

        def change(items: list) -> None:
            for i, existing in enumerate(items):
                if existing.get("name") == name:
                    items[i] = session
                    break
            else:
                items.append(session)
        self._commit(change)
        self._set_secret(name, password)
        if jump_password is not None:
            self._set_secret(f"{name}::jump", jump_password)

    def rename(self, old: str, new: str) -> bool:
        """Rename a session in place, moving its stored passwords with it.
        Returns False if *old* is missing or *new* is already taken."""
        if old == new:
            return True
        if not self.exists(old) or self.exists(new):
            return False

        def change(items: list) -> None:
            for existing in items:
                if existing.get("name") == old:
                    existing["name"] = new
        self._commit(change)
        for suffix in ("", "::jump"):
            secret = self.password(old + suffix)
            self._set_secret(new + suffix, secret)
            self._set_secret(old + suffix, None)
        return True

    def delete(self, name: str):
        self._commit(lambda items: items.__setitem__(
            slice(None), [s for s in items if s.get("name") != name]))
        self._set_secret(name, None)
        self._set_secret(f"{name}::jump", None)

    # --- secrets via keyring (graceful if unavailable) ---
    @staticmethod
    def _set_secret(key: str, value: Optional[str]):
        try:
            import keyring
            if value:
                keyring.set_password(_KR_SERVICE, key, value)
            else:
                try:
                    keyring.delete_password(_KR_SERVICE, key)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def password(name: str) -> Optional[str]:
        try:
            import keyring
            return keyring.get_password(_KR_SERVICE, name)
        except Exception:
            return None

    @staticmethod
    def jump_password(name: str) -> Optional[str]:
        """The session's OWN jump password (see :func:`jump_password_for`)."""
        return SessionStore.password(f"{name}::jump")

    def forget_jump_copies(self, shared: str) -> None:
        """Drop per-session jump passwords that are just copies of *shared*, the
        jump password in Settings that is about to change: they would keep the
        old one alive (and lock the account) after the change."""
        if not shared:
            return
        for name in self.names():
            if self.jump_password(name) == shared:
                self._set_secret(f"{name}::jump", None)


def jump_secret_to_store(session: dict, entered: str) -> str:
    """The jump password worth keeping for *session* ("" = none). Only a session
    that goes through a jump host keeps one, and only one that differs from the
    shared jump password in Settings: storing a copy of that for every session
    (as the dialog did) left copies behind when it was changed or cleared."""
    if not session.get("use_jump") or not entered:
        return ""
    from . import settings
    return "" if entered == settings.jump_password() else entered


def jump_password_for(name: str, session: dict) -> str:
    """The jump password to connect *session* with: its own, else the shared one
    from Settings (sessions keep their own only when it differs)."""
    if not session.get("use_jump"):
        return ""
    own = SessionStore.jump_password(name)
    if own:
        return own
    from . import settings
    return settings.jump_password()
