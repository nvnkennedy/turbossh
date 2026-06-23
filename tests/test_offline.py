import logging
logging.disable(logging.CRITICAL)

import turbossh as P
from turbossh import (SSHHandler, SSHConfig, Secret, mask, OperationResult,
                         SSHError, TransferResult)

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

# connect failure -> typed exc (raise) and OperationResult (safe)
bad = SSHConfig(host="10.255.255.1", username="x", password="y",
                connect_timeout=1, max_retries=1, retry_backoff=0.1)
try:
    SSHHandler(bad).connect(); raise SystemExit("FAIL: should have raised")
except SSHError as e:
    print("raise-mode exc:", type(e).__name__)
r = SSHHandler(bad, safe=True).connect()
assert isinstance(r, OperationResult) and not r
print("safe-mode error:", type(r.error).__name__)

print("ALL CORE CHECKS PASSED")
