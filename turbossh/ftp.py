"""
Plain FTP / FTPS handler (separate protocol from SSH/SFTP), built on the
standard-library ``ftplib`` so it needs no extra dependency. Mirrors the
push/pull/listing surface of SSHHandler and returns the same result objects.
"""

from __future__ import annotations

import os
import shutil
import ssl
import time
import uuid
import logging
from ftplib import FTP, FTP_TLS, error_perm, all_errors
from typing import Callable, Optional

from . import _logging
from ._jsonstore import _replace_with_retry
from .config import FTPConfig
from .credentials import Secret, mask
from .results import TransferResult, OperationResult
from .exceptions import FTPError


def _option(value, config, name: str, default):
    """*value* if given, else the config's field *name* (an FTPConfig without
    that field, or with it unset, gives *default*)."""
    if value is None:
        value = getattr(config, name, None)
    return default if value is None else value


def _put_in_place(tmp: str, dest: str) -> None:
    """Swap the finished download *tmp* in as *dest*.

    A file held open without delete sharing (a log viewer watching it) refuses
    the swap on Windows, but usually not a write: then the data is copied into
    it instead. Should even that fail, the download is kept and named in the
    error rather than deleted."""
    try:
        _replace_with_retry(tmp, dest)
        return
    except OSError as exc:
        refused = exc
    try:
        shutil.copyfile(tmp, dest)
    except OSError as exc:
        raise FTPError(
            f"FTP download finished, but {dest} could not be replaced ({refused}) "
            f"nor written ({exc}); the downloaded file is kept as {tmp}") from exc
    try:
        os.remove(tmp)
    except OSError:
        pass


# X509_V_ERR_HOSTNAME_MISMATCH / X509_V_ERR_IP_ADDRESS_MISMATCH: the certificate
# is good, but not for the name or address the server was reached by
_NAME_MISMATCH = (62, 64)


class FTPHandler:
    """
    >>> with FTPHandler(FTPConfig(host="ftp.example.com", username="u",
    ...                           password="p", use_tls=True)) as ftp:
    ...     ftp.push("local.txt", "remote.txt")
    ...     ftp.pull("remote.txt", "copy.txt")

    With FTPS (``use_tls``) the server's certificate and host name are checked
    against the system's trusted CAs, or against ``ca_file`` (a PEM bundle, e.g.
    a lab's own CA). ``check_hostname=False`` still checks the certificate but
    not the name in it (a server reached by an IP address its certificate does
    not list). ``verify_tls=False`` turns the whole check off, for a self-signed
    lab server only: without it anyone on the path can read the password.
    Each of the three can be given here or on the FTPConfig; a value given
    here wins.
    """

    def __init__(self, config: FTPConfig, *,
                 log_callback: Optional[Callable[[str], None]] = None,
                 logger: Optional[logging.Logger] = None, safe: bool = False,
                 verify_tls: Optional[bool] = None, ca_file: Optional[str] = None,
                 check_hostname: Optional[bool] = None):
        self.config = config
        # None: what the config says (FTPConfig.verify_tls / ca_file /
        # check_hostname), and the safe choice when it does not say
        self.verify_tls = bool(_option(verify_tls, config, "verify_tls", True))
        self.ca_file = _option(ca_file, config, "ca_file", None) or None
        self.check_hostname = bool(_option(check_hostname, config,
                                           "check_hostname", True))
        self._safe_default = safe
        self._log_callback = log_callback
        if logger is None:
            _logging.ensure_console_handler()
            logger = _logging.get_logger(f"ftp.{config.host}")
        self.log = logger
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

    def _tls_context(self) -> ssl.SSLContext:
        # FTP_TLS() without a context uses one that checks neither the
        # certificate nor the host name: any machine on the path could pose as
        # the server and take the login
        if not self.verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        cafile = os.path.expanduser(self.ca_file) if self.ca_file else None
        try:
            ctx = ssl.create_default_context(cafile=cafile)
        except (ssl.SSLError, OSError, ValueError) as exc:
            # ssl names neither the file nor the setting ("[Errno 2] No such
            # file or directory", "[X509] no certificate or crl found")
            if isinstance(exc, FileNotFoundError):
                raise FTPError(f"The FTPS CA file {cafile} (ca_file) was not "
                               f"found.") from exc
            raise FTPError(f"The FTPS CA file {cafile} (ca_file) could not be "
                           f"loaded as PEM certificates: {exc}") from exc
        if not self.check_hostname:
            ctx.check_hostname = False      # the certificate itself is still checked
        return ctx

    @staticmethod
    def _tls_hint(exc) -> str:
        """What to do about a certificate the check refused, naming the options
        and where they go."""
        if not isinstance(exc, ssl.SSLCertVerificationError):
            return ""
        def how(option: str) -> str:
            return f"FTPConfig(..., {option}) or FTPHandler(config, {option})"
        if getattr(exc, "verify_code", None) in _NAME_MISMATCH:
            return (" (the certificate is good but not issued for this host name "
                    "or address: connect by a name it lists, or keep checking the "
                    "certificate but not the name with "
                    + how("check_hostname=False") + ")")
        return (" (the server's TLS certificate could not be verified: give the "
                "CA that signed it, as a PEM file, with "
                + how("ca_file='lab-ca.pem'") + "; for a self-signed lab server "
                "only, turn the check off with " + how("verify_tls=False") + ")")

    def _connect(self):
        cfg = self.config
        pw = cfg.password.reveal() if isinstance(cfg.password, Secret) else cfg.password
        try:
            self._ftp = FTP_TLS(context=self._tls_context()) if cfg.use_tls else FTP()
            self._ftp.encoding = cfg.encoding
            self._ftp.connect(cfg.host, cfg.port, timeout=cfg.timeout)
            self._ftp.login(cfg.username, pw or "")
            if cfg.use_tls:
                self._ftp.prot_p()  # encrypt the data channel too
            self._ftp.set_pasv(cfg.passive)
            self._emit(logging.INFO, f"Connected to ftp://{cfg.username}@{cfg.host}.")
        except all_errors as exc:
            raise FTPError(f"FTP connect/login failed for {cfg.host}: "
                           f"{exc}{self._tls_hint(exc)}") from exc
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
                # size() fails on dirs; fall back to a listing probe. Compare by
                # basename — some servers' NLST returns bare names, others full
                # paths, so matching the raw path missed real entries.
                try:
                    base = os.path.basename(path.rstrip("/"))
                    listing = self._require().nlst(os.path.dirname(path) or ".")
                    return any(os.path.basename(e.rstrip("/")) == base
                               for e in listing)
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
            # a link is written through, to the file it points at (replacing
            # the link with a plain file would cut it off from its target)
            dest = os.path.realpath(lp) if os.path.islink(lp) else lp
            parent = os.path.dirname(dest)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            start = time.time()
            self._emit(logging.INFO, f"PULL {remote_path} -> {lp}")
            # Into a temporary file next to the target, swapped in only once it
            # is complete: writing the target itself destroyed the good copy
            # already there when the transfer broke off part-way (a VPN drop).
            tmp = os.path.join(parent, f".{os.path.basename(dest)}."
                                       f"{uuid.uuid4().hex[:8]}.part")
            try:
                with open(tmp, "xb") as fh:
                    def _cb(data):
                        fh.write(data)
                        if callback:
                            callback(len(data))
                    self._require().retrbinary(f"RETR {remote_path}", _cb)
            except BaseException as exc:
                # nothing partial is left behind, and the old file is untouched
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                if isinstance(exc, all_errors):
                    raise FTPError(f"FTP download failed: {exc}") from exc
                raise
            # the download is complete: from here on it is never thrown away
            _put_in_place(tmp, dest)
            return TransferResult(remote_path, lp, "pull", "ftp",
                                  os.path.getsize(dest), time.time() - start)
        return self._guard("pull", _do, safe=safe)

    def __enter__(self):
        r = self.connect()
        if isinstance(r, OperationResult) and not r.success:
            raise r.error or FTPError("connect failed")
        return self

    def __exit__(self, *exc):
        self.disconnect()
