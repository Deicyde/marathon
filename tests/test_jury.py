"""Tests for the advisory jury (marathon.jury).

The jury is the LLM half of the phase-2 gate: two thresholded rubrics
(proof_integrity ≥ 3, code_quality ≥ 3), explicitly **no faithfulness**
(marathon-v2 plan §2 ruling 3 — the information firewall means the model
never sees the source text, so the prompt must say faithfulness is out of
scope and must never ask for a source comparison).

Covered here, all with a monkeypatched subprocess (no real ``claude``):

* JSON parsing: strict happy path, lenient per-field recovery, garbage.
* Verdict semantics: recomputed from thresholds, model disagreement noted.
* Context assembly: code cap with truncation marker, diff cap, diff
  inclusion only when provided.
* None-on-failure: missing CLI, empty folder, exec error, nonzero exit,
  empty stdout — advisory means never raising out. Context assembly is
  covered too: unreadable / non-UTF-8 ``.lean`` entries are skipped or
  decoded with replacement, never raised.
* Prompt content: the faithfulness-out-of-scope sentence is present and
  no source-comparison vocabulary leaks in.
"""

from pathlib import Path
from types import SimpleNamespace

from marathon import jury
from marathon.jury import JuryVerdict, run_jury


# --- Fixture helpers ----------------------------------------------------------


def _make_target(tmp_path: Path, lean_text: str = "theorem foo : True := trivial\n"):
    """Repo dir + target folder with one .lean file."""
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Output" / "Chapter1"
    target.mkdir(parents=True)
    (target / "Basic.lean").write_text(lean_text)
    return repo_dir, target


def _install_claude(
    monkeypatch,
    response: str = "",
    returncode: int = 0,
    raise_oserror: bool = False,
    calls: list | None = None,
):
    """Fake both ``shutil.which`` and ``subprocess.run`` inside
    marathon.jury. Records ``(cmd, kwargs)`` into ``calls`` when given."""
    monkeypatch.setattr(jury.shutil, "which", lambda name: "/fake/bin/claude")

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if raise_oserror:
            raise OSError(7, "Argument list too long")
        return SimpleNamespace(stdout=response, stderr="", returncode=returncode)

    monkeypatch.setattr(jury.subprocess, "run", fake_run)


STRICT_PASS = (
    '{"proof_integrity": 4, "code_quality": 3, "verdict": "pass", '
    '"notes": "Proofs are genuine. Style is fine."}'
)


# --- JSON parsing: happy / lenient / garbage ------------------------------------


def test_strict_json_happy_path(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, response=STRICT_PASS)

    v = run_jury(repo_dir, target)

    assert v is not None
    assert v.proof_integrity == 4
    assert v.code_quality == 3
    assert v.verdict == "pass"
    assert v.passed is True
    assert v.notes == "Proofs are genuine. Style is fine."
    assert v.parse_warning is None


def test_fenced_json_is_extracted(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(
        monkeypatch,
        response="Here is my assessment:\n```json\n" + STRICT_PASS + "\n```\n",
    )

    v = run_jury(repo_dir, target)

    assert v is not None
    assert (v.proof_integrity, v.code_quality, v.verdict) == (4, 3, "pass")


def test_lenient_recovery_when_notes_break_json(tmp_path, monkeypatch):
    # Unescaped inner quotes make strict json.loads fail; the per-field
    # regex fallback must still recover both scores and the verdict.
    broken = (
        '{"proof_integrity": 2, "code_quality": 4, "verdict": "fail", '
        '"notes": "the "instance" over PUnit nullifies type distinctions"}'
    )
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, response=broken)

    v = run_jury(repo_dir, target)

    assert v is not None
    assert v.proof_integrity == 2
    assert v.code_quality == 4
    assert v.verdict == "fail"
    assert v.passed is False
    assert v.parse_warning is not None
    assert "lenient-parse fallback" in v.parse_warning


def test_garbage_response_returns_none(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, response="I cannot rate this code, sorry!")

    assert run_jury(repo_dir, target) is None


# --- Verdict semantics ----------------------------------------------------------


def test_verdict_recomputed_from_thresholds_overrides_model(tmp_path, monkeypatch):
    # Model claims "pass" but integrity=2 is below the ≥3 threshold:
    # the computed verdict wins and the disagreement is recorded.
    lying = (
        '{"proof_integrity": 2, "code_quality": 5, "verdict": "pass", '
        '"notes": "looks fine to me"}'
    )
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, response=lying)

    v = run_jury(repo_dir, target)

    assert v is not None
    assert v.verdict == "fail"
    assert v.passed is False
    assert v.parse_warning is not None
    assert "disagreed with thresholds" in v.parse_warning


def test_verdict_derived_when_model_omits_it(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(
        monkeypatch,
        response='{"proof_integrity": 3, "code_quality": 3, "notes": "borderline"}',
    )

    v = run_jury(repo_dir, target)

    assert v is not None
    assert v.verdict == "pass"  # both exactly at threshold ⇒ pass


def test_model_verdict_used_when_a_score_is_missing(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(
        monkeypatch,
        response='{"proof_integrity": 4, "verdict": "fail", "notes": "n"}',
    )

    v = run_jury(repo_dir, target)

    assert v is not None
    assert v.code_quality is None
    assert v.verdict == "fail"


# --- Context assembly -----------------------------------------------------------


def test_prompt_embeds_target_lean_code_and_label(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path, "theorem marker_decl : True := trivial\n")
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)

    run_jury(repo_dir, target)

    [(cmd, kwargs)] = calls
    prompt = kwargs["input"]
    assert "=== FILE: Basic.lean ===" in prompt
    assert "marker_decl" in prompt
    # Target labelled repo-relative.
    assert "Output/Chapter1" in prompt
    # Prompt travels via stdin, not argv (E2BIG class).
    assert all("marker_decl" not in part for part in cmd)


def test_code_cap_truncates_with_marker(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path, "-- filler\n" * 200)
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)
    monkeypatch.setattr(jury, "MAX_CODE_CHARS", 100)

    run_jury(repo_dir, target)

    prompt = calls[0][1]["input"]
    assert "... (code truncated at 100 chars)" in prompt
    # The full 2,000-char file must not have been embedded.
    assert prompt.count("-- filler") < 200


def test_diff_included_and_capped(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)
    monkeypatch.setattr(jury, "MAX_DIFF_CHARS", 50)

    run_jury(repo_dir, target, diff_text="+added line\n" * 100)

    prompt = calls[0][1]["input"]
    assert "## Diff under review" in prompt
    assert "... (diff truncated at 50 chars)" in prompt


def test_no_diff_section_without_diff(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)

    run_jury(repo_dir, target)

    assert "## Diff under review" not in calls[0][1]["input"]


def test_model_resolution_arg_beats_env(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)
    monkeypatch.setenv("MARATHON_CLAUDE_MODEL", "env-model")

    v = run_jury(repo_dir, target, model="arg-model")
    assert v is not None and v.model == "arg-model"
    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "arg-model"

    v2 = run_jury(repo_dir, target)
    assert v2 is not None and v2.model == "env-model"


def test_api_key_scrubbed_from_subprocess_env(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    run_jury(repo_dir, target)

    assert "ANTHROPIC_API_KEY" not in calls[0][1]["env"]


# --- None on any failure (advisory ⇒ never raises) -------------------------------


def test_none_when_claude_cli_missing(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    monkeypatch.setattr(jury.shutil, "which", lambda name: None)

    assert run_jury(repo_dir, target) is None


def test_none_when_target_has_no_lean_files(tmp_path, monkeypatch):
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Output" / "Chapter1"
    target.mkdir(parents=True)
    _install_claude(monkeypatch, response=STRICT_PASS)

    assert run_jury(repo_dir, target) is None


def test_unreadable_lean_entry_is_skipped_not_raised(tmp_path, monkeypatch, capsys):
    """A directory named ``*.lean`` (rglob matches it; ``read_text``
    raises ``IsADirectoryError``) must be skipped with a printed note —
    the advisory never-raises guarantee covers context assembly too, and
    the verdict still comes from the remaining readable context."""
    repo_dir, target = _make_target(tmp_path)
    (target / "Broken.lean").mkdir()
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)

    v = run_jury(repo_dir, target)

    assert v is not None  # scored on the surviving file, no raise
    prompt = calls[0][1]["input"]
    assert "=== FILE: Basic.lean ===" in prompt
    assert "=== FILE: Broken.lean ===" not in prompt
    assert "skipping unreadable" in capsys.readouterr().out


def test_non_utf8_lean_file_does_not_raise(tmp_path, monkeypatch):
    """Mojibake bytes decode with replacement instead of throwing
    ``UnicodeDecodeError`` out of the advisory jury."""
    repo_dir, target = _make_target(tmp_path)
    (target / "Mojibake.lean").write_bytes(b"\xff\xfe theorem bad : True := trivial\n")
    _install_claude(monkeypatch, response=STRICT_PASS)

    assert run_jury(repo_dir, target) is not None


def test_none_on_exec_oserror(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, raise_oserror=True)

    assert run_jury(repo_dir, target) is None  # swallowed, not raised


def test_none_on_nonzero_exit(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, response="boom", returncode=1)

    assert run_jury(repo_dir, target) is None


def test_none_on_empty_stdout(tmp_path, monkeypatch):
    repo_dir, target = _make_target(tmp_path)
    _install_claude(monkeypatch, response="   \n")

    assert run_jury(repo_dir, target) is None


# --- Prompt content: the firewall ------------------------------------------------


def test_prompt_declares_faithfulness_out_of_scope_and_never_asks_for_source(
    tmp_path, monkeypatch
):
    repo_dir, target = _make_target(tmp_path)
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_PASS, calls=calls)

    run_jury(repo_dir, target)

    prompt = calls[0][1]["input"]
    # The load-bearing sentence: faithfulness is explicitly out of scope
    # (humans own it) so the model doesn't drift into judging it.
    assert "OUT OF SCOPE for this jury" in prompt
    assert "humans own that judgment" in prompt
    # And the prompt never asks the model to consult or weigh the
    # original informal text — no source-comparison vocabulary at all
    # (autoform's rubrics say "Original Statement (from book)" / "Book
    # Source"; ours must not).
    lowered = prompt.lower()
    assert "book" not in lowered
    assert ".tex" not in lowered
    assert "latex" not in lowered
    assert "original statement" not in lowered
    assert "compare" not in lowered  # also catches "compared"/"comparison"
    assert "source material" not in lowered


def test_jury_md_on_disk_states_the_two_rubrics_and_thresholds():
    text = (Path(jury.__file__).parent / "prompts" / "jury.md").read_text()
    assert "proof_integrity" in text
    assert "code_quality" in text
    # Threshold line and the strict single-line JSON schema.
    assert "proof_integrity ≥ 3 AND code_quality ≥ 3" in text
    assert '{"proof_integrity": N, "code_quality": N, "verdict": "pass"|"fail", "notes": "..."}' in text
    # The only faithfulness mentions are scope exclusions, not a rubric.
    assert "OUT OF SCOPE" in text


# --- render_line -----------------------------------------------------------------


def test_render_line_pass_and_fail():
    ok = JuryVerdict(proof_integrity=4, code_quality=3, verdict="pass")
    assert ok.render_line() == "jury (advisory): integrity=4 quality=3 → PASS"

    bad = JuryVerdict(proof_integrity=1, code_quality=5, verdict="fail")
    assert "integrity=1 quality=5 → FAIL" in bad.render_line()


def test_render_line_handles_missing_fields():
    v = JuryVerdict()
    line = v.render_line()
    assert "integrity=—" in line and "quality=—" in line and "?" in line
