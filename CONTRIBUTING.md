# Contributing to TurboSSH

Thanks for helping improve TurboSSH. This is a small, pragmatic toolkit — clear
fixes and well-scoped features are very welcome.

## Development setup

```bash
git clone https://github.com/nvnkennedy/turbossh
cd turbossh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[gui,test]"                      # GUI extras optional
pip install ruff mypy build twine
```

> Building requires Python **3.9+** (setuptools ≥77 for the PEP 639 license). The
> published wheel still *runs* on 3.8; CI checks that by running the suite from
> the source tree on 3.8.

## Before you push

```bash
ruff check turbossh tests scripts   # lint (pyflakes + a few pycodestyle rules)
python -m pytest -q                 # the test suite
mypy turbossh                       # optional; type hints ship via py.typed
```

The suite needs no network, no SSH server, no serial port and no display:

- `tests/sshserver.py` is a loopback SSH + SFTP server (Paramiko's server side)
  with scripted behaviour — commands that hang or flood, a `sudo` that prompts,
  password / key auth with a log of every attempt, jump-host forwarding. The
  `ssh_server`, `make_config` and `ssh` fixtures in `tests/conftest.py` use it.
- GUI tests run on Qt's `offscreen` platform (set by `conftest.py`) and skip
  themselves when PyQt5 is not installed.
- `conftest.py` points `TURBOSSH_HOME`, `HOME` and `USERPROFILE` at a temp folder
  and routes `keyring` to an in-memory backend, so a test can never touch your
  real `~/.turbossh` or credential vault.

`python tests/test_offline.py` is the original stand-alone check script; it still
runs in CI and before a release.

CI runs lint + tests on Linux and Windows across Python 3.9 / 3.11 / 3.13, the
GUI tests on Windows, the suite from source on 3.8, and a build + `twine check`.

## Code style

- Match the surrounding code: dense, heavily-commented, explanatory docstrings.
- No new runtime dependencies without discussion. Optional integrations are
  guarded with a `try/except ImportError` + a clear "install X" message.
- Keep secrets off logs and command lines (`Secret`, `mask`, keyring, stdin/API).
- Nothing that touches the network, a process or a port runs on the GUI thread.
  Use the helpers in `turbossh/gui/qtutil.py` (`run_job`, `park_thread`,
  `run_teardown`); a widget's `close_session()` must not wait, and must leave no
  thread that can call back into it.
- Per-user state goes through `turbossh.paths.state_path(...)`, and JSON state
  through `turbossh._jsonstore` (atomic writes, corrupt files copied aside).

## Releasing (maintainers)

The version lives in **one place**: `turbossh/__init__.py` (`__version__`);
`pyproject.toml` reads it dynamically. Releases are published manually:

```bash
# 1. add a "## X.Y.Z — YYYY-MM-DD" section to CHANGELOG.md
# 2. build the GUI exe and put it where the wheel picks it up
python scripts/build_exe.py --onefile
copy dist\turbossh-gui.exe turbossh\bin\
# 3. test, bump, build, check, upload
python scripts/release.py X.Y.Z --dry-run      # everything except the upload
python scripts/release.py X.Y.Z
git commit -am "Release X.Y.Z" && git tag vX.Y.Z && git push --tags
```

`release.py` refuses to continue when CHANGELOG.md has no section for the
version or the built wheel lacks `turbossh/bin/turbossh-gui.exe` (`--no-exe` /
`--skip-changelog` override those), and it puts the old version string back if
any step fails.
