"""scripts/release.py: a release is the commit CI tested, or nothing. GitHub,
PyPI and git are stood in for: nothing here touches the network."""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "release.py"
_spec = importlib.util.spec_from_file_location("release_script", _PATH)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

SHA = "a" * 40
CI = ".github/workflows/ci.yml"


def github(runs, jobs=()):
    """A stand-in for the GitHub API: the runs of a commit, the jobs of a run."""
    asked = []

    def get(url, token=None):
        asked.append(url)
        if "/jobs" in url:
            return {"jobs": list(jobs)}
        return {"workflow_runs": list(runs)}
    get.asked = asked
    return get


def a_run(status="completed", conclusion="success", path=CI, created="2026-11-15T10:00:00Z",
          run_id=1):
    return {"id": run_id, "status": status, "conclusion": conclusion, "path": path,
            "created_at": created, "html_url": f"https://ci/{run_id}"}


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo", "https://github.com/owner/repo.git",
    "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git",
    "https://user@github.com/owner/repo/"])
def test_the_github_repository_is_read_from_the_remote(url):
    assert release.github_repo(url) == ("owner", "repo")


def test_a_remote_elsewhere_is_not_github():
    assert release.github_repo("https://gitlab.com/owner/repo.git") is None


def test_the_changelog_section_of_a_version():
    text = ("# Changelog\n\n## 2.1.10 — later\n\nten\n\n## 2.1.1 — 2026-11-15\n\n"
            "one\n- two\n\n## 2.1.0 — 2026-11-14\n\nzero\n")
    assert release.changelog_section("2.1.1", text) == "one\n- two\n"
    assert release.changelog_section("2.1.0", text) == "zero\n"
    assert release.changelog_section("2.1.2", text) is None


def test_a_bare_or_empty_changelog_heading():
    """The heading pattern ran on into the next line: a bare "## 2.1.2" ate its
    first entry, and an empty one took the next version's notes."""
    text = "## 2.1.2\n- first\n- second\n\n## 2.1.1 — 2026-11-15\n\nold\n"
    assert release.changelog_section("2.1.2", text) == "- first\n- second\n"
    assert release.changelog_section("2.1.2", "## 2.1.2\n## 2.1.1 — x\n\nold\n") is None


def test_ci_status_reads_the_newest_run_of_the_ci_workflow_only():
    runs = [a_run(conclusion="failure", run_id=1, created="2026-11-15T09:00:00Z"),
            a_run(run_id=2, created="2026-11-15T10:00:00Z"),
            a_run(status="in_progress", conclusion=None, path=".github/workflows/x.yml",
                  run_id=3, created="2026-11-15T11:00:00Z")]
    get = github(runs)
    assert release.ci_status("o", "r", SHA, get) == ("success", "https://ci/2", [])
    # the push of this commit to main: a pull request's run tests a merge
    query = get.asked[0]
    for part in (f"head_sha={SHA}", "event=push", "branch=main"):
        assert part in query, part


@pytest.mark.parametrize("run, expected", [
    (None, "missing"),
    (a_run(status="queued", conclusion=None), "running"),
    (a_run(status="in_progress", conclusion=None), "running"),
    (a_run(), "success"),
    (a_run(conclusion="failure"), "failed"),
    (a_run(conclusion="cancelled"), "failed")])
def test_ci_status_of_a_commit(run, expected):
    jobs = [{"name": "gui", "conclusion": "failure"},
            {"name": "build", "conclusion": "success"}]
    state, _, failed = release.ci_status("o", "r", SHA, github([run] if run else [], jobs))
    assert state == expected
    if expected == "failed":
        assert failed == ["gui"]


def test_waiting_for_ci_polls_until_it_passes():
    states = iter([[], [a_run(status="in_progress", conclusion=None)], [a_run()]])
    slept = []

    def get(url, token=None):
        return {"workflow_runs": next(states)}
    url = release.wait_for_ci("o", "r", SHA, True, get, sleep=slept.append,
                              clock=lambda: 0.0)
    assert url == "https://ci/1" and slept == [release.CI_POLL_SECONDS] * 2


def test_without_wait_ci_a_running_ci_stops_the_release():
    with pytest.raises(release.ReleaseError, match="--wait-ci"):
        release.wait_for_ci("o", "r", SHA, False,
                            github([a_run(status="in_progress", conclusion=None)]),
                            sleep=lambda s: None)


def test_a_failed_ci_stops_the_release_and_names_the_jobs():
    get = github([a_run(conclusion="failure")],
                 [{"name": "test (ubuntu-latest, 3.11)", "conclusion": "failure"}])
    with pytest.raises(release.ReleaseError) as err:
        release.wait_for_ci("o", "r", SHA, True, get, sleep=lambda s: None)
    assert "test (ubuntu-latest, 3.11)" in str(err.value)
    assert "Nothing was built or published" in str(err.value)


def test_waiting_for_ci_gives_up_in_the_end():
    clock = iter([0.0, release.CI_WAIT_LIMIT + 1.0])
    with pytest.raises(release.ReleaseError, match="Gave up"):
        release.wait_for_ci("o", "r", SHA, True,
                            github([a_run(status="in_progress", conclusion=None)]),
                            sleep=lambda s: None, clock=lambda: next(clock))


def test_githubs_request_limit_says_what_to_do():
    def get(url, token=None):
        raise urllib.error.HTTPError(url, 403, "rate limited", {}, io.BytesIO())
    with pytest.raises(release.ReleaseError, match="GITHUB_TOKEN"):
        release.wait_for_ci("o", "r", SHA, True, get, sleep=lambda s: None)


def test_pypi_is_asked_whether_the_version_is_there():
    def missing(url, token=None):
        raise urllib.error.HTTPError(url, 404, "nope", {}, io.BytesIO())

    def offline(url, token=None):
        raise urllib.error.URLError("no network")
    assert release.pypi_has("9.9.9", lambda url, token=None: {}) is True
    assert release.pypi_has("9.9.9", missing) is False
    assert release.pypi_has("9.9.9", offline) is None
    asked = []
    release.pypi_has("9.9.9", lambda url, token=None: asked.append(url),
                     index="https://test.pypi.org")
    assert asked == ["https://test.pypi.org/pypi/turbossh/9.9.9/json"]


# ------------------------------------------------------------------ the checkout
def test_the_checkout_must_be_the_commit_and_nothing_more(monkeypatch):
    """The build packs the folder, not git: a file git does not have, or one it
    ignores inside the package, would be published untested."""
    status = (" M turbossh/core.py\n?? turbossh/gui/wip.py\n?? notes.txt\n"
              "!! turbossh/bin/turbossh-gui.exe\n!! turbossh/gui/__pycache__/x.pyc\n"
              "!! turbossh/lab-notes.md\n!! build/\n!! .pytest_cache/\n")
    monkeypatch.setattr(release, "git", lambda *args, check=True, strip=True: status)
    text = "\n".join(release.tree_problems())
    for what in ("M turbossh/core.py", "turbossh/gui/wip.py", "notes.txt",
                 "turbossh/lab-notes.md"):
        assert what in text, what
    for fine in ("turbossh-gui.exe", "__pycache__", "build/", ".pytest_cache"):
        assert fine not in text, fine
    monkeypatch.setattr(release, "git", lambda *args, check=True, strip=True:
                        "!! turbossh/bin/turbossh-gui.exe\n!! dist/\n")
    assert release.tree_problems() == []


def test_files_git_is_told_not_to_check_count_as_changed(monkeypatch):
    listing = {"status": "", "ls-files": "H README.md\nS turbossh/x.py\nh turbossh/y.py\n"}
    monkeypatch.setattr(release, "git", lambda *args, check=True, strip=True:
                        listing[args[0]])
    (problem,) = release.tree_problems()
    assert "turbossh/x.py" in problem and "turbossh/y.py" in problem
    assert "README.md" not in problem


def test_a_checkout_that_changed_during_the_build_is_not_published(monkeypatch):
    head = {"now": SHA}
    monkeypatch.setattr(release, "tree_problems", lambda: [])
    monkeypatch.setattr(release, "git", lambda *args, check=True, strip=True: head["now"])
    release.still_the_release(SHA)
    head["now"] = "f" * 40
    with pytest.raises(release.ReleaseError, match="moved from aaaaaaa to fffffff"):
        release.still_the_release(SHA)
    monkeypatch.setattr(release, "tree_problems", lambda: ["Files git does not have"])
    head["now"] = SHA
    with pytest.raises(release.ReleaseError, match="changed while"):
        release.still_the_release(SHA)


def test_the_exe_must_be_built_after_the_last_change_to_the_package(monkeypatch, tmp_path):
    exe = tmp_path / "turbossh-gui.exe"
    monkeypatch.setattr(release, "EXE", exe)
    changed = {"at": 1_000_000}
    monkeypatch.setattr(release, "git", lambda *args, check=True, strip=True:
                        str(changed["at"]))
    assert "is missing" in release.exe_problem()
    exe.write_bytes(b"MZ")
    import os
    os.utime(exe, (999_000, 999_000))                      # built before the commit
    assert "build it again" in release.exe_problem()
    os.utime(exe, (1_000_500, 1_000_500))                  # after it
    assert release.exe_problem() is None


# ------------------------------------------------------------------ preflight
def _args(**kw):
    base = dict(skip_changelog=False, no_exe=False, no_ci_check=False, wait_ci=False,
                test_pypi=False)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def repo(monkeypatch):
    """A repository where everything is ready for 2.1.2; change what a test needs."""
    state = SimpleNamespace(
        version="2.1.2", branch="main", tree=[], head=SHA, pushed=SHA, tag="",
        remote_tags="", on_pypi=False, exe=None, ci=[], index=[],
        changelog="## 2.1.2 — today\n\nnotes\n")

    def git(*args, check=True, strip=True):
        table = {("rev-parse", "--abbrev-ref", "HEAD"): state.branch,
                 ("rev-parse", "HEAD"): state.head,
                 ("ls-remote", "origin", "refs/heads/main"):
                     f"{state.pushed}\trefs/heads/main" if state.pushed else "",
                 ("rev-parse", "--verify", "-q", "refs/tags/v2.1.2^{commit}"): state.tag,
                 ("ls-remote", "origin", "refs/tags/v2.1.2", "refs/tags/v2.1.2^{}"):
                     state.remote_tags,
                 ("remote", "get-url", "origin"): "https://github.com/owner/repo.git"}
        return table[args]

    def pypi_has(version, get=None, index="https://pypi.org"):
        state.index.append(index)
        return state.on_pypi
    monkeypatch.setattr(release, "git", git)
    monkeypatch.setattr(release, "tree_problems", lambda: list(state.tree))
    monkeypatch.setattr(release, "current_version", lambda: state.version)
    monkeypatch.setattr(release, "changelog_section",
                        lambda v, text=None: "notes\n" if f"## {v}" in state.changelog
                        else None)
    monkeypatch.setattr(release, "pypi_has", pypi_has)
    monkeypatch.setattr(release, "exe_problem", lambda: state.exe)
    monkeypatch.setattr(release, "wait_for_ci",
                        lambda owner, name, sha, wait: state.ci.append((owner, name, sha,
                                                                        wait)))
    return state


def test_a_ready_commit_is_released_once_ci_passed_on_it(repo):
    assert release.preflight("2.1.2", _args(wait_ci=True)) == SHA
    assert repo.ci == [("owner", "repo", SHA, True)]
    assert repo.index == ["https://pypi.org"]


def test_every_reason_not_to_release_is_listed_and_ci_is_not_asked(repo):
    repo.version, repo.branch = "2.1.1", "claude/x"
    repo.tree = ["Uncommitted changes - ...:\n      M turbossh/core.py"]
    repo.pushed, repo.tag, repo.on_pypi = "b" * 40, "c" * 40, True
    repo.exe, repo.changelog = "the exe is old", ""
    with pytest.raises(release.ReleaseError) as err:
        release.preflight("2.1.2", _args())
    text = str(err.value)
    for what in ("says 2.1.1", "no '## 2.1.2' section", "'claude/x'", "turbossh/core.py",
                 "main on GitHub is bbbbbbb", "Tag v2.1.2 here is on ccccccc",
                 "on https://pypi.org already", "the exe is old"):
        assert what in text, what
    assert repo.ci == []


def test_main_missing_on_github_is_not_pushed(repo):
    repo.pushed = ""
    with pytest.raises(release.ReleaseError, match="main on GitHub is missing"):
        release.preflight("2.1.2", _args())


def test_a_test_pypi_release_asks_test_pypi(repo):
    release.preflight("2.1.2", _args(test_pypi=True))
    assert repo.index == ["https://test.pypi.org"]


def test_no_ci_check_skips_only_the_ci_check(repo):
    assert release.preflight("2.1.2", _args(no_ci_check=True)) == SHA
    assert repo.ci == []
    repo.tree = ["Files git does not have - ..."]
    with pytest.raises(release.ReleaseError, match="git does not have"):
        release.preflight("2.1.2", _args(no_ci_check=True))


def test_a_tag_already_on_this_commit_is_fine(repo):
    repo.tag = SHA
    repo.remote_tags = f"{'d' * 40}\trefs/tags/v2.1.2\n{SHA}\trefs/tags/v2.1.2^{{}}\n"
    assert release.preflight("2.1.2", _args()) == SHA


def test_a_tag_on_github_on_another_commit_stops_the_release(repo):
    repo.remote_tags = f"{'e' * 40}\trefs/tags/v2.1.2\n"        # a lightweight tag
    with pytest.raises(release.ReleaseError, match="on GitHub is on eeeeeee"):
        release.preflight("2.1.2", _args())
    assert repo.ci == []


@pytest.mark.parametrize("listing, commit", [
    ("", ""),
    (f"{'1' * 40}\trefs/tags/v9\n", "1" * 40),                           # lightweight
    (f"{'1' * 40}\trefs/tags/v9\n{'2' * 40}\trefs/tags/v9^{{}}\n", "2" * 40)])  # annotated
def test_the_commit_of_a_tag_on_github(monkeypatch, listing, commit):
    monkeypatch.setattr(release, "git", lambda *args, check=True, strip=True: listing)
    assert release.remote_tag_commit("v9") == commit


# ------------------------------------------------------------------ main
@pytest.fixture
def stages(monkeypatch):
    """main() with every stage stood in for; ``ran`` records the commands."""
    ran = []
    monkeypatch.setattr(release, "preflight", lambda v, a: SHA)
    monkeypatch.setattr(release, "build", lambda a: ["d/x.whl", "d/x.tar.gz"])
    monkeypatch.setattr(release, "still_the_release", lambda head: None)
    monkeypatch.setattr(release, "write_notes", lambda v: None)
    monkeypatch.setattr(release, "next_steps", lambda v, h, n: "NEXT STEPS")
    monkeypatch.setattr(release, "run", lambda cmd, **kw: ran.append((cmd, kw)))
    monkeypatch.delenv("TWINE_USERNAME", raising=False)
    return ran


def test_a_release_uploads_what_it_built_and_says_what_is_next(stages, capsys):
    assert release.main(["2.1.2", "--skip-tests"]) == 0
    (cmd, kw), = stages
    assert cmd[-4:] == ["twine", "upload", "d/x.whl", "d/x.tar.gz"]
    assert kw["env"]["TWINE_USERNAME"] == "__token__"     # twine asks for the token
    assert "NEXT STEPS" in capsys.readouterr().out


def test_a_dry_run_uploads_nothing(stages):
    assert release.main(["2.1.2", "--dry-run", "--skip-tests"]) == 0
    assert stages == []


def test_the_tests_run_unless_skipped(stages):
    assert release.main(["2.1.2", "--dry-run"]) == 0
    assert [cmd[1:] for cmd, _ in stages] == [["-m", "pytest", "-q"],
                                              ["tests/test_offline.py"]]


def test_a_checkout_changed_during_the_build_uploads_nothing(stages, monkeypatch, capsys):
    def changed(head):
        raise release.ReleaseError("Not releasing - the checkout changed")
    monkeypatch.setattr(release, "still_the_release", changed)
    assert release.main(["2.1.2", "--skip-tests"]) == 1
    assert stages == [] and "checkout changed" in capsys.readouterr().err


def test_a_stopped_release_builds_nothing(monkeypatch, capsys):
    built = []
    monkeypatch.setattr(release, "preflight", lambda v, a: (_ for _ in ()).throw(
        release.ReleaseError("Not releasing: because")))
    monkeypatch.setattr(release, "build", lambda a: built.append(1))
    assert release.main(["2.1.2"]) == 1
    assert built == [] and "because" in capsys.readouterr().err


def test_the_version_is_given_as_x_y_z(capsys):
    assert release.main(["patch"]) == 2
    assert "X.Y.Z" in capsys.readouterr().err

