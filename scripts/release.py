#!/usr/bin/env python
"""
Release helper for turbossh:  check -> test -> build -> upload.

A release is the commit at the tip of main, exactly as GitHub's CI tested it.
Before anything is built, this checks, and lists everything that is not so:

  * this is main, the checkout is exactly the commit (nothing changed, no file
    git does not have, nothing ignored inside the package but the exe), and
    main is pushed (HEAD is where main is on GitHub);
  * turbossh/__init__.py already says the version - the bump is a commit of
    its own, so CI tests it - and CHANGELOG.md has a "## X.Y.Z" section;
  * the version is not on PyPI yet, and a vX.Y.Z tag, if there is one, is on
    this commit;
  * turbossh/bin/turbossh-gui.exe is there and newer than the last change to
    the package (an exe built before the last commit is not that commit);
  * CI passed on this very commit: every job of .github/workflows/ci.yml for
    its push to main, on Windows and Linux. With --wait-ci it waits while CI
    is still running.

Then it runs the tests, builds the wheel and the sdist, checks them (twine
check, the exe inside the wheel, the checkout unchanged meanwhile) and -
unless --dry-run - uploads them. twine
asks for the PyPI token unless TWINE_PASSWORD is set; the token is never
written down here. At the end it prints the tag and GitHub release steps, and
the release notes are in build/release-notes-X.Y.Z.md.

Usage
-----
    python scripts/release.py 2.1.2 --dry-run --wait-ci   # every check, no upload
    python scripts/release.py 2.1.2 --skip-tests          # the upload, after that

Overrides, for when you know why: --no-ci-check, --no-exe, --skip-changelog,
--skip-tests. GitHub allows 60 anonymous API requests an hour; set GITHUB_TOKEN
(or GH_TOKEN) for more. PyPI never takes a version twice, so a check that stops
a release costs nothing, and a published mistake costs a version number.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "turbossh" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
EXE = ROOT / "turbossh" / "bin" / "turbossh-gui.exe"
EXE_IN_WHEEL = "turbossh/bin/turbossh-gui.exe"
CI_WORKFLOW = ".github/workflows/ci.yml"
PACKAGE = "turbossh"

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
CI_POLL_SECONDS = 60             # 45 polls fit in the anonymous 60 an hour
CI_WAIT_LIMIT = 45 * 60


class ReleaseError(Exception):
    """A reason not to release; the message says what to do about it."""


def run(cmd: list, **kw) -> None:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def git(*args: str, check: bool = True, strip: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if check and out.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    if out.returncode != 0:
        return ""
    return out.stdout.strip() if strip else out.stdout


# Ignored files inside the package that belong in it. Anything else there that
# git does not have would be packed by the build (it reads the folder, not git).
PACKED_THOUGH_IGNORED = ("turbossh/bin/turbossh-gui.exe",)


def tree_problems() -> list:
    """What makes this checkout differ from the commit: changed files, files git
    does not know, and ignored files inside the package (but the exe and
    compiled Python)."""
    problems = []
    status = git("status", "--porcelain", "--ignored", "--untracked-files=all",
                 strip=False)
    changed, unknown, packed = [], [], []
    for line in status.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        if code == "??":
            unknown.append(path)
        elif code == "!!":
            if (path.startswith("turbossh/") and path not in PACKED_THOUGH_IGNORED
                    and "__pycache__/" not in path and not path.endswith(".pyc")):
                packed.append(path)
        elif code.strip():
            changed.append(f"{code.strip()} {path}")
    # git status does not look at these, so a change to one would go unseen
    hidden = [line[2:] for line in git("ls-files", "-v", strip=False).splitlines()
              if line[:1] == "S" or line[:1].islower()]
    for what, paths in (("Uncommitted changes", changed),
                        ("Files git does not have", unknown),
                        ("Ignored files the package would take in", packed),
                        ("Files git is told not to check (skip-worktree or "
                         "assume-unchanged)", hidden)):
        if paths:
            problems.append(f"{what} - the release would not be what CI tested:"
                            + "".join(f"\n      {p}" for p in paths))
    return problems


def current_version() -> str:
    # The version lives ONLY in turbossh/__init__.py; pyproject reads it
    # dynamically (tool.setuptools.dynamic), so there's a single source of truth.
    m = re.search(r'__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"))
    if not m:
        raise ReleaseError("Could not find __version__ in turbossh/__init__.py")
    return m.group(1)


def changelog_section(version: str, text: Optional[str] = None) -> Optional[str]:
    """The body of the "## X.Y.Z" section of CHANGELOG.md, or None (also for
    an empty one)."""
    if text is None:
        try:
            text = CHANGELOG.read_text(encoding="utf-8")
        except OSError:
            return None
    m = re.search(rf"^##[ \t]+\[?{re.escape(version)}\]?(?:[ \t].*)?$", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    end = re.search(r"^## ", rest, re.M)
    body = (rest[:end.start()] if end else rest).strip()
    return body + "\n" if body else None


def github_repo(url: str) -> Optional[tuple]:
    """(owner, repo) of a GitHub remote URL, or None."""
    m = re.match(r"^(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|"
                 r"ssh://git@github\.com/)([^/]+)/(.+?)(?:\.git)?/?$", url.strip())
    return (m.group(1), m.group(2)) if m else None


def _get_json(url: str, token: Optional[str] = None):
    headers = {"User-Agent": "turbossh-release", "Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def ci_status(owner: str, repo: str, sha: str, get: Callable = _get_json,
              token: Optional[str] = None) -> tuple:
    """``(state, url, failed_jobs)`` of the newest run of the CI workflow for
    the push of *sha* to main: state is "success", "running", "failed" or
    "missing". (A pull request's run tests a merge, another tree.)"""
    api = f"https://api.github.com/repos/{owner}/{repo}"
    runs = get(f"{api}/actions/runs?head_sha={sha}&event=push&branch=main&per_page=100",
               token)
    runs = [r for r in runs.get("workflow_runs", [])
            if (r.get("path") or "").split("@")[0] == CI_WORKFLOW]
    if not runs:
        return "missing", None, []
    newest = max(runs, key=lambda r: (r.get("created_at") or "", r.get("id") or 0))
    url = newest.get("html_url")
    if newest.get("status") != "completed":
        return "running", url, []
    if newest.get("conclusion") == "success":
        return "success", url, []
    jobs = get(f"{api}/actions/runs/{newest['id']}/jobs?per_page=100", token)
    failed = [j.get("name", "?") for j in jobs.get("jobs", [])
              if j.get("conclusion") not in ("success", "skipped")]
    return "failed", url, failed or [f"the run was {newest.get('conclusion')}"]


def wait_for_ci(owner: str, repo: str, sha: str, wait: bool, get: Callable = _get_json,
                sleep: Callable = time.sleep, clock: Callable = time.monotonic) -> str:
    """Return the CI run's URL once CI passed on *sha*; raise ReleaseError if it
    failed, or is not done and *wait* is false (or it takes too long)."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    deadline = clock() + CI_WAIT_LIMIT
    while True:
        try:
            state, url, failed = ci_status(owner, repo, sha, get, token)
        except urllib.error.HTTPError as exc:
            hint = (" GitHub allows 60 anonymous requests an hour: wait, or set "
                    "GITHUB_TOKEN." if exc.code in (403, 429) else "")
            raise ReleaseError(f"Could not ask GitHub about CI (HTTP {exc.code}).{hint} "
                               f"--no-ci-check releases without asking.") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ReleaseError(f"Could not ask GitHub about CI ({exc}). "
                               f"--no-ci-check releases without asking.") from None
        if state == "success":
            print(f"CI passed on {sha[:7]}: {url}")
            return url
        if state == "failed":
            raise ReleaseError(
                f"CI failed on {sha[:7]} - {', '.join(failed)}:\n    {url}\n"
                f"Nothing was built or published. Fix it in a new commit, push, "
                f"and run this again.")
        if state == "missing":
            note = (f"GitHub has no CI run for {sha[:7]} yet (it appears within a "
                    f"minute of the push).")
        else:
            note = f"CI is still running on {sha[:7]}: {url}"
        if not wait:
            raise ReleaseError(f"{note}\nRun this again when it has passed, or add "
                               f"--wait-ci to wait for it.")
        if clock() >= deadline:
            raise ReleaseError(f"{note}\nGave up after {CI_WAIT_LIMIT // 60} minutes.")
        print(f"{note} Checking again in {CI_POLL_SECONDS} s.", flush=True)
        sleep(CI_POLL_SECONDS)


def pypi_has(version: str, get: Callable = _get_json,
             index: str = "https://pypi.org") -> Optional[bool]:
    """Is *version* on PyPI (or *index*)? None when it could not be asked."""
    try:
        get(f"{index}/pypi/{PACKAGE}/{version}/json")
        return True
    except urllib.error.HTTPError as exc:
        return False if exc.code == 404 else None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def remote_main() -> str:
    """The commit main is on at origin, asked of origin itself (a local
    origin/main can be stale)."""
    out = git("ls-remote", "origin", "refs/heads/main")
    return out.split("\t", 1)[0] if out else ""


def still_the_release(head: str) -> None:
    """After the tests and the build: the checkout is still that commit, with
    nothing added (an edit meanwhile would be built and published)."""
    problems = tree_problems()
    now = git("rev-parse", "HEAD")
    if now != head:
        problems.append(f"HEAD moved from {head[:7]} to {now[:7]} meanwhile.")
    if problems:
        raise ReleaseError("Not releasing - the checkout changed while it was being "
                           "tested and built:\n" + "\n".join(f"  - {p}" for p in problems))


def remote_tag_commit(tag: str) -> str:
    """The commit *tag* is on at origin ("" when there is no such tag)."""
    out = git("ls-remote", "origin", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    refs = dict(reversed(line.split("\t", 1)) for line in out.splitlines() if "\t" in line)
    # an annotated tag's commit is the peeled ^{} line; a lightweight one has none
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}", "")


def exe_problem() -> Optional[str]:
    if not EXE.is_file():
        return (f"{EXE} is missing: build it from this commit "
                f"(python scripts/build_exe.py --onefile) and copy "
                f"dist/turbossh-gui.exe there.")
    changed = int(git("log", "-1", "--format=%ct", "--", "turbossh") or 0)
    built = EXE.stat().st_mtime
    if built + 1 < changed:
        return (f"{EXE} was built {time.ctime(built)}, before the last change to the "
                f"package ({time.ctime(changed)}): build it again from this commit.")
    return None


def preflight(version: str, args) -> str:
    """Every reason not to release, at once; then CI. Returns HEAD's sha."""
    problems = []
    current = current_version()
    if current != version:
        problems.append(
            f'turbossh/__init__.py says {current}. Set __version__ = "{version}" in a '
            f"commit of its own (with the changelog), push it, and let CI pass.")
    if not args.skip_changelog and changelog_section(version) is None:
        problems.append(f"CHANGELOG.md has no '## {version}' section.")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        problems.append(f"This is {branch!r}: a release is made from main.")
    problems += tree_problems()
    head = git("rev-parse", "HEAD")
    pushed = remote_main()
    if head != pushed:
        problems.append(f"main is not pushed as it is: HEAD is {head[:7]}, main on GitHub "
                        f"is {pushed[:7] or 'missing'}. Push (git push origin main) "
                        f"and let CI run on it.")
    tag = f"v{version}"
    local_tag = git("rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}",
                    check=False)
    for where, on in (("here", local_tag), ("on GitHub", remote_tag_commit(tag))):
        if on and on != head:
            problems.append(f"Tag {tag} {where} is on {on[:7]}, not on this commit.")
    index = "https://test.pypi.org" if getattr(args, "test_pypi", False) \
        else "https://pypi.org"
    on_pypi = pypi_has(version, index=index)
    if on_pypi:
        problems.append(f"{PACKAGE} {version} is on {index} already, and it never takes a "
                        f"version twice: release a new version. (If an upload of it "
                        f"stopped half-way, finish it with twine upload --skip-existing.)")
    elif on_pypi is None:
        print(f"WARNING: could not ask {index} whether {version} is there already.")
    if not args.no_exe:
        problem = exe_problem()
        if problem:
            problems.append(problem)
    if problems:
        raise ReleaseError("Not releasing:\n" + "\n".join(f"  - {p}" for p in problems))
    if args.no_ci_check:
        print("WARNING: --no-ci-check: releasing without knowing whether CI passed.")
        return head
    repo = github_repo(git("remote", "get-url", "origin"))
    if not repo:
        raise ReleaseError("origin is not a GitHub repository, so CI cannot be asked; "
                           "--no-ci-check releases without asking.")
    wait_for_ci(repo[0], repo[1], head, args.wait_ci)
    return head


def wheel_has_exe() -> bool:
    """Does the freshly built wheel carry the bundled GUI executable?"""
    import zipfile
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if not wheels:
        return False
    with zipfile.ZipFile(wheels[-1]) as wheel:
        return EXE_IN_WHEEL in wheel.namelist()


def _clean(path: Path) -> None:
    if path.exists():
        for f in sorted(path.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        path.rmdir()


def build(args) -> list:
    """Clean, build, check. Returns the distribution files."""
    for d in ("dist", "build"):
        _clean(ROOT / d)
    for egg in ROOT.glob("*.egg-info"):
        _clean(egg)
    run([sys.executable, "-m", "build"])
    dists = sorted(str(p) for p in (ROOT / "dist").iterdir()
                   if p.name.endswith((".whl", ".tar.gz")))
    run([sys.executable, "-m", "twine", "check", *dists])
    # The wheel is expected to carry the Windows GUI exe (it is git-ignored, so
    # nothing else would notice it missing until users could not start the GUI).
    if not args.no_exe and not wheel_has_exe():
        raise ReleaseError(f"The wheel does not contain {EXE_IN_WHEEL}.")
    return dists


def write_notes(version: str) -> Optional[Path]:
    body = changelog_section(version)
    if body is None:
        return None
    notes = ROOT / "build" / f"release-notes-{version}.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(body, encoding="utf-8")
    return notes


def next_steps(version: str, head: str, notes: Optional[Path]) -> str:
    repo = github_repo(git("remote", "get-url", "origin", check=False)) or ("OWNER", "REPO")
    lines = [f'  git -C "{ROOT}" tag -a v{version} -m "TurboSSH {version}" {head[:7]}',
             f'  git -C "{ROOT}" push origin v{version}',
             f"  GitHub release: https://github.com/{repo[0]}/{repo[1]}/releases/new"
             f"?tag=v{version}&title=TurboSSH%20{version}"]
    if notes:
        lines.append(f"    notes:  {notes}")
    if EXE.is_file():
        digest = hashlib.sha256(EXE.read_bytes()).hexdigest().upper()
        lines.append(f"    attach: {EXE}  (SHA256 {digest[:16]}...)")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check, build and publish turbossh.")
    ap.add_argument("version", help="X.Y.Z: the version turbossh/__init__.py says")
    ap.add_argument("--dry-run", action="store_true",
                    help="every check, the tests and the build; no upload")
    ap.add_argument("--wait-ci", action="store_true",
                    help="wait while CI is still running on this commit")
    ap.add_argument("--test-pypi", action="store_true",
                    help="upload to TestPyPI instead of PyPI")
    ap.add_argument("--skip-tests", action="store_true",
                    help="skip the local test run (CI is still checked)")
    ap.add_argument("--no-ci-check", action="store_true",
                    help="release without checking CI (an emergency only)")
    ap.add_argument("--skip-changelog", action="store_true",
                    help="release even if CHANGELOG.md has no section for it")
    ap.add_argument("--no-exe", action="store_true",
                    help="allow a wheel without the bundled Windows GUI exe")
    args = ap.parse_args(argv)
    version = args.version.strip()
    if not VERSION_RE.match(version):
        print("Give the version as X.Y.Z - the one turbossh/__init__.py says.",
              file=sys.stderr)
        return 2
    uploading = False
    try:
        head = preflight(version, args)
        print(f"\nReleasing {PACKAGE} {version} from {head[:7]}.")
        if not args.skip_tests:
            run([sys.executable, "-m", "pytest", "-q"])
            run([sys.executable, "tests/test_offline.py"])
        dists = build(args)
        still_the_release(head)
        notes = write_notes(version)
        if args.dry_run:
            print(f"\n--dry-run: every check passed and {len(dists)} files are built "
                  f"in {ROOT / 'dist'}. To publish:\n"
                  f"  python \"{Path(__file__).resolve()}\" {version} --skip-tests")
            return 0
        env = dict(os.environ)
        env.setdefault("TWINE_USERNAME", "__token__")   # twine asks for the token
        cmd = [sys.executable, "-m", "twine", "upload"]
        if args.test_pypi:
            cmd += ["--repository", "testpypi"]
        uploading = True
        run(cmd + dists, env=env)
        target = "TestPyPI" if args.test_pypi else "PyPI"
        print(f"\nPublished {PACKAGE} {version} to {target}. Next:\n"
              + next_steps(version, head, notes))
        return 0
    except ReleaseError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        what = ("Check PyPI: part of the upload may have gone through. To finish "
                "it: python -m twine upload --skip-existing dist/*"
                if uploading else "Nothing was published.")
        print(f"\nStopped: {' '.join(map(str, exc.cmd))} failed (exit {exc.returncode})."
              f" {what}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
