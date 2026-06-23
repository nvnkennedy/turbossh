"""
Confidential credential handling.

* ``Secret``        - wraps a password so it never shows up in logs, reprs,
                      tracebacks, or accidental ``print``/``str`` calls.
* ``mask``          - redact secrets inside arbitrary strings before logging.
* ``CredentialStore`` - store/retrieve passwords in the OS credential vault
                      (Windows Credential Manager / macOS Keychain / Secret
                      Service) via ``keyring``. Nothing is written in plaintext.
* ``prompt_password`` - read a password from the terminal without echoing it.
"""

from __future__ import annotations

import getpass
from typing import Optional

from .exceptions import CredentialError

try:  # keyring is optional; only needed for the persistent store
    import keyring
    _HAS_KEYRING = True
except Exception:  # pragma: no cover
    keyring = None
    _HAS_KEYRING = False


_REDACTED = "********"


class Secret:
    """
    A string-like wrapper that refuses to reveal itself except via ``reveal()``.

    >>> s = Secret("hunter2")
    >>> print(s)            # ********
    >>> repr(s)             # "Secret('********')"
    >>> f"pw={s}"           # "pw=********"
    >>> s.reveal()          # "hunter2"  (only when you explicitly ask)
    """

    __slots__ = ("_value",)

    def __init__(self, value: Optional[str]):
        self._value = value

    def reveal(self) -> Optional[str]:
        """Return the real secret. Call only at the point of use."""
        return self._value

    def is_empty(self) -> bool:
        return self._value is None or self._value == ""

    def __bool__(self) -> bool:
        return bool(self._value)

    def __str__(self) -> str:
        return _REDACTED

    def __repr__(self) -> str:
        return f"Secret('{_REDACTED}')"

    # Avoid leaking through equality / hashing in logs.
    def __eq__(self, other) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:  # so it can live in dataclasses/sets safely
        return hash(self._value)


def coerce_secret(value) -> Optional[Secret]:
    """Normalize None/str/Secret into a Secret (or None)."""
    if value is None:
        return None
    if isinstance(value, Secret):
        return value
    return Secret(str(value))


def mask(text: str, *secrets) -> str:
    """Replace any revealed secret values found in *text* with ``********``."""
    if not text:
        return text
    out = text
    for sec in secrets:
        raw = sec.reveal() if isinstance(sec, Secret) else sec
        if raw:
            out = out.replace(str(raw), _REDACTED)
    return out


def prompt_password(prompt: str = "Password: ") -> Secret:
    """Read a password from the terminal without echo. Returns a Secret."""
    return Secret(getpass.getpass(prompt))


class CredentialStore:
    """
    Persist passwords in the OS credential vault — never in plaintext files.

    The ``service`` namespaces your app's credentials. ``username`` is the full
    account identifier; for a domain account use ``"CORP\\myuser"`` (the store
    treats it as an opaque key, so domain accounts work transparently).
    """

    def __init__(self, service: str = "turbossh"):
        if not _HAS_KEYRING:
            raise CredentialError(
                "The 'keyring' package is required for CredentialStore. "
                "Install it with: pip install keyring"
            )
        self.service = service

    @staticmethod
    def available() -> bool:
        return _HAS_KEYRING

    def set(self, username: str, password) -> None:
        """Store (or overwrite) a password for *username*."""
        raw = password.reveal() if isinstance(password, Secret) else password
        try:
            keyring.set_password(self.service, username, raw)
        except Exception as exc:  # pragma: no cover
            raise CredentialError(f"Could not store credential: {exc}") from exc

    def get(self, username: str) -> Optional[Secret]:
        """Return the stored password as a Secret, or None if not found."""
        try:
            raw = keyring.get_password(self.service, username)
        except Exception as exc:  # pragma: no cover
            raise CredentialError(f"Could not read credential: {exc}") from exc
        return Secret(raw) if raw is not None else None

    def delete(self, username: str) -> bool:
        """Delete a stored password. Returns False if it was not present."""
        try:
            keyring.delete_password(self.service, username)
            return True
        except keyring.errors.PasswordDeleteError:  # type: ignore[attr-defined]
            return False
        except Exception as exc:  # pragma: no cover
            raise CredentialError(f"Could not delete credential: {exc}") from exc
