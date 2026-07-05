import logging
logging.disable(logging.CRITICAL)

import os
import sys
# Test the working-tree package, not whatever 'turbossh' happens to be installed
# in site-packages (running this file puts tests/ on sys.path, not the repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import turbossh as P
from turbossh import (SSHHandler, SSHConfig, FTPConfig, SSHPool, Secret, mask,
                         OperationResult, SSHError, TransferResult)
from turbossh.results import strip_ansi

print("version", P.__version__)

# Secret never leaks
s = Secret("hunter2")
assert str(s) == "********"
assert "hunter2" not in repr(s)
assert s.reveal() == "hunter2"
assert mask("login pw=hunter2 ok", s) == "login pw=******** ok"
print("Secret masking OK")

# domain username building (single backslash)
cfg = SSHConfig(host="h", domain="CORP", username="myuser", password="pw")
assert cfg.auth_username == "CORP\\myuser", repr(cfg.auth_username)
assert isinstance(cfg.password, Secret)
assert "hunter" not in repr(cfg) and "********" in repr(cfg)
print("domain login:", repr(cfg.auth_username), "| safe repr:", repr(cfg))

# fast_auth disables key probing when a password is present
h = SSHHandler.__new__(SSHHandler)
h.config = cfg
h._jump = None
h.log = logging.getLogger("x")
h._log_callback = None
kw = h._build_connect_kwargs(empty_password=False, sock=None)
assert kw["allow_agent"] is False and kw["look_for_keys"] is False
assert kw["password"] == "pw" and kw["username"] == "CORP\\myuser"
print("fast_auth kwargs OK")

# empty-password fallback strategy
kw2 = h._build_connect_kwargs(empty_password=True, sock=None)
assert kw2["password"] == "" and kw2["allow_agent"] is False
print("empty-password strategy OK")

# explicit password="" must send a blank password on the PRIMARY attempt
# (regression guard: previously the primary probed keys and failed with
# "No authentication methods available" before the empty password was tried)
hp = SSHHandler.__new__(SSHHandler)
hp.config = SSHConfig(host="t", username="u", password="")
hp._jump = None; hp.log = logging.getLogger("x2"); hp._log_callback = None
kw3 = hp._build_connect_kwargs(empty_password=False, sock=None)
assert kw3["password"] == "" and kw3["allow_agent"] is False \
    and kw3["look_for_keys"] is False
print("explicit empty-password (primary) OK")

# TransferResult math
tr = TransferResult("a", "b", "push", "sftp", 1048576, 2.0, 1)
assert abs(tr.speed_bps - 524288) < 1 and tr.human_size == "1.0MB"
print("TransferResult:", tr.human_speed, tr.human_size)

# mkdir_p path logic (absolute + relative)
class FS:
    def __init__(self): self.made = []; self.have = set()
    def stat(self, p):
        import types
        if p in self.have: return types.SimpleNamespace(st_mode=0o040755)
        raise IOError()
    def mkdir(self, p): self.made.append(p); self.have.add(p)

fs = FS(); SSHHandler._mkdir_p(h, fs, "/a/b/c")
assert fs.made == ["/a", "/a/b", "/a/b/c"], fs.made
fs2 = FS(); SSHHandler._mkdir_p(h, fs2, "rel/x")
assert fs2.made == ["rel", "rel/x"], fs2.made
print("mkdir_p OK")

# strip_ansi removes CSI colour codes, carriage returns and control chars
assert strip_ansi("\x1b[31mred\x1b[0m\r\nok") == "red\nok"
assert strip_ansi("a\x1b[2K\x1b[1Gb") == "ab"
print("strip_ansi OK")

# an unknown host_key_policy is rejected loudly (no silent AutoAdd fallback)
try:
    SSHConfig(host="h", host_key_policy="strcit")   # typo
    raise SystemExit("FAIL: bad host_key_policy should have raised")
except ValueError:
    print("host_key_policy validation OK")
for good in ("auto", "reject", "warn", "ignore"):
    SSHConfig(host="h", host_key_policy=good)

# FTPConfig coerces its password into a Secret too
fc = FTPConfig(host="h", username="u", password="pw")
assert isinstance(fc.password, Secret) and fc.password.reveal() == "pw"
print("FTPConfig secret OK")

# SSHPool keeps duplicate hosts distinct instead of collapsing them
pool = SSHPool([SSHConfig(host="dup", port=22, username="a", password="x"),
                SSHConfig(host="dup", port=2222, username="b", password="y")])
assert len(pool.handlers) == 2, list(pool.handlers)
print("SSHPool duplicate hosts OK:", list(pool.handlers))

# connect failure -> typed exc (raise) and OperationResult (safe).
# diagnose_on_failure=False so this stays fast and doesn't probe RDP ports.
bad = SSHConfig(host="10.255.255.1", username="x", password="y",
                connect_timeout=1, max_retries=1, retry_backoff=0.1,
                diagnose_on_failure=False)
try:
    SSHHandler(bad).connect(); raise SystemExit("FAIL: should have raised")
except SSHError as e:
    print("raise-mode exc:", type(e).__name__)
r = SSHHandler(bad, safe=True).connect()
assert isinstance(r, OperationResult) and not r
print("safe-mode error:", type(r.error).__name__)

print("ALL CORE CHECKS PASSED")
