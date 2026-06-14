"""Tests for marathon.extraction — the firewall-gated textbook intake.

Two extraction paths, gated by a per-project mode:

* **copyrighted (default)**: NO Claude reads the source .tex. Targets come
  from a human-supplied informal-statements markdown file and/or a list of
  named results. The firewall is enforced in code (a .tex is refused) and
  the assembled normalize prompt provably carries zero source text.

* **open (opt-in)**: the autoform consensus pipeline — chunk -> K extractor
  calls -> consensus -> reviewer arbitration -> merge dedup. Robust to
  Claude failures: a dropped call yields survivor consensus; an all-fail
  chunk yields no targets; nothing crashes.

All Claude is monkeypatched — fully offline, no real ``claude``, no
network, no lake/Aristotle.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from marathon import extraction
from marathon.extraction import (
    DEFAULT_SOURCE_MODE,
    ExtractionError,
    chunk_text,
    extract_targets,
    parse_informal_statements,
    parse_statement_list,
)


# ---------------------------------------------------------------------------
# Claude monkeypatch harness
# ---------------------------------------------------------------------------


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=""
    )


def _install_claude(monkeypatch, responder, *, capture: list | None = None):
    """Patch ``extraction.run_claude`` with a callable ``responder(prompt)``
    returning either a string (stdout, rc=0), a CompletedProcess, or an
    exception instance to raise. Optionally record every prompt in
    ``capture``."""

    def fake_run(prompt, *, model=None, timeout=None, extra_args=()):
        if capture is not None:
            capture.append(prompt)
        result = responder(prompt)
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, subprocess.CompletedProcess):
            return result
        return _proc(result)

    monkeypatch.setattr(extraction, "run_claude", fake_run)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INFORMAL_MD = """\
# Theorem 14.23 (Stokes' Theorem)

For a compactly supported smooth (n-1)-form on an oriented smooth n-manifold
with boundary, the integral of its exterior derivative equals the integral
over the boundary.

# Definition 14.1

A smooth n-form is a smooth section of the n-th exterior power of the
cotangent bundle.

## Lemma 14.5

The exterior derivative is natural with respect to pullback.
"""

OPEN_TEX = "\n".join(
    [f"line {i}" for i in range(1, 8)]
    + [
        "Theorem 1.1 (Heine-Borel). A subset of R^n is compact iff closed and bounded.",
        "Proof. Omitted.",
        "Definition 1.2. A space is compact if every open cover has a finite subcover.",
    ]
)


def _stmt_json(*entries: dict) -> str:
    return json.dumps(list(entries))


# ---------------------------------------------------------------------------
# parse_statement_list — lenient JSON
# ---------------------------------------------------------------------------


def test_parse_bare_json_list():
    out = parse_statement_list('[{"name": "Theorem 1"}]')
    assert out == [{"name": "Theorem 1"}]


def test_parse_fenced_json_list():
    resp = 'Here you go:\n```json\n[{"name": "Lemma 2"}]\n```\nDone.'
    assert parse_statement_list(resp) == [{"name": "Lemma 2"}]


def test_parse_embedded_json_list():
    resp = 'The statements are [{"name": "Def 3"}] in this chunk.'
    assert parse_statement_list(resp) == [{"name": "Def 3"}]


def test_parse_empty_list_is_empty_not_none():
    assert parse_statement_list("[]") == []


def test_parse_garbage_is_none():
    assert parse_statement_list("I could not find anything useful.") is None
    assert parse_statement_list("") is None


# ---------------------------------------------------------------------------
# Copyrighted path: parse the human informal-statements file into targets
# ---------------------------------------------------------------------------


def test_parse_informal_statements_sections():
    sections = parse_informal_statements(INFORMAL_MD)
    names = [s.name for s in sections]
    assert names == ["Theorem 14.23", "Definition 14.1", "Lemma 14.5"]
    # Trailing parenthetical is split off as a citation.
    assert sections[0].citation == "Stokes' Theorem"
    assert "exterior derivative" in sections[0].statement
    assert sections[1].citation == ""


def test_copyrighted_parses_informal_file_into_targets(tmp_path, monkeypatch):
    # No Claude needed for the non-normalized copyrighted path — but install
    # a tripwire that fails loudly if it's ever called.
    _install_claude(
        monkeypatch,
        lambda p: pytest.fail("Claude must not be called without --normalize"),
    )
    f = tmp_path / "informal.md"
    f.write_text(INFORMAL_MD)

    targets = extract_targets(
        None, mode="copyrighted", informal_statements=f
    )

    assert len(targets) == 3
    assert {t["source_mode"] for t in targets} == {"copyrighted"}
    by_name = {t["name"]: t for t in targets}
    assert by_name["Theorem 14.23"]["kind"] == "theorem"
    assert by_name["Definition 14.1"]["kind"] == "definition"
    assert by_name["Lemma 14.5"]["kind"] == "lemma"
    # The statement is the human's verbatim wording.
    assert "exterior derivative" in by_name["Theorem 14.23"]["statement"]
    # Provenance points at the human file, never the book.
    assert str(f) in by_name["Theorem 14.23"]["source_ref"]


def test_copyrighted_named_results_list(tmp_path, monkeypatch):
    _install_claude(monkeypatch, lambda p: pytest.fail("no Claude"))
    targets = extract_targets(
        None,
        mode="copyrighted",
        named_results=["Theorem 5.1 (Frobenius)", "Definition 5.2"],
    )
    assert [t["name"] for t in targets] == [
        "Theorem 5.1 (Frobenius)",
        "Definition 5.2",
    ]
    assert targets[0]["kind"] == "theorem"
    assert all(t["source_ref"] == "human-list" for t in targets)
    assert all(t["source_mode"] == "copyrighted" for t in targets)


def test_copyrighted_requires_a_human_source():
    with pytest.raises(ExtractionError, match="requires a human source"):
        extract_targets(None, mode="copyrighted")


# ---------------------------------------------------------------------------
# Firewall: copyrighted mode REFUSES a .tex
# ---------------------------------------------------------------------------


def test_copyrighted_refuses_tex_as_informal_file(tmp_path):
    tex = tmp_path / "Chapter14.tex"
    tex.write_text("\\begin{theorem} secret copyrighted content \\end{theorem}")
    with pytest.raises(ExtractionError, match="firewall"):
        extract_targets(None, mode="copyrighted", informal_statements=tex)


def test_copyrighted_refuses_tex_passed_as_source(tmp_path):
    tex = tmp_path / "book.tex"
    tex.write_text("\\theorem copyrighted")
    with pytest.raises(ExtractionError, match="firewall"):
        extract_targets(tex, mode="copyrighted", named_results=["Theorem 1"])


def test_firewall_guard_rejects_latex_variants(tmp_path):
    for suffix in (".tex", ".latex", ".ltx"):
        p = tmp_path / f"book{suffix}"
        p.write_text("copyrighted")
        with pytest.raises(ExtractionError, match="firewall"):
            extract_targets(None, mode="copyrighted", informal_statements=p)


# ---------------------------------------------------------------------------
# Firewall: the assembled copyrighted prompt carries ZERO source text
# ---------------------------------------------------------------------------


def test_copyrighted_normalize_prompt_has_no_source_text(tmp_path, monkeypatch):
    """With --normalize, Claude IS called — but the prompt must contain only
    the human's wording, never the copyrighted book. We assert the prompt
    holds the human statement and assert there is no book content (we make
    the book content a distinctive sentinel that, if it ever leaked, would
    appear in the captured prompts)."""
    SENTINEL = "COPYRIGHTED_BOOK_SENTINEL_DO_NOT_LEAK"
    # The book file exists on disk but must never be read by this path.
    book = tmp_path / "Chapter14.tex"
    book.write_text(f"\\begin{{theorem}} {SENTINEL} \\end{{theorem}}")

    f = tmp_path / "informal.md"
    f.write_text(INFORMAL_MD)

    captured: list[str] = []
    _install_claude(
        monkeypatch,
        lambda p: _stmt_json(
            {"name": "Theorem 14.23", "statement": "normalized stokes",
             "kind": "theorem", "citation": "Stokes' Theorem"}
        ),
        capture=captured,
    )

    targets = extract_targets(
        None, mode="copyrighted", informal_statements=f, normalize=True
    )

    assert captured, "normalize should have invoked Claude"
    blob = "\n".join(captured)
    # The book sentinel never appears in any prompt.
    assert SENTINEL not in blob
    # The human's wording IS present (that's the only thing Claude sees).
    assert "exterior derivative" in blob
    # The normalized statement made it through.
    assert any("normalized stokes" in t["statement"] for t in targets)
    assert all(t["source_mode"] == "copyrighted" for t in targets)


def test_normalize_keeps_human_text_on_claude_failure(tmp_path, monkeypatch):
    f = tmp_path / "informal.md"
    f.write_text("# Theorem 1\n\nhuman verbatim statement\n")
    _install_claude(monkeypatch, lambda p: _proc("", returncode=1))
    targets = extract_targets(
        None, mode="copyrighted", informal_statements=f, normalize=True
    )
    assert targets[0]["statement"] == "human verbatim statement"


# ---------------------------------------------------------------------------
# Open path: chunking
# ---------------------------------------------------------------------------


def test_chunk_text_overlaps():
    text = "\n".join(f"L{i}" for i in range(1, 13))
    chunks = chunk_text(text, chunk_size=5, overlap=2)
    assert chunks[0].start_line == 1 and chunks[0].end_line == 5
    # Advance = chunk_size - overlap = 3.
    assert chunks[1].start_line == 4
    assert chunks[-1].end_line == 12


def test_chunk_text_empty():
    assert chunk_text("") == []


def test_chunk_overlap_clamped_to_advance():
    # overlap >= chunk_size would never advance; it's clamped.
    chunks = chunk_text("\n".join(f"L{i}" for i in range(10)), chunk_size=3, overlap=5)
    assert len(chunks) >= 1
    assert chunks[-1].end_line == 10


# ---------------------------------------------------------------------------
# Open path: unanimous consensus
# ---------------------------------------------------------------------------

_HB = {"name": "Theorem 1.1 (Heine-Borel)", "statement": "compact iff closed+bounded",
       "kind": "theorem", "citation": "Section 1"}
_DEF = {"name": "Definition 1.2", "statement": "compact = finite subcover",
        "kind": "definition", "citation": "Section 1"}


def test_open_unanimous_consensus_accepts(tmp_path, monkeypatch):
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    # Every extractor call returns the same two statements (single chunk).
    _install_claude(monkeypatch, lambda p: _stmt_json(_HB, _DEF))
    targets = extract_targets(
        src, mode="open", k=4, chunk_size=500, overlap=50
    )
    names = sorted(t["name"] for t in targets)
    assert names == ["Definition 1.2", "Theorem 1.1 (Heine-Borel)"]
    assert all(t["source_mode"] == "open" for t in targets)
    # source_ref carries the book path + line range provenance.
    assert str(src) in targets[0]["source_ref"]


def test_open_disputed_statement_is_arbitrated(tmp_path, monkeypatch):
    """One extractor adds an extra statement the others missed; the reviewer
    arbitration call confirms it, so it's included."""
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    extra = {"name": "Lemma 1.3", "statement": "an extra lemma",
             "kind": "lemma", "citation": "Section 1"}

    calls = {"n": 0}

    def responder(prompt: str) -> str:
        # The reviewer/arbitration prompt is distinguishable by its header.
        if "Arbitration" in prompt:
            return _stmt_json(extra)  # arbiter confirms the disputed lemma
        calls["n"] += 1
        if calls["n"] == 1:
            return _stmt_json(_HB, _DEF, extra)  # first extractor sees 3
        return _stmt_json(_HB, _DEF)  # the rest see 2

    _install_claude(monkeypatch, responder)
    targets = extract_targets(src, mode="open", k=4)
    names = sorted(t["name"] for t in targets)
    assert names == ["Definition 1.2", "Lemma 1.3", "Theorem 1.1 (Heine-Borel)"]


def test_open_disputed_rejected_by_arbiter_is_dropped(tmp_path, monkeypatch):
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    bogus = {"name": "Lemma 9.9", "statement": "a hallucinated lemma",
             "kind": "lemma", "citation": ""}
    calls = {"n": 0}

    def responder(prompt: str) -> str:
        if "Arbitration" in prompt:
            return "[]"  # arbiter rejects the disputed statement
        calls["n"] += 1
        if calls["n"] == 1:
            return _stmt_json(_HB, _DEF, bogus)
        return _stmt_json(_HB, _DEF)

    _install_claude(monkeypatch, responder)
    targets = extract_targets(src, mode="open", k=3)
    names = sorted(t["name"] for t in targets)
    assert "Lemma 9.9" not in names
    assert names == ["Definition 1.2", "Theorem 1.1 (Heine-Borel)"]


# ---------------------------------------------------------------------------
# Open path: cross-chunk dedup (merge)
# ---------------------------------------------------------------------------


def test_open_cross_chunk_dedup(tmp_path, monkeypatch):
    """Overlapping windows re-emit the same labeled statement; the merge
    step dedups it by name so it appears once."""
    # Force two overlapping chunks; both will surface Heine-Borel.
    long_text = "\n".join(f"filler {i}" for i in range(20))
    src = tmp_path / "open.md"
    src.write_text(long_text)
    _install_claude(monkeypatch, lambda p: _stmt_json(_HB))  # every call: HB
    targets = extract_targets(
        src, mode="open", k=2, chunk_size=8, overlap=4
    )
    # Despite multiple chunks each emitting Heine-Borel, it appears once.
    assert [t["name"] for t in targets] == ["Theorem 1.1 (Heine-Borel)"]


# ---------------------------------------------------------------------------
# Open path: robustness to Claude failures
# ---------------------------------------------------------------------------


def test_open_survivor_consensus_when_one_call_fails(tmp_path, monkeypatch):
    """One of k extractor calls fails (non-zero exit); consensus is taken
    over the survivors, so the statement all survivors agree on is kept."""
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    calls = {"n": 0}

    def responder(prompt: str):
        calls["n"] += 1
        if calls["n"] == 2:
            return _proc("", returncode=1)  # second call dies
        return _stmt_json(_HB, _DEF)

    _install_claude(monkeypatch, responder)
    targets = extract_targets(src, mode="open", k=4)
    names = sorted(t["name"] for t in targets)
    # Survivors (3 of them) unanimously agree -> both accepted.
    assert names == ["Definition 1.2", "Theorem 1.1 (Heine-Borel)"]


def test_open_single_survivor_is_not_self_consensus(tmp_path, monkeypatch):
    """A LONE survivor is not its own consensus. When all but one extractor
    call fails (the rate-limiting case), the single survivor's finds are NOT
    auto-accepted — they must clear the reviewer arbitration call, which is
    the missing second independent look. Here the arbiter confirms only the
    real statement and drops the lone survivor's hallucination."""
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    real = {"name": "Theorem 1.1 (Heine-Borel)", "statement": "real",
            "kind": "theorem", "citation": ""}
    halluc = {"name": "Lemma 9.9", "statement": "hallucinated by the lone call",
              "kind": "lemma", "citation": ""}
    calls = {"n": 0}

    def responder(prompt: str):
        if "Arbitration" in prompt:
            return _stmt_json(real)  # arbiter confirms only the real one
        calls["n"] += 1
        if calls["n"] == 1:
            return _stmt_json(real, halluc)  # the sole survivor
        return _proc("", returncode=1)  # every other extractor call dies

    _install_claude(monkeypatch, responder)
    targets = extract_targets(src, mode="open", k=4)
    names = sorted(t["name"] for t in targets)
    # The lone survivor's hallucination is NOT auto-accepted; only the
    # arbiter-confirmed statement survives.
    assert names == ["Theorem 1.1 (Heine-Borel)"]


def test_open_single_survivor_dropped_when_arbiter_also_fails(tmp_path, monkeypatch):
    """If the only extractor survivor's finds are unconfirmable (the arbiter
    call also fails under rate-limiting), nothing is accepted — a lone
    uncorroborated call never lands a target."""
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    calls = {"n": 0}

    def responder(prompt: str):
        if "Arbitration" in prompt:
            return _proc("", returncode=1)  # arbiter unavailable too
        calls["n"] += 1
        if calls["n"] == 1:
            return _stmt_json(_HB, _DEF)
        return _proc("", returncode=1)

    _install_claude(monkeypatch, responder)
    assert extract_targets(src, mode="open", k=4) == []


def test_open_never_crashes_on_all_fail(tmp_path, monkeypatch):
    """Every Claude call raises/fails — extraction returns [] rather than
    crashing."""
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    _install_claude(monkeypatch, lambda p: OSError("boom"))
    targets = extract_targets(src, mode="open", k=4)
    assert targets == []


def test_open_handles_exit_failure_all_calls(tmp_path, monkeypatch):
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    _install_claude(monkeypatch, lambda p: _proc("garbage", returncode=2))
    assert extract_targets(src, mode="open", k=3) == []


def test_open_timeout_is_swallowed(tmp_path, monkeypatch):
    src = tmp_path / "open.md"
    src.write_text(OPEN_TEX)
    _install_claude(
        monkeypatch,
        lambda p: subprocess.TimeoutExpired(cmd="claude", timeout=1),
    )
    assert extract_targets(src, mode="open", k=2) == []


# ---------------------------------------------------------------------------
# Open path: source reading variants + missing source
# ---------------------------------------------------------------------------


def test_open_reads_directory_of_files(tmp_path, monkeypatch):
    book = tmp_path / "book"
    book.mkdir()
    (book / "01.md").write_text("Theorem 1.1 (Heine-Borel). foo.")
    (book / "02.tex").write_text("Definition 1.2. bar.")
    _install_claude(monkeypatch, lambda p: _stmt_json(_HB))
    targets = extract_targets(book, mode="open", k=2, chunk_size=500)
    assert targets[0]["name"] == "Theorem 1.1 (Heine-Borel)"


def test_open_missing_source_raises():
    with pytest.raises(ExtractionError, match="source not found"):
        extract_targets("/nonexistent/path.md", mode="open")


def test_open_requires_source():
    with pytest.raises(ExtractionError, match="requires a --source"):
        extract_targets(None, mode="open")


# ---------------------------------------------------------------------------
# Dispatch / mode validation
# ---------------------------------------------------------------------------


def test_unknown_mode_raises():
    with pytest.raises(ExtractionError, match="unknown source mode"):
        extract_targets(None, mode="bogus")


def test_default_mode_is_copyrighted():
    assert DEFAULT_SOURCE_MODE == "copyrighted"


# ---------------------------------------------------------------------------
# extract.md prompt: per-mode notes present + firewall language
# ---------------------------------------------------------------------------


def test_extract_md_has_both_mode_notes():
    prompt = (Path(extraction.__file__).parent / "prompts" / "extract.md").read_text()
    low = prompt.lower()
    assert "open mode" in low
    assert "copyrighted mode" in low
    # Open: from the provided chunk.
    assert "from the provided text chunk" in low
    # Copyrighted: normalize the human-supplied statement only; book off-limits.
    assert "normalize" in low
    assert "off-limits" in low or "not being shown" in low
    # Strict JSON list output instruction.
    assert "json list" in low


def test_extract_md_lists_statement_kinds():
    prompt = (Path(extraction.__file__).parent / "prompts" / "extract.md").read_text()
    low = prompt.lower()
    for kw in ("theorem", "definition", "lemma", "proposition", "corollary"):
        assert kw in low
