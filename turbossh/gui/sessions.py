"""Saved-session store (tabbed multi-session profiles).

Sessions are kept as JSON in ``<state dir>/sessions.json``; passwords are stored
separately in the OS credential vault (keyring), never in the JSON.

The file is written atomically, and a file that cannot be read is copied aside
and reported (``load_problem``) instead of being treated as "no sessions" and
then overwritten by the next save.
"""

from __future__ import annotations

from typing import Callable, Optional

from .. import paths
from .._jsonstore import atomic_write_json, read_json

_DIR = paths.state_dir()
_FILE = paths.state_path("sessions.json")
_KR_SERVICE = "turbossh-sessions"


class SessionStore:
    def __init__(self):
        self.sessions: list[dict] = []
        self.load_problem: Optional[str] = None   # set when the file was unreadable
        self.load()

    # ---- file ----
    def _read(self) -> Optional[list]:
        """The sessions on disk, or None when there is no (readable) file."""
        value, problem = read_json(_FILE, list)
        if problem:
            self.load_problem = problem
        if value is None:
            return None
        return [s for s in value if isinstance(s, dict) and s.get("name")]

    def load(self):
        data = self._read()
        self.sessions = data if data is not None else []

    def _commit(self, change: Callable[[list], None]) -> None:
        """Apply *change* to the newest list on disk, then write it atomically.

        Re-reading first makes the file the source of truth: a session added in
        a second TurboSSH window is not wiped out when this window (with its
        older in-memory copy) saves.
        """
        fresh = self._read()
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
        return SessionStore.password(f"{name}::jump")
