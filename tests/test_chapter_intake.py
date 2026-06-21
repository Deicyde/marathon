"""Headless chapter intake: gather chapter source → one Claude completion →
parse drafts → write the drafts file, never raising on failure."""
from __future__ import annotations

import subprocess

from marathon.review.chapter_intake import drafts_path_for, run_chapter_intake
from marathon.review.config import ReviewConfig, ReviewLabels


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
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


def _write_chapter(cfg, chapter, body="theorem foo : True := by sorry"):
    d = cfg.target_path(chapter)
    d.mkdir(parents=True, exist_ok=True)
    (d / "Foo.lean").write_text(
        f"namespace Demo.Chapter{chapter}\n\n{body}\n\nend Demo.Chapter{chapter}\n"
    )


_GOOD_DRAFTS = """## 1/2 — Theorem foo

**Title**: `Theorem 15.1 — foo is true`

**Parent**: #1

### Lean signatures

```lean
theorem foo : True
```

### Informal Statement

*⚠️ LLM-rendered from common knowledge; verification pending.*

> foo holds.

## 2/2 — Lemma bar

**Title**: `Lemma 15.2 — bar`

### Lean signatures

```lean
lemma bar : True
```

## Coverage summary

Two first-class results.
"""


def _runner(out, code=0, err="", capture=None):
    def _run(prompt, *, model=None, timeout=None):
        if capture is not None:
            capture["prompt"] = prompt
        return subprocess.CompletedProcess(["claude"], code, out, err)
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


def test_intake_claude_nonzero_exit_is_an_error(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)
    res = run_chapter_intake(cfg, 15, _runner=_runner("", code=1, err="boom"))
    assert not res.ok
    assert "exited 1" in (res.error or "") and "boom" in (res.error or "")


def test_intake_runner_exception_is_caught(tmp_path):
    cfg = _cfg(tmp_path)
    _write_chapter(cfg, 15)

    def _boom(prompt, *, model=None, timeout=None):
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
