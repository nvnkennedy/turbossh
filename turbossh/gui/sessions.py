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
        # True while the file is there but could not be read (a passing lock):
        # what is in memory is not what it holds, so read it again soon
        self.unread = False
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
            self.unread = True
            return False
        self._seen = seen
        self.unread = False
        if self.load_problem is not None and _transient(self.load_problem):
            self.load_problem = None   # it reads now: the lock has passed
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
        self.unread = False
        self._seen = _signature()        # this list is what the file holds now

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

    def shared_jump_changing(self, old: dict, new: dict) -> list:
        """The jump host in Settings changes from *old* to *new* (each
        ``{"host", "user", "password"}``): keep every session that used the old
        one on the password of ITS jump host.

        Only sessions whose jump host and user are the Settings ones share its
        password (:func:`uses_shared_jump`). Of those:

        * same host and user, new password: a copy of the old password that a
          session kept (2.0.0 stored one for each) is dropped, so it follows the
          new one instead of locking the account with the old one;
        * another host or user: a session that took the shared password keeps
          the old one as its own. It still goes to the old host, which must not
          be sent the new host's password (and its own still works there).

        A session with another jump host is not touched: its own password is its
        own, even when it happens to equal the shared one. Returns the names of
        those that keep the OLD password while it changes: perhaps the same
        jump host by another name (a full DNS name, an IP address), so the
        caller can say so."""
        old_pw = old.get("password") or ""
        if not old_pw:
            return []                    # nothing was shared, nothing to keep
        same = _same_jump(old.get("host"), old.get("user"), new.get("host"), new.get("user"))
        if same and (new.get("password") or "") == old_pw:
            return []
        elsewhere = []
        for session in list(self.sessions):
            name = session.get("name")
            if not name:
                continue
            if not uses_shared_jump(session, old):
                if (same and session.get("use_jump")
                        and (self.jump_password(name) or "") == old_pw):
                    elsewhere.append(name)
                continue
            own = self.jump_password(name) or ""
            if same:
                if own == old_pw:
                    self._set_secret(f"{name}::jump", None)
            elif not own and session.get("use_jump"):
                self._set_secret(f"{name}::jump", old_pw)
        return elsewhere


def _same_jump(host_a, user_a, host_b, user_b) -> bool:
    """The same jump account: host names compare case-insensitively (DNS), and
    so do user names (the jump host is usually a Windows machine, where "JDoe"
    is "jdoe"). An empty host is nobody's."""
    host_a, host_b = (host_a or "").strip(), (host_b or "").strip()
    return bool(host_a) and host_a.lower() == host_b.lower() \
        and (user_a or "").strip().lower() == (user_b or "").strip().lower()


def uses_shared_jump(session: dict, shared: Optional[dict] = None) -> bool:
    """Whether *session* goes through the jump host in Settings (*shared*, read
    afresh when not given): its jump host AND user are the Settings ones. Only
    then is the password in Settings its password; any other jump host has its
    own, and none means none (a key or agent login)."""
    if shared is None:
        from . import settings
        shared = settings.shared_jump()
    return _same_jump(session.get("jhost"), session.get("juser"),
                      shared.get("host"), shared.get("user"))


def jump_secret_to_store(session: dict, entered: str) -> str:
    """The jump password worth keeping for *session* ("" = none). Only a session
    that goes through a jump host keeps one; a session going through the jump
    host in Settings does not keep a copy of its password (it follows that one
    when it changes). Any other jump host keeps what was typed, even when it
    equals the shared one: it is that host's password."""
    if not session.get("use_jump") or not entered:
        return ""
    from . import settings
    shared = settings.shared_jump()
    if uses_shared_jump(session, shared) and entered == shared["password"]:
        return ""
    return entered


def effective_jump_password(session: dict, entered: str) -> str:
    """The jump password to connect *session* with when *entered* is what its
    own is: that, else the shared one if it goes through the jump host in
    Settings, else none."""
    if entered:
        return entered
    from . import settings
    shared = settings.shared_jump()
    return shared["password"] if uses_shared_jump(session, shared) else ""


def jump_password_for(name: str, session: dict) -> str:
    """The jump password to connect saved *session* with: its own, else the
    shared one from Settings when it goes through that jump host, else none.
    A 2.0.0 session with no jump password of its own (a key or agent login to
    another bastion) is not sent the shared one."""
    if not session.get("use_jump"):
        return ""
    return effective_jump_password(session, SessionStore.jump_password(name) or "")
