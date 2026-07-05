# Contributing to TurboSSH

Thanks for helping improve TurboSSH. This is a small, pragmatic toolkit — clear
fixes and well-scoped features are very welcome.

## Development setup

```bash
git clone https://github.com/nvnkennedy/turbossh
cd turbossh
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[gui]"                           # GUI extras optional
pip install ruff mypy build twine
```

> Building requires Python **3.9+** (setuptools ≥77 for the PEP 639 license). The
> published wheel still *runs* on 3.8.

## Before you push

```bash
ruff check turbossh          # lint (pyflakes + a few pycodestyle rules)
python tests/test_offline.py # fast, offline unit checks (no network needed)
mypy turbossh                # optional; type hints ship via py.typed
```

CI runs the same lint + tests across Python 3.9–3.13 and a build/`twine check`.

## Code style

- Match the surrounding code: dense, heavily-commented, explanatory docstrings.
- No new runtime dependencies without discussion. Optional integrations are
  guarded with a `try/except ImportError` + a clear "install X" message.
- Keep secrets off logs and command lines (`Secret`, `mask`, keyring, stdin/API).

## Releasing (maintainers)

The version lives in **one place**: `turbossh/__init__.py` (`__version__`);
`pyproject.toml` reads it dynamically.

Preferred (automated, no token):

```bash
# edit turbossh/__init__.py __version__, update CHANGELOG.md
git commit -am "Release X.Y.Z"
git tag vX.Y.Z && git push --tags     # .github/workflows/release.yml does the rest
```

The release workflow builds the Windows GUI exe, bundles it, builds the wheel,
and publishes to PyPI via Trusted Publishing (OIDC). It requires a one-time PyPI
"pending publisher" + a GitHub `pypi` environment (see comments in
`release.yml`).

Manual fallback: rebuild the exe (`python scripts/build_exe.py --onefile`, copy
to `turbossh/bin/`), then `python scripts/release.py X.Y.Z`.
