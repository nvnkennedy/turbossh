"""
Plain FTP / FTPS handler (separate protocol from SSH/SFTP), built on the
standard-library ``ftplib`` so it needs no extra dependency. Mirrors the
push/pull/listing surface of SSHHandler and returns the same result objects.
"""

from __future__ import annotations

import os
import time
import logging
from ftplib import FTP, FTP_TLS, error_perm, all_errors
from typing import Callable, Optional

from .config import FTPConfig
from .credentials import Secret, mask
from .results import TransferResult, OperationResult
from .exceptions import FTPError


class FTPHandler:
    """
    >>> with FTPHandler(FTPConfig(host="ftp.example.com", username="u",
    ...                           password="p", use_tls=True)) as ftp:
    ...     ftp.push("local.txt", "remote.txt")
    ...     ftp.pull("remote.txt", "copy.txt")
    """

    def __init__(self, config: FTPConfig, *,
                 log_callback: Optional[Callable[[str], None]] = None,
                 logger: Optional[logging.Logger] = None, safe: bool = False):
        self.config = config
        self._safe_default = safe
        self._log_callback = log_callback
        self.log = logger or logging.getLogger(f"ftp_handler.{config.host}")
        if not self.log.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
            self.log.addHandler(h)
            self.log.setLevel(logging.INFO)
        self._ftp: Optional[FTP] = None

    def _emit(self, level, msg):
        msg = mask(msg, self.config.password)
        self.log.log(level, msg)
        if self._log_callback:
            try:
                self._log_callback(f"[{logging.getLevelName(level)}] {msg}")
            except Exception:
                pass

    def _guard(self, action, fn, *a, safe=None, **k):
        use_safe = self._safe_default if safe is None else safe
        if not use_safe:
            return fn(*a, **k)
        try:
            return OperationResult(True, action, value=fn(*a, **k))
        except Exception as exc:
            self._emit(logging.ERROR, f"{action} failed: {exc}")
            return OperationResult(False, action, error=exc)

    # --- connection ---
    def connect(self, *, safe=None):
        return self._guard("connect", self._connect, safe=safe)

    def _connect(self):
        cfg = self.config
        pw = cfg.password.reveal() if isinstance(cfg.password, Secret) else cfg.password
        try:
            self._ftp = FTP_TLS() if cfg.use_tls else FTP()
            self._ftp.encoding = cfg.encoding
            self._ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
            self._ftp.login(cfg.username, pw or "")
            if cfg.use_tls:
                self._ftp.prot_p()  # encrypt the data channel too
            self._ftp.set_pasv(cfg.passive)
            self._emit(logging.INFO, f"Connected to ftp://{cfg.username}@{cfg.host}.")
        except all_errors as exc:
            raise FTPError(f"FTP connect/login failed for {cfg.host}: {exc}") from exc
        return self

    def disconnect(self):
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except Exception:
                try:
                    self._ftp.close()
                except Exception:
                    pass
            self._emit(logging.INFO, f"Disconnected from {self.config.host}.")
            self._ftp = None

    close = disconnect

    def _require(self) -> FTP:
        if self._ftp is None:
            raise FTPError("Not connected. Call connect() first.")
        return self._ftp

    # --- operations ---
    def listdir(self, path: str = ".", *, safe=None):
        return self._guard("listdir", lambda: self._require().nlst(path), safe=safe)

    def cwd(self, path: str, *, safe=None):
        return self._guard("cwd", lambda: self._require().cwd(path), safe=safe)

    def pwd(self, *, safe=None):
        return self._guard("pwd", lambda: self._require().pwd(), safe=safe)

    def mkdir(self, path: str, *, safe=None):
        return self._guard("mkdir", lambda: self._require().mkd(path), safe=safe)

    def remove(self, path: str, *, safe=None):
        return self._guard("remove", lambda: self._require().delete(path), safe=safe)

    def rmdir(self, path: str, *, safe=None):
        return self._guard("rmdir", lambda: self._require().rmd(path), safe=safe)

    def rename(self, old: str, new: str, *, safe=None):
        return self._guard("rename", lambda: self._require().rename(old, new), safe=safe)

    def size(self, path: str, *, safe=None):
        return self._guard("size", lambda: self._require().size(path), safe=safe)

    def exists(self, path: str, *, safe=None):
        def _do():
            try:
                self._require().size(path)
                return True
            except error_perm:
                # size() fails on dirs; fall back to a listing probe.
                try:
                    return path in self._require().nlst(os.path.dirname(path) or ".")
                except all_errors:
                    return False
        return self._guard("exists", _do, safe=safe)

    def push(self, local_path: str, remote_path: str, *, callback=None, safe=None):
        """Upload a single file (STOR)."""
        def _do():
            lp = os.path.expanduser(local_path)
            if not os.path.isfile(lp):
                raise FTPError(f"Local file not found: {lp}")
            start = time.time()
            self._emit(logging.INFO, f"PUSH {lp} -> {remote_path}")
            try:
                with open(lp, "rb") as fh:
                    self._require().storbinary(f"STOR {remote_path}", fh,
                                               callback=callback)
            except all_errors as exc:
                raise FTPError(f"FTP upload failed: {exc}") from exc
            return TransferResult(lp, remote_path, "push", "ftp",
                                  os.path.getsize(lp), time.time() - start)
        return self._guard("push", _do, safe=safe)

    def pull(self, remote_path: str, local_path: str, *, callback=None, safe=None):
        """Download a single file (RETR)."""
        def _do():
            lp = os.path.expanduser(local_path)
            parent = os.path.dirname(lp)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            start = time.time()
            self._emit(logging.INFO, f"PULL {remote_path} -> {lp}")
            try:
                with open(lp, "wb") as fh:
                    def _cb(data):
                        fh.write(data)
                        if callback:
                            callback(len(data))
                    self._require().retrbinary(f"RETR {remote_path}", _cb)
            except all_errors as exc:
                raise FTPError(f"FTP download failed: {exc}") from exc
            return TransferResult(remote_path, lp, "pull", "ftp",
                                  os.path.getsize(lp), time.time() - start)
        return self._guard("pull", _do, safe=safe)

    def __enter__(self):
        r = self.connect()
        if isinstance(r, OperationResult) and not r.success:
            raise r.error or FTPError("connect failed")
        return self

    def __exit__(self, *exc):
        self.disconnect()
