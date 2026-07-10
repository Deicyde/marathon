"""Headless chapter intake: gather chapter source → one Claude completion →
parse drafts → write the drafts file, never raising on failure."""
from __future__ import annotations

import re
import subprocess

from marathon.review.chapter_intake import drafts_path_for, run_chapter_intake
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "base"],
        cwd=repo, check=True,
    )
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="owner/repo",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={},
    )


def _write_chapter(
    cfg, chapter,
    body="theorem foo : True := by sorry\n\nlemma bar : True := by sorry",
):
    d = cfg.target_path(chapter)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Foo.lean").write_text(
        f"namespace Demo.Chapter{chapter}\n\n{body}\n\nend Demo.Chapter{chapter}\n"
    )
    subprocess.run(["git", "add", "."], cwd=cfg.repo_dir, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"chapter {chapter}"],
        cwd=cfg.repo_dir, check=True,
    )


_GOOD_DRAFTS = """## 1/2 — Theorem foo

**Title**: `Theorem 15.1 — foo is true`

**Parent**: #1 — **LeeSM ref**: p. 1 — **Status**: 🟠

### Lean signatures

```lean
theorem foo : True
```
[`Demo.Chapter15.foo` at Demo/Chapter15/Foo.lean:L3-L3](https://github.com/owner/repo/blob/abc/Demo/Chapter15/Foo.lean#L3-L3)

### Informal Statement

*⚠️ LLM-rendered from common knowledge; verification pending.*

> foo holds.

### Mechanical accuracy

- The proposition has no hypotheses and its proof is still `sorry`.

### Verification questions

- Confirm that `True` is the intended target.

**Verdict**: _(VERIFIED 🟡 / REJECTED ❌)_

## 2/2 — Lemma bar

**Title**: `Lemma 15.2 — bar`

**Parent**: #1 — **LeeSM ref**: p. 2 — **Status**: 🟠

### Lean signatures

```lean
lemma bar : True
```
[`Demo.Chapter15.bar` at Demo/Chapter15/Foo.lean:L5-L5](https://github.com/owner/repo/blob/abc/Demo/Chapter15/Foo.lean#L5-L5)

### Informal Statement

*⚠️ LLM-rendered from common knowledge; verification pending.*

> bar holds.

### Mechanical accuracy

- The lemma has no hypotheses and its proof is still `sorry`.

### Verification questions

- Confirm that `True` is the intended target.

**Verdict**: _(VERIFIED 🟡 / REJECTED ❌)_

## Coverage summary

Two first-class results.
"""


def _runner(out, code=0, err="", capture=None):
    def _run(prompt, *, model=None, timeout=None, extra_args=(), env_overrides=None):
        if capture is not None:
            capture["prompt"] = prompt
            capture["model"] = model
            capture["extra_args"] = tuple(extra_args)
            capture["env_overrides"] = env_overrides
        match = re.search(
            r"https://github\.com/owner/repo/blob/([0-9a-f]{40})/", prompt,
        )
        rendered = out
        if match:
            rendered = rendered.replace("/blob/abc/", f"/blob/{match.group(1)}/")
        return subprocess.CompletedProcess(["claude"], code, rendered, err)
    return _run


def test_intake_parses_drafts_and_writes_file(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    cap: dict = {}
    res = run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS, capture=cap))

    assert res.ok and res.error is None
    assert [s.index for s in res.subissues] == [1, 2]
    assert res.subissues[0].title == "Theorem 15.1 — foo is true"
    assert "theorem foo" in res.subissues[0].body
    # drafts file written for the apply step
    dp = drafts_path_for(cfg, 15)
    assert dp.is_file() and "## 1/2 — Theorem foo" in dp.read_text()
    # headless: the chapter source must be EMBEDDED in the prompt (no file access)
    assert "theorem foo : True := by sorry" in cap["prompt"]
    assert "Chapter 15" in cap["prompt"]
    assert "Durable issue-body contract" in cap["prompt"]
    assert "### Mechanical accuracy" in cap["prompt"]
    # The old bug embedded the interactive chat-summary schema here.
    assert "**TL;DR**" not in cap["prompt"]
    assert "`### Lean declarations` section" not in cap["prompt"]
    # no effort env → no --effort flag (the CLI default effort)
    assert cap["extra_args"] == ()


def test_intake_wires_effort_flag_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("MARATHON_INTAKE_EFFORT", "max")
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    cap: dict = {}
    res = run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS, capture=cap))
    assert res.ok
    assert cap["extra_args"] == ("--effort", "max")   # passed to claude -p
    assert "effort=max" in res.model                  # surfaced in the deck job label


def test_intake_no_effort_flag_when_env_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("MARATHON_INTAKE_EFFORT", raising=False)
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    cap: dict = {}
    run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS, capture=cap))
    assert cap["extra_args"] == ()


def test_intake_no_sources_returns_error_not_raise(tmp_path):
    cfg = _cfg(tmp_path)  # chapter folder never created
    res = run_chapter_intake(cfg, 15, _runner=_runner("irrelevant"))
    assert not res.ok
    assert "no .lean files" in (res.error or "")
    assert res.subissues == []


def test_intake_unparseable_output_is_an_error(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    res = run_chapter_intake(cfg, 15, _runner=_runner("I can't help with that."))
    assert not res.ok and "no parseable" in (res.error or "")
    assert not drafts_path_for(cfg, 15).is_file()  # nothing written on a bad parse


def test_intake_rejects_old_thin_ch15_body(tmp_path):
    """A titled section is no longer enough: the degraded Ch15 shape must fail
    before a drafts file can be approved/applied."""
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    thin = """## 1/1 — Foo

**Title**: `Foo`

### Lean declarations
- `foo`

### Informal Statement
*📋 Anchored to roadmap.*

## Coverage summary
one
"""
    res = run_chapter_intake(cfg, 15, _runner=_runner(thin))
    assert not res.ok
    assert "structural validation" in (res.error or "")
    assert "Lean signatures" in (res.error or "")
    assert not drafts_path_for(cfg, 15).is_file()


def test_intake_rejects_hallucinated_declaration(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    bad = _GOOD_DRAFTS.replace("lemma bar : True", "lemma invented : True").replace(
        "Demo.Chapter15.bar`", "Demo.Chapter15.invented`"
    )
    res = run_chapter_intake(cfg, 15, _runner=_runner(bad))
    assert not res.ok
    assert "`invented` is absent from source" in (res.error or "")
    assert not drafts_path_for(cfg, 15).is_file()


def test_intake_rejects_declaration_that_exists_only_in_block_comment(tmp_path):
    """Commented textbook examples are not live declaration identities."""
    cfg = _cfg(tmp_path)
    _write_chapter(
        cfg,
        15,
        body=(
            "/-\n"
            "theorem foo : True := by sorry\n"
            "-/\n\n"
            "lemma bar : True := by sorry"
        ),
    )

    res = run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS))

    assert not res.ok
    assert "`foo` is absent from source" in (res.error or "")
    assert not drafts_path_for(cfg, 15).is_file()


def test_intake_rejects_fake_namespace_even_when_leaf_exists(tmp_path):
    """A dotted citation cannot borrow an unrelated source declaration's leaf."""
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    bad = _GOOD_DRAFTS.replace(
        "theorem foo : True", "theorem TotallyWrong.foo : True", 1,
    ).replace("Demo.Chapter15.foo`", "TotallyWrong.foo`", 1)

    res = run_chapter_intake(cfg, 15, _runner=_runner(bad))

    assert not res.ok
    assert "`TotallyWrong.foo` is absent from source" in (res.error or "")


def test_intake_duplicate_owner_uses_resolved_identity(tmp_path):
    """Bare ``foo`` and qualified ``Demo.Chapter15.foo`` are one key card."""
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    duplicate_block = """```lean
theorem Demo.Chapter15.foo : True
```
[`foo` at Demo/Chapter15/Foo.lean:L3-L3](https://github.com/owner/repo/blob/abc/Demo/Chapter15/Foo.lean#L3-L3)"""
    bad = re.sub(
        r"```lean\nlemma bar : True\n```\n\[`Demo\.Chapter15\.bar`[^\n]+\]\([^\n]+\)",
        duplicate_block,
        _GOOD_DRAFTS,
        count=1,
    )

    res = run_chapter_intake(cfg, 15, _runner=_runner(bad))

    assert not res.ok
    assert "`Demo.Chapter15.foo` is already owned by draft 1" in (res.error or "")


def test_intake_rejects_signature_permalink_identity_mismatch(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    bad = _GOOD_DRAFTS.replace("Demo.Chapter15.foo`", "Demo.Chapter15.bar`", 1)

    res = run_chapter_intake(cfg, 15, _runner=_runner(bad))

    assert not res.ok
    assert "identify different source declarations" in (res.error or "")


def test_intake_rejects_wrong_signature_result_type(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    bad = _GOOD_DRAFTS.replace("theorem foo : True", "theorem foo : False", 1)

    res = run_chapter_intake(cfg, 15, _runner=_runner(bad))

    assert not res.ok
    assert "does not exactly match the complete source" in (res.error or "")


def test_intake_rejects_signature_missing_source_binder(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(
        cfg, 15,
        body=(
            "theorem foo (n : Nat) : True := by sorry\n\n"
            "lemma bar : True := by sorry"
        ),
    )

    res = run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS))

    assert not res.ok
    assert "does not exactly match the complete source" in (res.error or "")


def test_intake_rejects_permalink_for_wrong_repo_or_sha(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    bad = _GOOD_DRAFTS.replace("github.com/owner/repo", "github.com/evil/repo", 1)

    res = run_chapter_intake(cfg, 15, _runner=_runner(bad))

    assert not res.ok
    assert "must pin the exact repository, HEAD SHA" in (res.error or "")


def test_intake_refuses_dirty_chapter_source_before_model_call(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    (cfg.target_path(15) / "Foo.lean").write_text("theorem changed : True := trivial\n")

    def boom(*args, **kwargs):
        raise AssertionError("dirty intake must stop before invoking the model")

    res = run_chapter_intake(cfg, 15, _runner=boom)

    assert not res.ok
    assert "commit the chapter source before intake" in (res.error or "")


def test_tracker_intake_requires_one_draft_per_roadmap_line(tmp_path, monkeypatch):
    import marathon.review.github as ghmod

    monkeypatch.setattr(ghmod, "issue_body", lambda num, repo: _tracker_body())
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    one = _GOOD_DRAFTS.split("## 2/2", 1)[0].replace("## 1/2", "## 1/1")
    one += "\n## Coverage summary\none\n"
    res = run_chapter_intake(cfg, 15, use_tracker=True, _runner=_runner(one))
    assert not res.ok
    assert "tracker has 2 roadmap result(s)" in (res.error or "")


def test_intake_claude_nonzero_exit_is_an_error(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    res = run_chapter_intake(cfg, 15, _runner=_runner("", code=1, err="boom"))
    assert not res.ok
    assert "exited 1" in (res.error or "") and "boom" in (res.error or "")


def test_intake_runner_exception_is_caught(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)

    def _boom(prompt, *, model=None, timeout=None, extra_args=(), env_overrides=None):
        raise OSError("claude vanished")

    res = run_chapter_intake(cfg, 15, _runner=_boom)
    assert not res.ok and "claude intake call failed" in (res.error or "")


def test_intake_informal_file_embedded_with_verbatim_directive(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    informal = tmp_path / "informal.md"
    informal.write_text("## Theorem 15.1\nfoo is genuinely true.\n")
    cap: dict = {}
    res = run_chapter_intake(
        cfg, 15, informal_path=informal, _runner=_runner(_GOOD_DRAFTS, capture=cap)
    )
    assert res.ok
    assert "foo is genuinely true" in cap["prompt"]  # informal embedded
    assert "VERBATIM" in cap["prompt"]                # verbatim directive active


# --- tracker roadmap as the informal source ----------------------------------

def _tracker_body() -> str:
    return (
        "# LeeSM Tracker\n\nSome preamble about the project.\n\n"
        "### Chapter 14: Forms\n"
        "1. (p. 400) Define differential forms🟡\n\n"
        "### Chapter 15: Orientations\n"
        "1. (p. 379) Proposition 15.4: Pointwise orientations🟡\n"
        "2. (p. 385) Define the orientation of a manifold🟡\n\n"
        "### Chapter 16: Integration\n"
        "1. (p. 401) Define integration of forms🟡\n"
    )


def test_tracker_chapter_section_extracts_only_that_chapter(tmp_path, monkeypatch):
    import marathon.review.github as ghmod
    from marathon.review.chapter_intake import tracker_chapter_section

    monkeypatch.setattr(ghmod, "issue_body", lambda num, repo: _tracker_body())
    sec = tracker_chapter_section(_cfg(tmp_path), 15)
    assert sec is not None
    assert "### Chapter 15: Orientations" in sec
    assert "Pointwise orientations" in sec and "orientation of a manifold" in sec
    assert "differential forms" not in sec      # bounded below (Ch14 excluded)
    assert "integration of forms" not in sec    # bounded above (Ch16 excluded)


def test_intake_from_tracker_embeds_section_with_anchor_directive(tmp_path, monkeypatch):
    import marathon.review.github as ghmod

    monkeypatch.setattr(ghmod, "issue_body", lambda num, repo: _tracker_body())
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    cap: dict = {}
    res = run_chapter_intake(
        cfg, 15, use_tracker=True, _runner=_runner(_GOOD_DRAFTS, capture=cap)
    )
    assert res.ok
    assert "Pointwise orientations" in cap["prompt"]            # Ch15 section embedded
    assert "orientation of a manifold" in cap["prompt"]
    assert "integration of forms" not in cap["prompt"]          # bounded (no Ch16)
    assert "Anchored to the project tracker roadmap" in cap["prompt"]  # anchor directive
    assert "tracker roadmap" in cap["prompt"]


def test_intake_tracker_missing_section_is_error_not_raise(tmp_path, monkeypatch):
    import marathon.review.github as ghmod

    monkeypatch.setattr(  # tracker has no Chapter 15 section
        ghmod, "issue_body", lambda num, repo: "### Chapter 14: Forms\n1. x🟡\n"
    )
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    res = run_chapter_intake(cfg, 15, use_tracker=True, _runner=_runner(_GOOD_DRAFTS))
    assert not res.ok
    assert "tracker roadmap" in (res.error or "") and "Chapter 15" in (res.error or "")
    assert not drafts_path_for(cfg, 15).is_file()  # nothing written


def test_intake_raises_output_token_cap(tmp_path, monkeypatch):
    """The intake lifts CLAUDE_CODE_MAX_OUTPUT_TOKENS so a 10-draft, max-effort
    decomposition doesn't die on the 32k default (the Chapter 16 failure)."""
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    monkeypatch.delenv("MARATHON_INTAKE_MAX_OUTPUT_TOKENS", raising=False)
    cap = {}
    run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS, capture=cap))
    assert cap["env_overrides"] == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"}


def test_intake_output_token_cap_env_override(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    monkeypatch.setenv("MARATHON_INTAKE_MAX_OUTPUT_TOKENS", "96000")
    cap = {}
    run_chapter_intake(cfg, 15, _runner=_runner(_GOOD_DRAFTS, capture=cap))
    assert cap["env_overrides"] == {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "96000"}
