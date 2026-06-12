"""Tests for ``marathon.claude_proc`` — the shared ``claude`` CLI helper
and its cross-process concurrency limiter — plus one representative test
per migrated call site (reviewer / rater / Hermes / jury / referee).

Boundaries are monkeypatched throughout: ``subprocess.run`` is replaced
with an in-process fake (nothing ever execs the real ``claude``), and the
slot directory is redirected to ``tmp_path`` by an autouse fixture so no
test can touch the real ``~/.marathon/claude-slots``. The slot files
themselves are exercised for real — ``fcntl.flock`` against tmp files is
the unit under test, not a boundary.

Covered:

* stdin carries the prompt (never argv) and ``ANTHROPIC_API_KEY`` is
  scrubbed from the child env while the rest of the env survives.
* Model precedence — kwarg > ``MARATHON_CLAUDE_MODEL`` env > default —
  matching the order ``jury._resolve_model`` established.
* Slot contention: all K slots held ⇒ acquire waits, then proceeds with
  a warning after the (shortened) timeout; a free sibling slot is taken
  immediately; slots are released on completion and on subprocess error.
* Each migrated call site still parses its output through a monkeypatched
  ``run_claude`` and pins the model it historically used.
"""

import fcntl
import os
import subprocess
import time
from pathlib import Path

import pytest

from marathon import claude_proc
from marathon.claude_proc import run_claude


# --- Fixtures -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sandboxed_slots(monkeypatch, tmp_path):
    """Redirect the slot dir to tmp_path and shrink the acquire budget so
    no test in this module can write to the real home dir or block for
    minutes. Also clears the model/concurrency env so precedence tests
    start from a known state."""
    monkeypatch.setenv("MARATHON_CLAUDE_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.delenv("MARATHON_CLAUDE_MODEL", raising=False)
    monkeypatch.delenv("MARATHON_CLAUDE_MAX_CONCURRENT", raising=False)
    monkeypatch.setattr(claude_proc, "SLOT_ACQUIRE_TIMEOUT_SECONDS", 0.3)
    monkeypatch.setattr(claude_proc, "SLOT_POLL_INTERVAL_SECONDS", 0.02)


def _install_fake_claude(
    monkeypatch,
    response: str = "ok",
    returncode: int = 0,
    raise_oserror: bool = False,
    calls: list | None = None,
):
    """Fake ``shutil.which`` + ``subprocess.run`` as seen by claude_proc.
    Records ``(cmd, kwargs)`` into ``calls`` when given."""
    monkeypatch.setattr(claude_proc.shutil, "which", lambda name: "/fake/bin/claude")

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if raise_oserror:
            raise OSError(7, "Argument list too long")
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=response, stderr=""
        )

    monkeypatch.setattr(claude_proc.subprocess, "run", fake_run)


def _fake_run_claude(record: list, stdout: str = "", returncode: int = 0):
    """A stand-in for ``claude_proc.run_claude`` used by the call-site
    tests: records the prompt + kwargs, returns a canned CompletedProcess."""

    def fake(prompt, *, model=None, timeout=None, extra_args=()):
        record.append(
            {"prompt": prompt, "model": model, "timeout": timeout,
             "extra_args": tuple(extra_args)}
        )
        return subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr=""
        )

    return fake


def _flock_nb(path: Path) -> int:
    """Open + exclusively flock ``path`` non-blocking; return the fd.
    Raises ``OSError`` when the lock is held elsewhere — which is exactly
    what the release tests assert against."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise
    return fd


# --- Subprocess conventions -------------------------------------------------------


def test_prompt_travels_via_stdin_not_argv(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)

    proc = run_claude("THE PROMPT TEXT")

    [(cmd, kwargs)] = calls
    assert kwargs["input"] == "THE PROMPT TEXT"
    assert all("THE PROMPT TEXT" not in part for part in cmd)
    assert proc.returncode == 0 and proc.stdout == "ok"


def test_shared_flag_set_and_binary(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)

    run_claude("p")

    [(cmd, _)] = calls
    assert cmd[0] == "/fake/bin/claude"
    assert cmd[1] == "-p"
    # --tools "" disables the agent tool surface (single completion);
    # --output-format text keeps stdout parseable.
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--output-format") + 1] == "text"


def test_api_key_scrubbed_but_rest_of_env_kept(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("MARATHON_TEST_SENTINEL", "still-here")

    run_claude("p")

    env = calls[0][1]["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert env["MARATHON_TEST_SENTINEL"] == "still-here"


def test_extra_args_appended_after_shared_flags(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)

    run_claude("p", extra_args=("--add-dir", "/x"))

    [(cmd, _)] = calls
    assert cmd[-2:] == ["--add-dir", "/x"]


def test_timeout_forwarded_to_subprocess(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)

    run_claude("p", timeout=42)
    run_claude("p")

    assert calls[0][1]["timeout"] == 42
    assert calls[1][1]["timeout"] is None  # default: no timeout (parity)


def test_missing_cli_raises_filenotfounderror(monkeypatch):
    monkeypatch.setattr(claude_proc.shutil, "which", lambda name: None)

    with pytest.raises(FileNotFoundError):
        run_claude("p")


# --- Model precedence: kwarg > env > default --------------------------------------
# (The order jury._resolve_model established; verified against existing code.)


def test_model_default_when_nothing_set(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)

    run_claude("p")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == claude_proc.DEFAULT_MODEL == "claude-opus-4-7"


def test_model_env_beats_default(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)
    monkeypatch.setenv("MARATHON_CLAUDE_MODEL", "env-model")

    run_claude("p")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "env-model"


def test_model_kwarg_beats_env(monkeypatch):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)
    monkeypatch.setenv("MARATHON_CLAUDE_MODEL", "env-model")

    run_claude("p", model="kwarg-model")

    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "kwarg-model"


# --- Slot limiter ------------------------------------------------------------------


def test_slot_dir_and_files_created_lazily(monkeypatch, tmp_path):
    _install_fake_claude(monkeypatch)
    slot_dir = tmp_path / "slots"
    assert not slot_dir.exists()  # nothing at import time

    run_claude("p")

    assert (slot_dir / "slot-0.lock").is_file()


def test_slot_released_on_completion(monkeypatch, tmp_path):
    _install_fake_claude(monkeypatch)
    monkeypatch.setenv("MARATHON_CLAUDE_MAX_CONCURRENT", "1")

    run_claude("p")

    # With K=1, a leaked fd would still hold the flock (locks are per
    # open-file-description, so they conflict even within one process).
    fd = _flock_nb(tmp_path / "slots" / "slot-0.lock")
    os.close(fd)


def test_slot_released_when_subprocess_raises(monkeypatch, tmp_path):
    _install_fake_claude(monkeypatch, raise_oserror=True)
    monkeypatch.setenv("MARATHON_CLAUDE_MAX_CONCURRENT", "1")

    with pytest.raises(OSError):
        run_claude("p")

    fd = _flock_nb(tmp_path / "slots" / "slot-0.lock")
    os.close(fd)


def test_contention_waits_then_proceeds_with_warning(monkeypatch, tmp_path, capsys):
    """All K slots held ⇒ run_claude polls until the (shortened) acquire
    timeout, prints the courtesy-throttle warning, and runs anyway —
    the limiter must never become a deadlock source."""
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)
    monkeypatch.setenv("MARATHON_CLAUDE_MAX_CONCURRENT", "1")
    slot_dir = tmp_path / "slots"
    slot_dir.mkdir(parents=True)
    holder = _flock_nb(slot_dir / "slot-0.lock")  # test process holds K=1
    try:
        start = time.monotonic()
        proc = run_claude("p")
        elapsed = time.monotonic() - start
    finally:
        os.close(holder)

    assert len(calls) == 1  # the call ran anyway
    assert proc.returncode == 0
    assert elapsed >= 0.29  # waited out the 0.3s acquire budget
    out = capsys.readouterr().out
    assert "WARNING" in out and "proceeding anyway" in out


def test_free_sibling_slot_taken_immediately(monkeypatch, tmp_path, capsys):
    calls: list = []
    _install_fake_claude(monkeypatch, calls=calls)
    monkeypatch.setenv("MARATHON_CLAUDE_MAX_CONCURRENT", "2")
    slot_dir = tmp_path / "slots"
    slot_dir.mkdir(parents=True)
    holder = _flock_nb(slot_dir / "slot-0.lock")  # slot-0 busy, slot-1 free
    try:
        start = time.monotonic()
        run_claude("p")
        elapsed = time.monotonic() - start

        assert len(calls) == 1
        assert elapsed < 0.25  # no timeout wait — took slot-1
        assert "WARNING" not in capsys.readouterr().out
        # slot-1 was released on completion; slot-0 is still ours.
        fd = _flock_nb(slot_dir / "slot-1.lock")
        os.close(fd)
        with pytest.raises(OSError):
            _flock_nb(slot_dir / "slot-0.lock")
    finally:
        os.close(holder)


def test_garbage_concurrency_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MARATHON_CLAUDE_MAX_CONCURRENT", "two")
    assert claude_proc._max_concurrent() == claude_proc.DEFAULT_MAX_CONCURRENT
    monkeypatch.setenv("MARATHON_CLAUDE_MAX_CONCURRENT", "0")
    assert claude_proc._max_concurrent() == 1  # clamped, never zero slots


# --- Migrated call sites still parse their outputs ---------------------------------


def test_reviewer_call_site_returns_response_and_pins_module_model(
    monkeypatch, tmp_path
):
    from marathon import claude_review as cr

    record: list = []
    monkeypatch.setattr(cr.shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(cr, "run_claude", _fake_run_claude(record, stdout="DRAFTED PROMPT\n"))
    # Repo context normally shells out to `git ls-files`; stub it.
    monkeypatch.setattr(cr, "_read_repo_lean_context", lambda repo, excl: "")

    repo = tmp_path / "repo"
    target = repo / "Output" / "Chapter1"
    target.mkdir(parents=True)
    (target / "Basic.lean").write_text("theorem reviewer_marker : True := trivial\n")

    response = cr.review_and_draft_prompt(
        target, repo, marathon_md=None, refine_log="",
        iteration_idx=1, max_iterations=3,
    )

    assert response == "DRAFTED PROMPT"
    [call] = record
    assert call["model"] == cr.CLAUDE_MODEL  # import-time module pin, preserved
    assert "Reviewer rubric" in call["prompt"]
    assert "reviewer_marker" in call["prompt"]


def test_rater_call_site_parses_scores_and_pins_model(monkeypatch, tmp_path):
    from marathon import post_pipeline as pp

    record: list = []
    monkeypatch.setattr(pp.shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(
        pp, "run_claude",
        _fake_run_claude(
            record,
            stdout=(
                '{"quality": 4, "math_correctness": 5, "generality": 3, '
                '"api_coverage": 4, "concision": 3, "modern_lean4": 4, '
                '"structural_focus": 2, "notes": "solid"}'
            ),
        ),
    )

    target = tmp_path / "Chapter1"
    target.mkdir()
    (target / "Basic.lean").write_text("theorem rater_marker : True := trivial\n")

    r = pp.call_claude_rater(target, build_result=None)

    assert r.parse_error is None
    assert (r.quality, r.math_correctness, r.structural_focus) == (4, 5, 2)
    assert r.notes == "solid"
    [call] = record
    assert call["model"] == "claude-opus-4-7"  # rater stays pinned (no env override)
    assert "rater_marker" in call["prompt"]


def test_hermes_call_site_round_trips_decision_and_raises_on_failure(
    monkeypatch, tmp_path
):
    from marathon import hermes_watcher as hw

    monkeypatch.setattr(hw.shutil, "which", lambda name: "/fake/bin/claude")
    watcher = hw.HermesWatcher(
        workdir=tmp_path,
        target_folder=tmp_path / "repo" / "Chapter1",
        repo_dir=tmp_path / "repo",
        referee_path=None,
        skeleton_mode=False,
        iteration_idx=0,
    )

    record: list = []
    monkeypatch.setattr(
        hw, "run_claude",
        _fake_run_claude(record, stdout='{"steer": false, "reason": "on course"}\n'),
    )
    raw = watcher._invoke_claude("judge this edit")
    assert raw == '{"steer": false, "reason": "on course"}\n'  # unstripped, historical
    decision = hw._parse_decision(raw)
    assert decision.steer is False and decision.reason == "on course"
    assert record[0]["model"] == hw.CLAUDE_MODEL == "claude-opus-4-7"
    assert record[0]["prompt"] == "judge this edit"

    monkeypatch.setattr(
        hw, "run_claude", _fake_run_claude([], stdout="boom", returncode=1)
    )
    with pytest.raises(RuntimeError, match="claude exited 1"):
        watcher._invoke_claude("judge this edit")


def test_jury_call_site_parses_verdict_through_run_claude(monkeypatch, tmp_path):
    from marathon import jury

    record: list = []
    monkeypatch.setattr(jury.shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(
        jury, "run_claude",
        _fake_run_claude(
            record,
            stdout='{"proof_integrity": 4, "code_quality": 3, "verdict": "pass", "notes": "n"}',
        ),
    )

    repo = tmp_path / "repo"
    target = repo / "Chapter1"
    target.mkdir(parents=True)
    (target / "Basic.lean").write_text("theorem jury_marker : True := trivial\n")

    v = jury.run_jury(repo, target, model="pinned-model")

    assert v is not None and v.verdict == "pass"
    assert (v.proof_integrity, v.code_quality) == (4, 3)
    [call] = record
    assert call["model"] == "pinned-model"  # pre-resolved model passed through
    assert "jury_marker" in call["prompt"]


def test_referee_call_site_sends_prompt_via_stdin_not_argv(monkeypatch):
    """The referee was the last argv call site; after migration the prompt
    must arrive as run_claude's stdin argument and the response contract
    ((ok, stripped_text)) is unchanged."""
    from marathon import referee

    record: list = []
    monkeypatch.setattr(referee.shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(
        referee, "run_claude",
        _fake_run_claude(record, stdout="### Top-leverage open items\n- item\n"),
    )

    ok, response = referee._invoke_claude_referee("REFEREE PROMPT " * 10)

    assert ok is True
    assert response == "### Top-leverage open items\n- item"
    [call] = record
    assert call["prompt"].startswith("REFEREE PROMPT ")  # stdin, not argv
    assert call["model"] == referee.REFEREE_MODEL == "claude-opus-4-7"


def test_referee_call_site_reports_failure_tuple(monkeypatch):
    from marathon import referee

    monkeypatch.setattr(referee.shutil, "which", lambda name: "/fake/bin/claude")
    monkeypatch.setattr(
        referee, "run_claude", _fake_run_claude([], stdout="err", returncode=2)
    )

    ok, message = referee._invoke_claude_referee("p")

    assert ok is False
    assert "claude exited 2" in message
