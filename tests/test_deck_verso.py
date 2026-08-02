import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import marathon.deck.verso as verso
from marathon.deck.verso import (
    VersoArtifactManager,
    build_verso_manifest,
    discover_verso_project,
    effective_artifact_token,
    safe_artifact_path,
    verso_source_token,
    verso_tag_from_toolchain,
)
from marathon.lake_build_lock import (
    LakeBuildLockTimeout,
    lake_build_lock,
    lake_build_lock_path,
)


TOKEN_A = "a" * 64
TOKEN_B = "b" * 64
TOKEN_C = "c" * 64
TOKEN_D = "d" * 64
TOKEN_E = "e" * 64
TOKEN_F = "f" * 64
TOKEN_G = "0" * 64


def _project(tmp_path: Path, *, toolchain: str = "leanprover/lean4:v4.28.0") -> Path:
    repo = tmp_path / "reviewed"
    repo.mkdir()
    (repo / "lean-toolchain").write_text(toolchain + "\n")
    (repo / "lakefile.toml").write_text(
        'name = "DemoPackage"\n\n[[lean_lib]]\nname = "Demo"\n'
    )
    (repo / "Demo.lean").write_text("def Demo.answer : Nat := 42\n")
    return repo


def _write_site(site: Path) -> None:
    page = site / "Demo" / "Main" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        """<!doctype html>
<html><head><title>Demo.Main</title></head><body>
<span class="const token" data-binding="const-Demo.answer"
      id="Demo.answer">answer</span>
<span class="const token" data-binding="const-Demo.answer">answer</span>
<span class="var token" data-binding="var-1" id="not-a-constant">x</span>
</body></html>
"""
    )
    (site / "code.css").write_text(".const { color: blue; }\n")


class FakeVersoRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.fail_build = False

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.environments.append(dict(kwargs["env"]))
        root = Path(kwargs["cwd"])
        if command[1] == "update":
            (root / "lake-manifest.json").write_text(
                '{"version": "1.1.0", "packages": []}\n'
            )
        elif command[1] == "build":
            if self.fail_build:
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="elaboration failed"
                )
            literate = root / ".lake" / "build" / "literate"
            literate.mkdir(parents=True, exist_ok=True)
            (literate / "Demo.json").write_text("{}")
            for target in command[2:]:
                if "/+" not in target or not target.endswith(":literate"):
                    continue
                module = target.split("/+", 1)[1].removesuffix(":literate")
                output = literate / (module.replace(".", "/") + ".json")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("{}")
        elif command[1:3] == ["exe", "verso-html"]:
            assert Path(command[3]).is_dir()
            _write_site(Path(command[-1]))
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


def test_discovers_matching_stable_verso_tag(tmp_path):
    repo = _project(tmp_path)
    assert verso_tag_from_toolchain(repo) == "v4.28.0"
    assert discover_verso_project(repo).library == "Demo"
    assert discover_verso_project(repo).package == "DemoPackage"

    (repo / "lean-toolchain").write_text("leanprover/lean4:nightly-2026-07-01\n")
    manager = VersoArtifactManager(repo, runner=FakeVersoRunner())
    status = manager.status()
    assert status["state"] == "unavailable"
    assert status["available"] is False
    assert "stable lean-toolchain" in status["error"]


def test_lake_environment_adds_elan_for_minimal_gui_service_path(tmp_path):
    home = tmp_path / "home"
    lake = home / ".elan" / "bin" / "lake"
    lake.parent.mkdir(parents=True)
    lake.write_text("#!/bin/sh\n")
    lake.chmod(0o755)

    env = verso._lake_environment({"PATH": "/usr/bin"}, home=home)
    assert env["PATH"].split(verso.os.pathsep) == [
        str(lake.parent), "/usr/bin"
    ]
    assert verso.shutil.which("lake", path=env["PATH"]) == str(lake)
    assert verso._lake_environment(env, home=home)["PATH"] == env["PATH"]


def test_semantic_source_identity_uses_content_not_head_or_mtime(tmp_path):
    repo = _project(tmp_path)
    source = repo / "Demo.lean"
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
    first = verso_source_token(repo)

    stat = source.stat()
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    assert verso_source_token(repo) == first

    (repo / "README.md").write_text("metadata only\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "metadata"], cwd=repo, check=True)
    assert verso_source_token(repo) == first

    source.write_text("def Demo.answer : Nat := 43\n")
    assert verso_source_token(repo) != first


def test_semantic_source_identity_includes_file_inventory(tmp_path):
    repo = _project(tmp_path)
    first = verso_source_token(repo)
    extra = repo / "Demo" / "Extra.lean"
    extra.parent.mkdir()
    extra.write_text("def Demo.Extra.answer : Nat := 42\n")
    assert verso_source_token(repo) != first


def test_staged_literate_math_is_scoped_restored_and_escaped(tmp_path):
    source = tmp_path / "persistent-literate"
    source.mkdir()
    payload = {
        "items": {
            "items": [
                {
                    "code": [
                        {
                            "markdown": [
                                0,
                                "Demo.answer",
                                {
                                    "blocks": [
                                        {
                                            "p": [
                                                {"normal": "For "},
                                                {
                                                    "latexMath": [
                                                        "x < y & ",
                                                        "</code><script>bad()</script>",
                                                    ]
                                                },
                                                {
                                                    "latexMathDisplay": [
                                                        r"\int_0^1 x\,dx"
                                                    ]
                                                },
                                            ]
                                        }
                                    ]
                                },
                            ]
                        },
                        {
                            "markdownModDoc": {
                                "blocks": [
                                    {"p": [{"latexMath": ["z", "^2"]}]}
                                ]
                            }
                        },
                        {"goals": {"latexMath": ["not Markdown"]}},
                    ]
                }
            ]
        },
        "outsideMarkdown": {"latexMathDisplay": ["also untouched"]},
    }
    original = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    persistent_json = source / "Demo.json"
    persistent_json.write_text(original, encoding="utf-8")
    (source / "asset.txt").write_text("copied", encoding="utf-8")

    staged = tmp_path / "work" / "literate"
    nonce = "1234567890abcdef1234567890abcdef"
    markers = verso._stage_literate_for_html(source, staged, nonce)

    assert persistent_json.read_text(encoding="utf-8") == original
    assert (staged / "asset.txt").read_text(encoding="utf-8") == "copied"
    assert list(markers.values()) == [
        ("inline", "x < y & </code><script>bad()</script>"),
        ("display", r"\int_0^1 x\,dx"),
        ("inline", "z^2"),
    ]
    assert all(
        re.fullmatch(
            rf"__MARATHON_VERSO_MATH_{nonce}_[ID]_[0-9]+__", marker
        )
        for marker in markers
    )

    rewritten = json.loads((staged / "Demo.json").read_text(encoding="utf-8"))
    code = rewritten["items"]["items"][0]["code"]
    paragraph = code[0]["markdown"][2]["blocks"][0]["p"]
    inline_marker, display_marker, module_marker = markers
    assert paragraph[1] == {"code": [inline_marker]}
    assert paragraph[2] == {"code": [display_marker]}
    assert code[1]["markdownModDoc"]["blocks"][0]["p"][0] == {
        "code": [module_marker]
    }
    assert code[2]["goals"] == {"latexMath": ["not Markdown"]}
    assert rewritten["outsideMarkdown"] == {
        "latexMathDisplay": ["also untouched"]
    }

    site = tmp_path / "site"
    page = site / "Demo" / "index.html"
    page.parent.mkdir(parents=True)
    page.write_text(
        "<p>"
        f"<code>{inline_marker}</code>"
        f"<code>{display_marker}</code>"
        f"<code>{module_marker}</code>"
        "<code>ordinary</code>"
        "</p>",
        encoding="utf-8",
    )

    assert verso._restore_rendered_math(site, markers) == 3
    html = page.read_text(encoding="utf-8")
    assert (
        '<code class="math inline">'
        "x &lt; y &amp; &lt;/code&gt;&lt;script&gt;bad()&lt;/script&gt;"
        "</code>"
    ) in html
    assert (
        '<code class="math display">\\int_0^1 x\\,dx</code>' in html
    )
    assert '<code class="math inline">z^2</code>' in html
    assert "<script>" not in html
    assert "<code>ordinary</code>" in html


def test_image_alt_math_is_normalized_without_creating_a_marker(tmp_path):
    source = tmp_path / "persistent-literate"
    source.mkdir()
    payload = {
        "items": {
            "items": [
                {
                    "code": [
                        {
                            "markdown": {
                                "blocks": [
                                    {
                                        "p": [
                                            {
                                                "img": [
                                                    [{"normal": "plot.svg"}],
                                                    [],
                                                    [
                                                        {"normal": "Plot of "},
                                                        {"latexMath": ["f", "(x)"]},
                                                    ],
                                                ]
                                            },
                                            {"normal": " where "},
                                            {"latexMath": ["x", "^2"]},
                                        ]
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    }
    (source / "Demo.json").write_text(json.dumps(payload), encoding="utf-8")

    staged = tmp_path / "work" / "literate"
    nonce = "1234567890abcdef1234567890abcdef"
    markers = verso._stage_literate_for_html(source, staged, nonce)

    assert len(markers) == 1
    assert list(markers.values()) == [("inline", "x^2")]
    rewritten = json.loads((staged / "Demo.json").read_text(encoding="utf-8"))
    paragraph = rewritten["items"]["items"][0]["code"][0]["markdown"][
        "blocks"
    ][0]["p"]
    assert paragraph[0]["img"][2] == [
        {"normal": "Plot of "},
        {"normal": "f(x)"},
    ]
    assert paragraph[2] == {"code": [next(iter(markers))]}


def test_rendered_math_marker_mismatch_fails_closed(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text("<p>formula disappeared</p>")
    marker = (
        "__MARATHON_VERSO_MATH_"
        "1234567890abcdef1234567890abcdef_I_0__"
    )

    with pytest.raises(
        verso.VersoBuildError,
        match=r"expected 1 exact markers; missing 1, duplicated 0",
    ):
        verso._restore_rendered_math(site, {marker: ("inline", "x")})


def test_rendered_math_marker_mismatch_reports_missing_and_duplicates(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    nonce = "1234567890abcdef1234567890abcdef"
    first = f"__MARATHON_VERSO_MATH_{nonce}_I_0__"
    second = f"__MARATHON_VERSO_MATH_{nonce}_D_1__"
    (site / "index.html").write_text(
        f"<code>{first}</code><code>{first}</code>", encoding="utf-8"
    )

    with pytest.raises(
        verso.VersoBuildError,
        match=r"expected 2 exact markers; missing 1, duplicated 1",
    ):
        verso._restore_rendered_math(
            site,
            {first: ("inline", "x"), second: ("display", "y")},
        )


def test_missing_platform_lock_reports_precise_unavailable(tmp_path, monkeypatch):
    repo = _project(tmp_path)
    runner = FakeVersoRunner()
    monkeypatch.setattr(verso, "_fcntl", None)
    manager = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: TOKEN_A
    )
    status = manager.status()
    assert status["state"] == "unavailable"
    assert "POSIX flock" in status["error"]
    assert "raw-source fallback" in status["error"]
    assert runner.commands == []


def test_builds_nested_overlay_and_semantic_manifest(tmp_path):
    repo = _project(tmp_path)
    unimported = repo / "Demo" / "Chapter15" / "Hidden.lean"
    unimported.parent.mkdir(parents=True)
    unimported.write_text("def Demo.Chapter15.hidden : Nat := 15\n")
    original_lakefile = (repo / "lakefile.toml").read_text()
    original_toolchain = (repo / "lean-toolchain").read_text()
    runner = FakeVersoRunner()
    manager = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: TOKEN_A
    )

    assert manager.status()["state"] == "building"
    assert manager.wait(2)
    status = manager.status(trigger=False)
    artifact_token = effective_artifact_token(
        TOKEN_A, manager._dependency_signature()
    )
    assert status == {
        "state": "ready",
        "available": True,
        "stale": False,
        "source_token": TOKEN_A,
        "artifact_token": artifact_token,
        "error": None,
        "verso_tag": "v4.28.0",
        "library": "Demo",
    }

    assert runner.commands[0] == ["lake", "update"]
    assert runner.commands[1] == [
        "lake",
        "build",
        "Demo:literate",
        "@DemoPackage/+Demo:literate",
        "@DemoPackage/+Demo.Chapter15.Hidden:literate",
    ]
    assert runner.commands[2][:3] == ["lake", "exe", "verso-html"]
    assert Path(runner.commands[2][3]).name == "literate"
    assert Path(runner.commands[2][-1]).name == "site"
    assert Path(runner.commands[2][3]).parent == Path(runner.commands[2][-1]).parent
    assert Path(runner.commands[2][-1]).parent.name.startswith(artifact_token + "-")
    assert all(env["LEAN_NUM_THREADS"] == "2" for env in runner.environments)
    assert manager.root == repo / ".lake" / "marathon-verso"
    assert (manager.root / ".lake" / "packages").resolve() == (
        repo / ".lake" / "packages"
    ).resolve()
    overlay = (manager.root / "lakefile.toml").read_text()
    assert 'path = "../.."' in overlay
    assert 'rev = "v4.28.0"' in overlay
    assert (repo / "lakefile.toml").read_text() == original_lakefile
    assert (repo / "lean-toolchain").read_text() == original_toolchain

    manifest = manager.manifest(trigger=False)
    assert manifest == {
        "artifact_token": artifact_token,
        "root_url": f"/verso/{artifact_token}/",
        "constants": {
            "Demo.answer": {"url": "Demo/Main/", "anchor": "Demo.answer"}
        },
        "modules": {"Demo.Main": {"url": "Demo/Main/"}},
    }
    assert manager.resolve_artifact("Demo/Main/").name == "index.html"
    assert manager.resolve_artifact("code.css").read_text().startswith(".const")
    assert not (manager.root / "artifacts" / artifact_token / "literate").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited flock")
def test_build_holds_repo_scoped_lake_lock_for_every_command(tmp_path):
    repo = _project(tmp_path)
    observed_fds: list[int] = []

    class LockCheckingRunner(FakeVersoRunner):
        def __call__(self, command, **kwargs):
            (lock_fd,) = kwargs["pass_fds"]
            assert os.fstat(lock_fd).st_ino == os.stat(
                lake_build_lock_path(repo)
            ).st_ino
            with lake_build_lock(repo, blocking=False) as contender:
                assert contender is None
            observed_fds.append(lock_fd)
            return super().__call__(command, **kwargs)

    manager = VersoArtifactManager(
        repo,
        runner=LockCheckingRunner(),
        source_token_fn=lambda _repo: TOKEN_A,
    )
    manager.trigger()

    assert manager.wait(2)
    assert manager.status(trigger=False)["state"] == "ready"
    assert len(observed_fds) == 3  # update, literate build, HTML render
    assert len(set(observed_fds)) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited flock")
def test_lock_timeout_retries_unchanged_token_when_contention_clears(
    tmp_path, monkeypatch
):
    repo = _project(tmp_path)
    runner = FakeVersoRunner()
    timed_out = threading.Event()

    @contextmanager
    def observed_lock(repo_dir, **kwargs):
        try:
            with lake_build_lock(repo_dir, **kwargs) as lock_fd:
                yield lock_fd
        except LakeBuildLockTimeout:
            timed_out.set()
            raise

    monkeypatch.setattr(verso, "lake_build_lock", observed_lock)
    monkeypatch.setattr(verso, "_BUILD_TIMEOUT_SECONDS", 0.02)

    with lake_build_lock(repo):
        manager = VersoArtifactManager(
            repo,
            runner=runner,
            source_token_fn=lambda _repo: TOKEN_A,
        )
        assert manager.trigger()["state"] == "building"
        assert timed_out.wait(1)
        status = manager.status(trigger=False)
        assert status["state"] == "building"
        assert status["error"] is None
        assert manager._failed_token is None
        assert runner.commands == []

    assert manager.wait(2)
    assert manager.status(trigger=False)["state"] == "ready"
    assert manager._failed_token is None
    assert [command[1] for command in runner.commands] == [
        "update",
        "build",
        "exe",
    ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX inherited flock")
def test_shutdown_cancels_build_waiting_for_shared_lock(tmp_path, monkeypatch):
    repo = _project(tmp_path)
    runner = FakeVersoRunner()
    waiting = threading.Event()

    @contextmanager
    def observed_lock(repo_dir, **kwargs):
        if kwargs.get("blocking"):
            waiting.set()
        with lake_build_lock(repo_dir, **kwargs) as lock_fd:
            yield lock_fd

    monkeypatch.setattr(verso, "lake_build_lock", observed_lock)

    with lake_build_lock(repo):
        manager = VersoArtifactManager(
            repo,
            runner=runner,
            source_token_fn=lambda _repo: TOKEN_A,
        )
        manager.trigger()
        assert waiting.wait(1)
        started = time.monotonic()
        manager.shutdown(wait=True)
        elapsed = time.monotonic() - started

    assert elapsed < 1
    assert manager.wait(0.1)
    assert runner.commands == []


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
def test_timeout_kills_grandchild_before_shared_lock_is_released(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pid_file = tmp_path / "grandchild.pid"
    manager = object.__new__(VersoArtifactManager)
    manager._check_lake = True
    manager._process_env = os.environ.copy()
    manager.root = tmp_path
    child_pid = 0

    try:
        with lake_build_lock(repo) as lock_fd:
            assert lock_fd is not None
            parent_code = f"""
import pathlib
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"],
    pass_fds=({lock_fd},),
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
time.sleep(60)
"""
            with pytest.raises(verso.VersoBuildError, match="timed out"):
                manager._run_command(
                    [sys.executable, "-c", parent_code],
                    0.5,
                    lock_fd=lock_fd,
                )

        child_pid = int(pid_file.read_text())
        with lake_build_lock(repo, blocking=False) as next_owner:
            assert next_owner is not None
    finally:
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_failed_build_diagnostic_keeps_command_exit_cause_and_tail(tmp_path):
    repo = _project(tmp_path)
    first = "FIRST: compiler crashed while elaborating Demo.answer"
    last = "LAST: final worker warning"

    def failed(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 139, stdout="", stderr=first + "\n" + ("x" * 8000) + "\n" + last
        )

    manager = VersoArtifactManager(
        repo, runner=failed, source_token_fn=lambda _repo: TOKEN_A
    )
    with lake_build_lock(repo) as lock_fd:
        with pytest.raises(verso.VersoBuildError) as caught:
            manager._run_command(
                ["lake", "build", "@DemoPackage/+Demo:literate"],
                1,
                lock_fd=lock_fd,
            )

    message = str(caught.value)
    assert "lake build @DemoPackage/+Demo:literate" in message
    assert "exited 139" in message
    assert first in message
    assert "output clipped" in message
    assert last in message
    assert len(message) <= verso._MAX_ERROR_CHARS


def test_non_lake_diagnostic_never_echoes_payload_argument():
    message = verso._process_failure_message(
        ["python", "-c", "SECRET_PAYLOAD"], 1, "safe failure"
    )
    assert "python" in message
    assert "SECRET_PAYLOAD" not in message


def test_artifact_build_restores_math_without_mutating_incremental_cache(tmp_path):
    repo = _project(tmp_path)
    payload = {
        "items": {
            "items": [
                {
                    "code": [
                        {
                            "markdown": [
                                0,
                                "Demo.answer",
                                {
                                    "blocks": [
                                        {
                                            "p": [
                                                {"latexMath": ["x", " < y"]},
                                                {
                                                    "latexMathDisplay": [
                                                        "z & </code>"
                                                    ]
                                                },
                                            ]
                                        }
                                    ]
                                },
                            ]
                        }
                    ]
                }
            ]
        },
        "module": "Demo",
    }
    persistent_text = json.dumps(payload, ensure_ascii=False) + "\n"

    class MathRunner(FakeVersoRunner):
        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            root = Path(kwargs["cwd"])
            if command[1] == "build":
                persistent = root / ".lake" / "build" / "literate" / "Demo.json"
                persistent.write_text(persistent_text, encoding="utf-8")
            elif command[1:3] == ["exe", "verso-html"]:
                staged_text = Path(command[3]).joinpath("Demo.json").read_text(
                    encoding="utf-8"
                )
                markers = re.findall(
                    r"__MARATHON_VERSO_MATH_[0-9a-f]{32}_[ID]_[0-9]+__",
                    staged_text,
                )
                assert len(markers) == 2
                persistent = root / ".lake" / "build" / "literate" / "Demo.json"
                assert persistent.read_text(encoding="utf-8") == persistent_text
                page = Path(command[-1]) / "Demo" / "Main" / "index.html"
                html = page.read_text(encoding="utf-8")
                html = html.replace(
                    "</body>",
                    "".join(f"<code>{marker}</code>" for marker in markers)
                    + "</body>",
                )
                page.write_text(html, encoding="utf-8")
            return result

    manager = VersoArtifactManager(
        repo, runner=MathRunner(), source_token_fn=lambda _repo: TOKEN_A
    )
    manager.trigger()
    assert manager.wait(2)
    assert manager.status(trigger=False)["state"] == "ready"

    persistent = manager.root / ".lake" / "build" / "literate" / "Demo.json"
    assert persistent.read_text(encoding="utf-8") == persistent_text
    page = manager.resolve_artifact("Demo/Main/").read_text(encoding="utf-8")
    assert '<code class="math inline">x &lt; y</code>' in page
    assert (
        '<code class="math display">z &amp; &lt;/code&gt;</code>' in page
    )
    artifact = manager.root / "artifacts" / manager.manifest(trigger=False)[
        "artifact_token"
    ]
    assert not (artifact / "literate").exists()


def test_cache_is_keyed_by_source_and_failure_keeps_last_success(tmp_path):
    repo = _project(tmp_path)
    source = [TOKEN_A]
    runner = FakeVersoRunner()
    manager = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: source[0]
    )
    manager.trigger()
    assert manager.wait(2)
    first_manifest = manager.manifest(trigger=False)
    artifact_a = first_manifest["artifact_token"]

    source[0] = TOKEN_B
    stale = manager.status(trigger=False)
    assert stale["state"] == "stale"
    assert stale["available"] is True
    assert stale["stale"] is True
    assert stale["artifact_token"] == artifact_a

    runner.fail_build = True
    manager.trigger()
    assert manager.wait(2)
    failed = manager.status(trigger=False)
    assert failed["state"] == "error"
    assert failed["available"] is True
    assert failed["stale"] is True
    assert failed["artifact_token"] == artifact_a
    assert "elaboration failed" in failed["error"]
    assert manager.manifest(trigger=False) == first_manifest
    calls_after_failure = len(runner.commands)
    for _ in range(5):
        assert manager.trigger()["state"] == "error"
    assert len(runner.commands) == calls_after_failure

    # Both the last good pointer and the failed-token explanation survive a
    # deck restart; no subprocess is needed to report either honestly.
    calls = len(runner.commands)
    restarted = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: TOKEN_B
    )
    assert restarted.status(trigger=False)["state"] == "error"
    assert len(runner.commands) == calls


def test_concurrent_triggers_are_single_flight(tmp_path):
    repo = _project(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingRunner(FakeVersoRunner):
        def __call__(self, command, **kwargs):
            if command[1] == "build":
                started.set()
                assert release.wait(2)
            return super().__call__(command, **kwargs)

    runner = BlockingRunner()
    manager = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: TOKEN_A
    )
    manager.trigger()
    assert started.wait(1)
    for _ in range(20):
        assert manager.trigger()["state"] == "building"
    release.set()
    assert manager.wait(2)
    assert [cmd[1] for cmd in runner.commands].count("build") == 1
    assert manager.status(trigger=False)["state"] == "ready"


def test_incremental_literate_cache_prunes_only_invalid_modules(tmp_path):
    repo = _project(tmp_path)
    (repo / "Demo").mkdir()
    (repo / "Demo" / "Keep.lean").write_text("def Demo.Keep.ok := true\n")
    (repo / "Demo" / "Zero.lean").write_text("def Demo.Zero.ok := true\n")
    root = repo / ".lake" / "marathon-verso"
    literate = root / ".lake" / "build" / "literate" / "Demo"
    literate.mkdir(parents=True)
    keep = literate / "Keep.json"
    stale = literate / "Gone.json"
    zero = literate / "Zero.json"
    keep.write_text('{"module": "keep"}')
    stale.write_text('{"module": "gone"}')
    zero.write_text("")

    class InspectingRunner(FakeVersoRunner):
        def __call__(self, command, **kwargs):
            if command[1] == "build":
                assert keep.read_text() == '{"module": "keep"}'
                assert not stale.exists()
                assert not zero.exists()
            if command[1:3] == ["exe", "verso-html"]:
                assert keep.is_file()
            return super().__call__(command, **kwargs)

    manager = VersoArtifactManager(
        repo,
        runner=InspectingRunner(),
        source_token_fn=lambda _repo: TOKEN_A,
        root=root,
    )
    manager.trigger()
    assert manager.wait(2)
    assert keep.is_file()
    assert not stale.exists()
    assert zero.read_text() == "{}"
    assert manager.status(trigger=False)["state"] == "ready"


def test_startup_cleans_work_and_publish_keeps_recent_pinned_artifacts(tmp_path):
    repo = _project(tmp_path)
    root = repo / ".lake" / "marathon-verso"
    abandoned = root / "work" / "abandoned" / "partial.html"
    abandoned.parent.mkdir(parents=True)
    abandoned.write_text("partial")
    source = [TOKEN_A]
    manager = VersoArtifactManager(
        repo,
        runner=FakeVersoRunner(),
        source_token_fn=lambda _repo: source[0],
        root=root,
    )
    assert not abandoned.exists()

    source_tokens = (
        TOKEN_A, TOKEN_B, TOKEN_C, TOKEN_D, TOKEN_E, TOKEN_F, TOKEN_G
    )
    artifact_tokens = []
    for token in source_tokens:
        source[0] = token
        manager.trigger()
        assert manager.wait(2)
        assert manager.status(trigger=False)["state"] == "ready"
        artifact_tokens.append(manager.manifest(trigger=False)["artifact_token"])

    artifacts = {path.name for path in (root / "artifacts").iterdir()}
    assert artifacts == set(artifact_tokens[-5:])
    assert manager.manifest(trigger=False)["artifact_token"] == artifact_tokens[-1]
    assert not (root / "artifacts" / artifact_tokens[0]).exists()
    assert manager.resolve_artifact(
        "Demo/Main/", artifact_token=artifact_tokens[-5]
    ).name == "index.html"
    assert not any((root / "work").iterdir())


def test_source_change_mid_build_retries_without_mislabeling_artifact(tmp_path):
    repo = _project(tmp_path)
    source = [TOKEN_A]

    class MovingSourceRunner(FakeVersoRunner):
        def __init__(self):
            super().__init__()
            self.moved = False

        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            if command[1:3] == ["exe", "verso-html"] and not self.moved:
                self.moved = True
                source[0] = TOKEN_B
            return result

    runner = MovingSourceRunner()
    manager = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: source[0]
    )
    manager.trigger()
    assert manager.wait(2)
    assert manager.status(trigger=False)["state"] == "ready"
    artifact_a = effective_artifact_token(TOKEN_A, manager._dependency_signature())
    artifact_b = effective_artifact_token(TOKEN_B, manager._dependency_signature())
    assert manager.manifest(trigger=False)["artifact_token"] == artifact_b
    assert not (manager.root / "artifacts" / artifact_a).exists()
    assert (manager.root / "artifacts" / artifact_b).is_dir()
    assert [command[1] for command in runner.commands].count("build") == 2


def test_dirty_toolchain_and_lakefile_changes_refresh_without_restart(tmp_path):
    repo = _project(tmp_path)
    runner = FakeVersoRunner()
    manager = VersoArtifactManager(
        repo, runner=runner, source_token_fn=lambda _repo: TOKEN_A
    )
    manager.trigger()
    assert manager.wait(2)
    first = manager.manifest(trigger=False)["artifact_token"]

    (repo / "lean-toolchain").write_text("leanprover/lean4:v4.29.0\n")
    stale = manager.status(trigger=False)
    assert stale["state"] == "stale"
    assert stale["verso_tag"] == "v4.29.0"
    manager.trigger()
    assert manager.wait(2)
    second = manager.manifest(trigger=False)["artifact_token"]
    assert second != first
    assert manager.status(trigger=False)["verso_tag"] == "v4.29.0"

    (repo / "lakefile.toml").write_text(
        'name = "RenamedPackage"\n\n[[lean_lib]]\nname = "Demo"\n'
    )
    assert manager.status(trigger=False)["state"] == "stale"
    manager.trigger()
    assert manager.wait(2)
    third = manager.manifest(trigger=False)["artifact_token"]
    assert third not in {first, second}
    assert 'rev = "v4.29.0"' in (manager.root / "lakefile.toml").read_text()
    build_commands = [command for command in runner.commands if command[1] == "build"]
    assert "@RenamedPackage/+Demo:literate" in build_commands[-1]
    assert [command[1] for command in runner.commands].count("update") == 3


def test_slow_project_discovery_cannot_overwrite_newer_configuration(
    tmp_path, monkeypatch
):
    repo = _project(tmp_path)
    manager = VersoArtifactManager(
        repo, runner=FakeVersoRunner(), source_token_fn=lambda _repo: TOKEN_A
    )
    original_discover = verso.discover_verso_project
    slow_started = threading.Event()
    release_slow = threading.Event()

    def racing_discover(repo_dir):
        project = original_discover(repo_dir)
        if project.package == "SlowPackage":
            slow_started.set()
            assert release_slow.wait(2)
        return project

    monkeypatch.setattr(verso, "discover_verso_project", racing_discover)
    (repo / "lakefile.toml").write_text(
        'name = "SlowPackage"\n\n[[lean_lib]]\nname = "SlowLib"\n'
    )
    slow_status = {}

    def read_slow_status():
        slow_status.update(manager.status(trigger=False))

    thread = threading.Thread(target=read_slow_status)
    thread.start()
    assert slow_started.wait(1)
    try:
        (repo / "lakefile.toml").write_text(
            'name = "CurrentPackage"\n\n[[lean_lib]]\nname = "CurrentLib"\n'
        )
        current_status = manager.status(trigger=False)
    finally:
        release_slow.set()
        thread.join(2)

    assert not thread.is_alive()
    assert current_status["library"] == "CurrentLib"
    assert slow_status["library"] == "CurrentLib"
    assert manager.project.package == "CurrentPackage"
    assert manager._project_signature == manager._dependency_signature()


def test_legacy_raw_token_is_stale_fallback_not_false_current(tmp_path):
    repo = _project(tmp_path)
    root = repo / ".lake" / "marathon-verso"
    legacy = root / "artifacts" / TOKEN_A
    _write_site(legacy / "site")
    legacy.joinpath("manifest.json").write_text(
        '{"artifact_token": "' + TOKEN_A + '", "constants": {}, "modules": {}}'
    )
    legacy.joinpath("metadata.json").write_text("{}")
    root.joinpath("state.json").write_text(
        '{"artifact_token": "' + TOKEN_A + '"}'
    )

    manager = VersoArtifactManager(
        repo,
        runner=FakeVersoRunner(),
        source_token_fn=lambda _repo: TOKEN_A,
        root=root,
    )
    stale = manager.status(trigger=False)
    assert stale["state"] == "stale"
    assert stale["available"] is True
    assert stale["artifact_token"] == TOKEN_A
    manager.trigger()
    assert manager.wait(2)
    ready = manager.status(trigger=False)
    assert ready["state"] == "ready"
    assert ready["artifact_token"] != TOKEN_A
    assert manager.resolve_artifact(
        "Demo/Main/", artifact_token=TOKEN_A
    ).is_file()


def test_two_managers_share_one_cross_process_build(tmp_path):
    repo = _project(tmp_path)
    root = repo / ".lake" / "marathon-verso"
    started = threading.Event()
    release = threading.Event()

    class LockedRunner(FakeVersoRunner):
        def __call__(self, command, **kwargs):
            if command[1] == "update":
                started.set()
                assert release.wait(2)
            return super().__call__(command, **kwargs)

    first_runner = LockedRunner()
    first = VersoArtifactManager(
        repo,
        runner=first_runner,
        source_token_fn=lambda _repo: TOKEN_A,
        root=root,
    )
    first.trigger()
    assert started.wait(1)

    second_runner = FakeVersoRunner()
    second = VersoArtifactManager(
        repo,
        runner=second_runner,
        source_token_fn=lambda _repo: TOKEN_A,
        root=root,
    )
    assert second.trigger()["state"] == "building"
    assert second_runner.commands == []

    release.set()
    assert first.wait(2)
    assert second.wait(2)
    assert second_runner.commands == []
    assert first.status(trigger=False)["state"] == "ready"
    assert second.status(trigger=False)["state"] == "ready"
    assert second.manifest(trigger=False) == first.manifest(trigger=False)


def test_manifest_and_artifact_lookup_use_only_stdlib_and_stay_contained(tmp_path):
    site = tmp_path / "site"
    _write_site(site)
    manifest = build_verso_manifest(site, TOKEN_A)
    assert list(manifest["constants"]) == ["Demo.answer"]
    assert manifest["modules"]["Demo.Main"]["url"] == "Demo/Main/"

    assert safe_artifact_path(site, "Demo/Main/") == (
        site / "Demo" / "Main" / "index.html"
    ).resolve()
    assert safe_artifact_path(site, "../secret") is None
    assert safe_artifact_path(site, "%2e%2e/secret") is None
    assert safe_artifact_path(site, "/etc/passwd") is None
    assert safe_artifact_path(site, "Demo\\Main\\index.html") is None
    assert safe_artifact_path(site, "Demo/./Main/index.html") is None
    assert safe_artifact_path(site, "Demo//Main/index.html") is None
    assert safe_artifact_path(site, "Demo/%zz/index.html") is None
    assert safe_artifact_path(site, "Demo/Main") is None

    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (site / "escape").symlink_to(outside)
    assert safe_artifact_path(site, "escape") is None
