"""Saved-session store (tabbed multi-session profiles).

Sessions are kept as JSON in ~/.turbossh/sessions.json; passwords are stored
separately in the OS credential vault (keyring), never in the JSON.
"""

from __future__ import annotations

import os
import json
from typing import Optional

_DIR = os.path.join(os.path.expanduser("~"), ".turbossh")
_FILE = os.path.join(_DIR, "sessions.json")
_KR_SERVICE = "turbossh-sessions"


class SessionStore:
    def __init__(self):
        self.sessions: list[dict] = []
        self.load()

    def load(self):
        try:
            with open(_FILE, "r", encoding="utf-8") as fh:
                self.sessions = json.load(fh)
        except Exception:
            self.sessions = []

    def _flush(self):
        os.makedirs(_DIR, exist_ok=True)
        with open(_FILE, "w", encoding="utf-8") as fh:
            json.dump(self.sessions, fh, indent=2)

    def names(self) -> list[str]:
        return [s.get("name", "?") for s in self.sessions]

    def get(self, name: str) -> Optional[dict]:
        for s in self.sessions:
            if s.get("name") == name:
                return dict(s)
        return None

    def save(self, session: dict, password: Optional[str] = None,
             jump_password: Optional[str] = None):
        name = session["name"]
        self.sessions = [s for s in self.sessions if s.get("name") != name]
        self.sessions.append(session)
        self._flush()
        self._set_secret(name, password)
        if jump_password is not None:
            self._set_secret(f"{name}::jump", jump_password)

    def delete(self, name: str):
        self.sessions = [s for s in self.sessions if s.get("name") != name]
        self._flush()
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
