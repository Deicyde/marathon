"""Tests for marathon.gate — the mode-aware quality-gate engine.

The gate is pure and deterministic, so everything here runs on fixture
``.lean`` trees under tmp_path: no git, no lake, no network. The one
external seam — ``formalization.check_axioms``, which spawns
``lake env lean`` — is monkeypatched in every test that can reach it
(any ``build_ok=True`` run), which also pins down the call contract
(qualified decl names + dotted module paths) the real implementation
expects.
"""

import textwrap
from pathlib import Path

import pytest

from marathon import formalization
from marathon.gate import (
    CHECK_AXIOMS,
    CHECK_BUILD,
    CHECK_FORBIDDEN,
    CHECK_SORRIES,
    LEVEL_FAIL,
    LEVEL_INFO,
    LEVEL_WARN,
    MODE_PROOF,
    MODE_SKELETON,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_SKIP,
    STATUS_WARN,
    SorryCounts,
    measure_sorries,
    run_gate,
)


# --- fixture helpers ----------------------------------------------------------


def _make_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Repo root + target chapter folder, like a marathon consumer repo."""
    repo = tmp_path / "repo"
    target = repo / "Chapter1"
    target.mkdir(parents=True)
    return repo, target


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


CLEAN_LEAN = """\
    import Mathlib

    theorem clean : True := trivial
    """

# One theorem-body sorry + one definition-body sorry.
MIXED_SORRY_LEAN = """\
    import Mathlib

    theorem thmA : True := by
      sorry

    def defB : Nat := by
      sorry
    """

THEOREM_SORRY_LEAN = """\
    import Mathlib

    theorem thmOnly : True := by
      sorry
    """


def _run(repo, target, *, mode=MODE_PROOF, build_ok=None, **kwargs):
    return run_gate(repo, target, mode=mode, build_ok=build_ok, **kwargs)


def _findings(report, check_name, level=None):
    check = report.check(check_name)
    assert check is not None, f"check {check_name} missing from report"
    if level is None:
        return check.findings
    return [f for f in check.findings if f.level == level]


# --- build check ----------------------------------------------------------------


def test_build_ok_none_skips_with_reason(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    report = _run(repo, target, build_ok=None)
    build = report.check(CHECK_BUILD)
    assert build.status == STATUS_SKIP
    assert "no build info" in build.summary
    # A skipped build never degrades the verdict by itself.
    assert report.verdict == STATUS_PASS


def test_build_pass(tmp_path, monkeypatch):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    # build_ok=True lets the gate reach the axiom check; stub the
    # `lake env lean` seam so this stays hermetic (no lake on PATH needed).
    monkeypatch.setattr(
        formalization, "check_axioms",
        lambda repo_dir, pairs, **kw: {d: ["propext"] for d, _ in pairs},
    )
    report = _run(repo, target, build_ok=True)
    assert report.check(CHECK_BUILD).status == STATUS_PASS


def test_build_fail_fails_gate_and_carries_log_tail(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    report = _run(repo, target, build_ok=False, build_log_tail="error: kaboom")
    build = report.check(CHECK_BUILD)
    assert build.status == STATUS_FAIL
    assert report.verdict == STATUS_FAIL
    info = [f for f in build.findings if f.level == LEVEL_INFO]
    assert any("kaboom" in f.message for f in info)


# --- axiom check -----------------------------------------------------------------


AXIOMS_LEAN = """\
    import Mathlib

    namespace Geo

    theorem clean : True := trivial

    theorem sorryBacked : True := by sorry

    theorem bad : True := trivial

    end Geo

    def Standalone : Nat := 0
    """


def test_axioms_whitelist_and_sorry_ax_accounting(tmp_path, monkeypatch):
    repo, target = _make_repo(tmp_path)
    _write(target / "Axioms.lean", AXIOMS_LEAN)

    captured: dict = {}

    def fake_check_axioms(repo_dir, decl_to_module, *, timeout=120):
        captured["repo_dir"] = repo_dir
        captured["pairs"] = decl_to_module
        return {
            "Geo.clean": ["propext", "Classical.choice", "Quot.sound"],
            "Geo.sorryBacked": ["propext", "sorryAx"],
            "Geo.bad": ["propext", "Geo.shadyAxiom"],
            "Standalone": None,  # undetermined
        }

    monkeypatch.setattr(formalization, "check_axioms", fake_check_axioms)
    report = _run(repo, target, build_ok=True)

    # Discovery passed namespace-qualified names + dotted module paths.
    assert captured["pairs"] == [
        ("Geo.clean", "Chapter1.Axioms"),
        ("Geo.sorryBacked", "Chapter1.Axioms"),
        ("Geo.bad", "Chapter1.Axioms"),
        ("Standalone", "Chapter1.Axioms"),
    ]

    axioms = report.check(CHECK_AXIOMS)
    assert axioms.status == STATUS_FAIL
    assert report.verdict == STATUS_FAIL

    fails = [f for f in axioms.findings if f.level == LEVEL_FAIL]
    assert len(fails) == 1
    assert "Geo.bad" in fails[0].message
    assert "Geo.shadyAxiom" in fails[0].message
    assert fails[0].file == "Chapter1/Axioms.lean"
    assert fails[0].line == 9  # the `theorem bad` line

    # sorryAx is ACCOUNTED (info, per decl), never a failure by itself.
    infos = [f for f in axioms.findings if f.level == LEVEL_INFO]
    assert any("Geo.sorryBacked" in f.message and "sorryAx" in f.message for f in infos)

    # The undetermined decl is reported in the summary, not failed.
    assert "1 undetermined" in axioms.summary


def test_sorry_ax_alone_passes_axiom_check(tmp_path, monkeypatch):
    repo, target = _make_repo(tmp_path)
    _write(target / "Axioms.lean", AXIOMS_LEAN)

    def fake_check_axioms(repo_dir, decl_to_module, *, timeout=120):
        return {decl: ["propext", "sorryAx"] for decl, _ in decl_to_module}

    monkeypatch.setattr(formalization, "check_axioms", fake_check_axioms)
    report = _run(repo, target, build_ok=True)
    assert report.check(CHECK_AXIOMS).status == STATUS_PASS
    assert report.verdict == STATUS_PASS


@pytest.mark.parametrize("build_ok", [False, None])
def test_axioms_skipped_without_green_build(tmp_path, monkeypatch, build_ok):
    repo, target = _make_repo(tmp_path)
    _write(target / "Axioms.lean", AXIOMS_LEAN)

    def explode(*args, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("check_axioms must not run without a green build")

    monkeypatch.setattr(formalization, "check_axioms", explode)
    report = _run(repo, target, build_ok=build_ok)
    axioms = report.check(CHECK_AXIOMS)
    assert axioms.status == STATUS_SKIP
    assert "successful build" in axioms.summary


# --- sorry accounting -------------------------------------------------------------


def test_measure_sorries_splits_definition_bodies(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", MIXED_SORRY_LEAN)
    counts = measure_sorries(target)
    assert counts == SorryCounts(total=2, definitions=1)


def test_no_baseline_reports_counts_without_delta(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", MIXED_SORRY_LEAN)
    report = _run(repo, target, mode=MODE_PROOF)
    sorries = report.check(CHECK_SORRIES)
    assert sorries.status == STATUS_PASS
    assert "no baseline" in sorries.summary


def test_mode_difference_same_folder_new_def_sorries(tmp_path):
    """Same folder, same baseline: skeleton warns, proof fails."""
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", MIXED_SORRY_LEAN)
    baseline = SorryCounts(total=0, definitions=0)

    proof = _run(repo, target, mode=MODE_PROOF, prev_sorry_counts=baseline)
    assert proof.check(CHECK_SORRIES).status == STATUS_FAIL
    assert proof.verdict == STATUS_FAIL

    skeleton = _run(repo, target, mode=MODE_SKELETON, prev_sorry_counts=baseline)
    assert skeleton.check(CHECK_SORRIES).status == STATUS_WARN
    assert skeleton.verdict == STATUS_WARN
    warns = _findings(skeleton, CHECK_SORRIES, LEVEL_WARN)
    assert any("definition bodies" in f.message for f in warns)


def test_skeleton_theorem_sorries_expected_proof_regresses(tmp_path):
    """Theorem-body-only sorries: skeleton's expected product passes;
    proof mode treats the same delta as a regression."""
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", THEOREM_SORRY_LEAN)
    baseline = SorryCounts(total=0, definitions=0)

    skeleton = _run(repo, target, mode=MODE_SKELETON, prev_sorry_counts=baseline)
    assert skeleton.check(CHECK_SORRIES).status == STATUS_PASS
    assert skeleton.verdict == STATUS_PASS

    proof = _run(repo, target, mode=MODE_PROOF, prev_sorry_counts=baseline)
    assert proof.check(CHECK_SORRIES).status == STATUS_FAIL


def test_unchanged_counts_pass_both_modes(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", MIXED_SORRY_LEAN)
    baseline = SorryCounts(total=2, definitions=1)
    for mode in (MODE_SKELETON, MODE_PROOF):
        report = _run(repo, target, mode=mode, prev_sorry_counts=baseline)
        assert report.check(CHECK_SORRIES).status == STATUS_PASS


def test_proof_mode_catches_def_sorries_masked_by_flat_total(tmp_path):
    """A proof-side removal must not mask a new sorry'd definition."""
    repo, target = _make_repo(tmp_path)
    _write(
        target / "A.lean",
        """\
        def defB : Nat := by
          sorry
        """,
    )
    baseline = SorryCounts(total=2, definitions=0)  # total goes DOWN
    report = _run(repo, target, mode=MODE_PROOF, prev_sorry_counts=baseline)
    assert report.check(CHECK_SORRIES).status == STATUS_FAIL


def test_baseline_accepts_json_shaped_mapping(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", MIXED_SORRY_LEAN)
    report = _run(
        repo, target, mode=MODE_PROOF,
        prev_sorry_counts={"total": 2, "definitions": 1},
    )
    assert report.check(CHECK_SORRIES).status == STATUS_PASS


# --- forbidden keywords ------------------------------------------------------------


TAMPER_LEAN = """\
    -- macro mentioned in a comment is fine
    notation "⟦" x "⟧" => id x
    local notation:65 a " ⊞ " b => a + b
    macro "solveIt" : tactic => `(tactic| trivial)
    elab "myTerm" : term => pure default
    syntax "weird" : term
    macro_rules | `(weird) => `(42)
    def macroHelper : Nat := 0
    """


def test_forbidden_keywords_cited_by_file_and_line(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "Tamper.lean", TAMPER_LEAN)
    report = _run(repo, target)
    forbidden = report.check(CHECK_FORBIDDEN)
    assert forbidden.status == STATUS_FAIL
    assert report.verdict == STATUS_FAIL

    by_line = {f.line: f for f in forbidden.findings}
    # notation (incl. `local notation:65`) is warn-level …
    assert by_line[2].level == LEVEL_WARN
    assert by_line[3].level == LEVEL_WARN
    # … the tampering-capable commands are fail-level.
    for line, keyword in [(4, "macro"), (5, "elab"), (6, "syntax"), (7, "macro_rules")]:
        assert by_line[line].level == LEVEL_FAIL
        assert f"`{keyword}`" in by_line[line].message
    # Citations are repo-relative.
    assert all(f.file == "Chapter1/Tamper.lean" for f in forbidden.findings)
    # The comment line and the `macroHelper` identifier are NOT flagged.
    assert 1 not in by_line
    assert 8 not in by_line


def test_notation_alone_is_warn_not_fail(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", 'notation "⟦" x "⟧" => id x\n')
    report = _run(repo, target)
    assert report.check(CHECK_FORBIDDEN).status == STATUS_WARN
    assert report.verdict == STATUS_WARN


def test_clean_folder_passes_forbidden_check(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    report = _run(repo, target)
    assert report.check(CHECK_FORBIDDEN).status == STATUS_PASS


# --- engine contract ---------------------------------------------------------------


def test_unknown_mode_raises(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    with pytest.raises(ValueError, match="unknown gate mode"):
        run_gate(repo, target, mode="banana", build_ok=None)


def test_target_outside_repo_raises(tmp_path):
    repo, _ = _make_repo(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError, match="not under repo_dir"):
        run_gate(repo, outside, mode=MODE_PROOF, build_ok=None)


def test_missing_target_skips_folder_checks(tmp_path):
    repo, target = _make_repo(tmp_path)
    missing = repo / "Chapter99"
    report = run_gate(repo, missing, mode=MODE_PROOF, build_ok=True)
    for name in (CHECK_SORRIES, CHECK_FORBIDDEN):
        check = report.check(name)
        assert check.status == STATUS_SKIP
        assert "not found" in check.summary


def test_to_dict_carries_structure_for_wiring(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    report = _run(repo, target, build_ok=False)
    d = report.to_dict()
    assert d["verdict"] == STATUS_FAIL
    assert d["mode"] == MODE_PROOF
    names = [c["name"] for c in d["checks"]]
    assert names == [CHECK_BUILD, CHECK_AXIOMS, CHECK_SORRIES, CHECK_FORBIDDEN]
    assert all("findings" in c and "status" in c for c in d["checks"])


# --- rendering ---------------------------------------------------------------------


def test_render_markdown_smoke(tmp_path):
    repo, target = _make_repo(tmp_path)
    _write(target / "Tamper.lean", TAMPER_LEAN)
    report = _run(repo, target, build_ok=False, build_log_tail="error: boom")
    md = report.render_markdown()
    # Per-check table lines.
    for name in (CHECK_BUILD, CHECK_AXIOMS, CHECK_SORRIES, CHECK_FORBIDDEN):
        assert f"| {name} |" in md
    assert "**FAIL**" in md
    # Warn/fail findings carry citations into the PR body.
    assert "Chapter1/Tamper.lean:4" in md
    # The no-faithfulness footer is load-bearing messaging.
    assert "faithfulness review stays human" in md


def test_render_console_smoke(tmp_path, monkeypatch):
    repo, target = _make_repo(tmp_path)
    _write(target / "A.lean", CLEAN_LEAN)
    # build_ok=True reaches the axiom check; stub the lake seam (hermetic).
    monkeypatch.setattr(
        formalization, "check_axioms",
        lambda repo_dir, pairs, **kw: {d: ["propext"] for d, _ in pairs},
    )
    report = _run(repo, target, build_ok=True, mode=MODE_SKELETON)
    text = report.render_console()
    assert "skeleton" in text
    for name in (CHECK_BUILD, CHECK_AXIOMS, CHECK_SORRIES, CHECK_FORBIDDEN):
        assert name in text


# --- shared scanner (gate's discovery dependency) ------------------------------------


def test_scan_lean_source_qualifies_names_through_nesting():
    decls, orphans = formalization.scan_lean_source(
        textwrap.dedent(
            """\
            namespace A
            section
            namespace B.C
            theorem t1 : True := trivial
            end B.C
            end
            theorem _root_.Top : True := trivial
            theorem inA : True := trivial
            instance : Inhabited Nat := ⟨0⟩
            end A
            """
        )
    )
    assert orphans == 0
    by_name = {d.qualified: d for d in decls if d.qualified}
    assert "A.B.C.t1" in by_name
    assert "Top" in by_name  # _root_ escapes the namespace stack
    assert "A.inA" in by_name
    # Anonymous instances are discovered but carry no qualified name.
    anon = [d for d in decls if d.qualified is None]
    assert len(anon) == 1 and anon[0].kind == "instance"


def test_scan_lean_source_attributes_sorries_to_decls():
    decls, orphans = formalization.scan_lean_source(
        textwrap.dedent(
            """\
            -- sorry in a comment doesn't count
            def withSorry : Nat := by
              sorry

            theorem twoSorries : True ∧ True :=
              ⟨by sorry, by sorry⟩
            """
        )
    )
    assert orphans == 0
    counts = {d.name: d.sorry_count for d in decls}
    assert counts == {"withSorry": 1, "twoSorries": 2}
