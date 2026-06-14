"""Offline tests for Phase-7 planner intake (marathon.plan), the ledger
v3 targets/target_deps tables, the order.txt legacy importer, and the
`marathon plan` CLI.

Binding contract under test (docs/marathon-v2-plan.md §3 Phase 7):

* ledger v2 -> v3 migration is additive + idempotent on a REAL v2 db
  (existing rows untouched; the future-version guard still fires for v4+);
* `targets` is MUTABLE (status updates in place) UNLIKE the append-only
  verdict logs; `target_deps` is a wholesale-replaced edge set;
* `plan_from_sorries` scans a synthesized multi-file fixture (right count,
  file:line source_ref, decl name) reusing the fill scanner's regex;
* `plan_from_axiom` makes one target; `plan_from_repo` covers the repo;
* dependency edges derive from a fabricated audit snapshot (a sorry
  depends on its cone's def-targets) and are ABSENT with no snapshot;
* --gate-policy mixed selects milestone-named targets human, rest auto;
* the order.txt legacy import maps chapters to coarse targets while the
  existing parse/use is unchanged;
* --dry-run writes nothing; the CLI smoke-runs end to end.

No subprocesses (beyond `git` for the gitignore filter on a real init'd
repo), no network, no Lean toolchain, no Claude.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from marathon.audit.engine import save_snapshot
from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.ledger import (
    LEDGER_RELPATH,
    SCHEMA_VERSION,
    Ledger,
    LedgerError,
    Target,
)
from marathon.order import import_order_as_targets, parse_order_file
from marathon.plan import (
    Plan,
    find_sorries_in_file,
    plan_from_axiom,
    plan_from_repo,
    plan_from_sorries,
    resolve_gate_policy,
    source_mode,
)


# --- fixtures ----------------------------------------------------------------


def _init_git_repo(repo_dir: Path) -> None:
    """A real git repo so the gitignore filter (`git ls-files`) works."""
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True)


def write_lean_repo(tmp_path: Path) -> Path:
    """A multi-file Lean repo fixture with a known sorry inventory:

    * A.lean: `foo` (sorry), `bar` (no sorry), `baz` (sorry)
    * sub/B.lean: `qux` (sorry)
    * C.lean: no sorries
    * a gitignored build artifact with a sorry (must be skipped)
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "A.lean").write_text(
        "def foo : Nat := sorry\n"
        "theorem bar : True := trivial\n"
        "-- sorry in a comment must NOT count\n"
        "lemma baz : True := by\n"
        "  sorry\n"
    )
    sub = repo / "sub"
    sub.mkdir()
    (sub / "B.lean").write_text("theorem qux : True := by sorry\n")
    (repo / "C.lean").write_text("theorem clean : True := trivial\n")
    # A gitignored artifact carrying a sorry — the gitignore filter must
    # drop it from `plan repo` (build artifacts are never targets).
    (repo / ".gitignore").write_text("build/\n")
    build = repo / "build"
    build.mkdir()
    (build / "Generated.lean").write_text("def gen : Nat := sorry\n")
    _init_git_repo(repo)
    return repo


# --- ledger v2 -> v3 migration -----------------------------------------------


def _make_real_v2_db(db_path: Path) -> None:
    """Create a db stamped schema_version=2 with a real v1/v2 row in it,
    WITHOUT the v3 tables — the genuine pre-migration shape."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta (key, value) VALUES ('schema_version', '2');
        CREATE TABLE issues (
            issue_num INTEGER PRIMARY KEY, chapter INTEGER,
            status TEXT NOT NULL, verdict_ts TEXT NOT NULL, notes TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, last_iteration_ts TEXT
        );
        INSERT INTO issues (issue_num, status, verdict_ts)
            VALUES (14, 'verified', '2026-06-01T00:00:00+00:00');
        """
    )
    conn.commit()
    conn.close()


def test_v2_to_v3_migration_is_additive_and_idempotent(tmp_path):
    db = tmp_path / LEDGER_RELPATH
    _make_real_v2_db(db)
    ledger = Ledger.for_repo(tmp_path)

    # First open upgrades in place to v3.
    ledger.init()
    info = ledger.status()
    assert info["schema_version"] == SCHEMA_VERSION == 3
    assert "targets" in info["tables"] and "target_deps" in info["tables"]

    # The pre-existing v2 row is untouched.
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT status FROM issues WHERE issue_num = 14"
        ).fetchone()
    assert row == ("verified",)

    # Re-opening is a no-op (still v3, no error).
    ledger.init()
    assert ledger.status()["schema_version"] == 3


def test_future_version_guard_still_fires_for_v4(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    ledger.init()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute("UPDATE meta SET value = '4' WHERE key = 'schema_version'")
    with pytest.raises(LedgerError):
        ledger.init()
    with pytest.raises(LedgerError):
        ledger.upsert_target(Target(name="x", kind="sorry"))


# --- targets are MUTABLE (unlike the append-only verdict log) ----------------


def test_target_upsert_is_idempotent_by_name(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    t = Target(name="sorry:M:foo", kind="sorry", source_ref="A.lean:1",
               lean_file="A.lean", lean_decl="foo", gate_policy="auto")
    id1 = ledger.upsert_target(t)
    id2 = ledger.upsert_target(t)
    assert id1 == id2
    assert len(ledger.all_targets()) == 1


def test_target_status_updates_in_place(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    ledger.upsert_target(Target(name="t1", kind="sorry"))
    assert ledger.get_target("t1").status == "planned"
    assert ledger.set_target_status("t1", "in_progress") is True
    assert ledger.get_target("t1").status == "in_progress"
    # A re-plan (upsert) must NOT reset the live status back to planned.
    ledger.upsert_target(Target(name="t1", kind="sorry", notes="re-planned"))
    again = ledger.get_target("t1")
    assert again.status == "in_progress"
    assert again.notes == "re-planned"  # other columns DO refresh


def test_target_bad_status_rejected_by_check(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    ledger.upsert_target(Target(name="t1", kind="sorry"))
    with pytest.raises(sqlite3.IntegrityError):
        ledger.set_target_status("t1", "not-a-status")


def test_target_deps_replace_is_wholesale_and_cascades(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    a = ledger.upsert_target(Target(name="a", kind="sorry"))
    b = ledger.upsert_target(Target(name="b", kind="sorry"))
    c = ledger.upsert_target(Target(name="c", kind="sorry"))
    ledger.replace_target_deps(a, [b, c])
    assert ledger.target_deps(a) == sorted([b, c])
    # Self-edges/dupes dropped silently.
    ledger.replace_target_deps(a, [a, b, b])
    assert ledger.target_deps(a) == [b]
    # All-edges read.
    assert (a, b) in ledger.all_target_deps()
    # FK cascade: deleting target b drops the (a, b) edge.
    with ledger._tx() as conn:
        conn.execute("DELETE FROM targets WHERE id = ?", (b,))
    assert ledger.target_deps(a) == []


# --- sorry scan over a synthesized multi-file fixture ------------------------


def test_find_sorries_in_file_pins_decl_and_line(tmp_path):
    f = tmp_path / "A.lean"
    f.write_text(
        "def foo : Nat := sorry\n"        # line 1: decl + sorry same line
        "theorem bar : True := trivial\n"  # line 2: no sorry
        "lemma baz : True := by\n"          # line 4-decl
        "  sorry\n"                          # line 4: sorry on body line
    )
    hits = find_sorries_in_file(f)
    assert [(h.decl, h.decl_line) for h in hits] == [("foo", 1), ("baz", 3)]


def test_plan_from_sorries_counts_and_source_refs(tmp_path):
    repo = write_lean_repo(tmp_path)
    plan = plan_from_sorries(repo, "A.lean", derive_deps=False)
    decls = sorted(t.lean_decl for t in plan.targets)
    assert decls == ["baz", "foo"]
    by_decl = {t.lean_decl: t for t in plan.targets}
    assert by_decl["foo"].source_ref == "A.lean:1"
    assert by_decl["baz"].source_ref == "A.lean:4"
    assert by_decl["foo"].kind == "sorry"
    assert by_decl["foo"].lean_file == "A.lean"
    # Unique names are module-qualified.
    assert by_decl["foo"].name == "sorry:A:foo"


def test_plan_from_repo_is_gitignore_filtered(tmp_path):
    repo = write_lean_repo(tmp_path)
    plan = plan_from_repo(repo, derive_deps=False)
    decls = sorted(t.lean_decl for t in plan.targets)
    # foo, baz (A.lean), qux (sub/B.lean); the gitignored build/Generated
    # 'gen' must NOT appear; clean (C.lean) has no sorry.
    assert decls == ["baz", "foo", "qux"]
    qux = next(t for t in plan.targets if t.lean_decl == "qux")
    assert qux.source_ref == "sub/B.lean:1"
    assert qux.name == "sorry:sub.B:qux"


# --- axiom single-target -----------------------------------------------------


def test_plan_from_axiom_single_target(tmp_path):
    repo = write_lean_repo(tmp_path)
    plan = plan_from_axiom(repo, "Foo.myAxiom")
    assert len(plan.targets) == 1
    (t,) = plan.targets
    assert t.kind == "axiom"
    assert t.lean_decl == "Foo.myAxiom"
    assert t.source_ref == "Foo.myAxiom"
    assert t.name == "axiom:Foo.myAxiom"
    assert plan.edges == []


# --- dependency edges from a fabricated audit snapshot -----------------------


def _mk_decl(name, kind, cone=(), has_sorry=False, type_pp="Nat", module="M"):
    return DeclAudit(
        name=name, kind=kind, module=module, status="ok",
        type_pp=type_pp, value_pp=None, cone=list(cone), axioms=[],
        has_sorry=has_sorry, tags=[], reason=None,
    )


def _write_snapshot(repo: Path, decls) -> None:
    snap = AuditSnapshot(
        repo_dir=str(repo), modules=["M"],
        toolchain="leanprover/lean4:v4.28.0", lean_version="4.28.0",
        package_revs={}, trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-14T00:00:00+00:00", decls=list(decls),
        failures=[],
    )
    save_snapshot(snap, repo)


def test_dep_edges_from_cone_when_snapshot_exists(tmp_path):
    """A sorry theorem `mainThm` whose statement cone reaches local def
    `helperDef` (itself sorry-bodied) must get a dep edge mainThm ->
    helperDef. A trusted-vocabulary constant (absent from the snapshot)
    contributes no edge."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "M.lean").write_text(
        "def helperDef : Nat := sorry\n"
        "theorem mainThm : helperDef = 0 := by sorry\n"
    )
    _init_git_repo(repo)
    # Snapshot: mainThm's TYPE cone mentions helperDef (a local def).
    _write_snapshot(repo, [
        _mk_decl("helperDef", "def", cone=(), has_sorry=True),
        _mk_decl("mainThm", "theorem", cone=("helperDef",), has_sorry=True),
    ])
    plan = plan_from_repo(repo, derive_deps=True)
    names = {t.lean_decl: t.name for t in plan.targets}
    assert set(names) == {"helperDef", "mainThm"}
    assert (names["mainThm"], names["helperDef"]) in plan.edges
    # Exactly one edge (helperDef has no local-def cone).
    assert len(plan.edges) == 1


def test_dep_edges_with_fully_qualified_snapshot_names(tmp_path):
    """REGRESSION: a real audit snapshot stores FULLY-QUALIFIED decl names
    (`Proj.M.mainThm`) while the source-line scanner can only see the bare
    name (`mainThm`). The dep-edge derivation must resolve bare -> qualified
    (module-disambiguated) so edges derive on namespaced repos, not only on
    bare-named test fixtures."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "M.lean").write_text(
        "def helperDef : Nat := sorry\n"
        "theorem mainThm : helperDef = 0 := by sorry\n"
    )
    _init_git_repo(repo)
    _write_snapshot(repo, [
        _mk_decl("Proj.M.helperDef", "def", cone=(), has_sorry=True,
                 module="Proj.M"),
        _mk_decl("Proj.M.mainThm", "theorem", cone=("Proj.M.helperDef",),
                 has_sorry=True, module="Proj.M"),
    ])
    plan = plan_from_repo(repo, derive_deps=True)
    names = {t.lean_decl: t.name for t in plan.targets}
    assert set(names) == {"helperDef", "mainThm"}
    assert (names["mainThm"], names["helperDef"]) in plan.edges
    assert len(plan.edges) == 1


def test_no_dep_edges_without_snapshot(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "M.lean").write_text(
        "def helperDef : Nat := sorry\n"
        "theorem mainThm : helperDef = 0 := by sorry\n"
    )
    _init_git_repo(repo)
    plan = plan_from_repo(repo, derive_deps=True)  # no snapshot on disk
    assert plan.edges == []


# --- gate_policy resolution --------------------------------------------------


def test_gate_policy_uniform_modes():
    assert resolve_gate_policy(gate_mode="auto", decl="anything") == "auto"
    assert resolve_gate_policy(gate_mode="human", decl="anything") == "human"


def test_gate_policy_mixed_milestone_selection():
    # milestone-named (default keywords include 'stokes'/'theorem'/'main')
    assert resolve_gate_policy(
        gate_mode="mixed", decl="StokesTheorem.main") == "human"
    assert resolve_gate_policy(
        gate_mode="mixed", decl="helperLemma_aux") == "auto"
    # source_ref also matched.
    assert resolve_gate_policy(
        gate_mode="mixed", decl="x", source_ref="Milestone/Foo.lean:3"
    ) == "human"
    # custom keyword set overrides.
    assert resolve_gate_policy(
        gate_mode="mixed", decl="frobenius", milestone_keywords=("frob",)
    ) == "human"


def test_plan_mixed_marks_milestone_human(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "M.lean").write_text(
        "theorem mainTheorem : True := by sorry\n"
        "lemma scaffold_aux : True := by sorry\n"
    )
    _init_git_repo(repo)
    plan = plan_from_repo(repo, gate_mode="mixed", derive_deps=False)
    by_decl = {t.lean_decl: t.gate_policy for t in plan.targets}
    assert by_decl["mainTheorem"] == "human"
    assert by_decl["scaffold_aux"] == "auto"


# --- order.txt legacy import (existing parse/use UNCHANGED) -------------------


def test_import_order_as_targets_maps_chapters(tmp_path):
    order = tmp_path / "order.txt"
    order.write_text(
        "chap14.tex -> Chapter14\n"
        "    - Prove Lemma 14.7.\n"
        "chap15.tex -> Chapter15\n"
    )
    targets = import_order_as_targets(order)
    assert [t.name for t in targets] == ["order:Chapter14", "order:Chapter15"]
    assert all(t.kind == "statement" for t in targets)
    assert targets[0].source_ref == "chap14.tex"
    assert targets[0].lean_file == "Chapter14"
    assert targets[0].notes == "- Prove Lemma 14.7."
    assert targets[1].notes is None

    # The existing parser is untouched — parse_order_file still works.
    entries = parse_order_file(order)
    assert [e.output_folder for e in entries] == ["Chapter14", "Chapter15"]


def test_import_order_targets_commit_to_ledger(tmp_path):
    order = tmp_path / "order.txt"
    order.write_text("chap14.tex -> Chapter14\n")
    ledger = Ledger.for_repo(tmp_path)
    Plan.from_targets(import_order_as_targets(order)).commit(ledger)
    rows = ledger.all_targets()
    assert len(rows) == 1 and rows[0].kind == "statement"


# --- source_mode firewall config (per-project, safe default) -----------------


def test_source_mode_defaults_to_copyrighted(tmp_path):
    # No config file -> the SAFE default.
    assert source_mode(tmp_path) == "copyrighted"


def test_source_mode_reads_open_from_config(tmp_path):
    cfg = tmp_path / ".marathon" / "review" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('source_mode = "open"\n')
    assert source_mode(tmp_path) == "open"
    # An unknown value falls back to the safe default.
    cfg.write_text('source_mode = "nonsense"\n')
    assert source_mode(tmp_path) == "copyrighted"


# --- commit / dry-run --------------------------------------------------------


def test_plan_commit_writes_targets_and_edges(tmp_path):
    repo = write_lean_repo(tmp_path)
    _write_snapshot(repo, [
        _mk_decl("foo", "def", has_sorry=True),
    ])
    plan = plan_from_sorries(repo, "A.lean", derive_deps=False)
    ledger = Ledger.for_repo(repo)
    written = plan.commit(ledger)
    assert written["targets"] == 2
    assert len(ledger.all_targets()) == 2


def test_dry_run_writes_nothing(tmp_path, capsys):
    """The CLI --dry-run path must not create any target rows."""
    import marathon.__main__ as m

    repo = write_lean_repo(tmp_path)
    plan = plan_from_repo(repo, derive_deps=False)
    m._emit_plan(plan, repo, dry_run=True)
    out = capsys.readouterr().out
    assert "dry run" in out
    # Nothing was written — the ledger has no target rows (db may not even
    # exist; if it does, it's empty of targets).
    ledger = Ledger.for_repo(repo)
    if ledger.db_path.is_file():
        assert ledger.all_targets() == []


# --- CLI smoke ---------------------------------------------------------------


def _run_cli(argv):
    import marathon.__main__ as m

    parser = m._build_parser()
    args = parser.parse_args(argv)
    args.func(args)


def test_cli_plan_axiom_smoke(tmp_path, capsys):
    repo = write_lean_repo(tmp_path)
    _run_cli(["plan", "axiom", "Foo.bar", "--repo-dir", str(repo)])
    out = capsys.readouterr().out
    assert "axiom:Foo.bar" in out
    assert "wrote 1 target" in out
    assert Ledger.for_repo(repo).get_target("axiom:Foo.bar") is not None


def test_cli_plan_repo_dry_run_writes_nothing(tmp_path, capsys):
    repo = write_lean_repo(tmp_path)
    _run_cli(["plan", "repo", "--repo-dir", str(repo), "--dry-run"])
    out = capsys.readouterr().out
    assert "dry run" in out
    ledger = Ledger.for_repo(repo)
    if ledger.db_path.is_file():
        assert ledger.all_targets() == []


def test_cli_plan_sorries_mixed_gate(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "M.lean").write_text(
        "theorem mainTheorem : True := by sorry\n"
        "lemma helper_aux : True := by sorry\n"
    )
    _init_git_repo(repo)
    _run_cli(["plan", "sorries", "--repo-dir", str(repo),
              "--gate-policy", "mixed"])
    out = capsys.readouterr().out
    assert "human=1" in out and "auto=1" in out
