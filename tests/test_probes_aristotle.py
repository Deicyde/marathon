"""Offline tests for marathon.probes_aristotle — budget-capped Aristotle
vacuity probes (phase-6b, plan §2 ruling 5 + crit-feas §4).

A vacuity probe asks Aristotle to DISPROVE a theorem's hypotheses (prove
``hyps → False``); a SUCCESS means a broken/vacuous spec. Because this is the
expensive probe (it spends real Aristotle budget), the caps/dedup/opt-in are
load-bearing — and that is what these tests pin.

NO real Aristotle: every submission is monkeypatched at the
``aristotle_runtime.submit_vacuity_probe`` seam (the one narrow helper),
which is imported lazily inside ``run_one_probe``. No Lean toolchain, no
network. The staging test uses a throwaway git repo so the ``.gitignore``
filter (which excludes other probe files / caches from the bundle) is real.

Covered:

* goal generation for a multi-hypothesis theorem (statement embedded; valid
  import; single sorry; stable dedup hash);
* dedup skips a repeat goal (persisted index);
* the --max-probes cap is enforced by the governor;
* a SUCCESSFUL disproof (TaskStatus.COMPLETE) writes a finding + is
  high-signal; a FAILURE is inconclusive, writes NO finding, and never
  signals a tier change;
* opt-in only — nothing submits without the explicit verb (importing the
  module / planning spends nothing);
* the staged probe file is added to a repo COPY and excluded from other
  bundles (gitignored caches/probes never staged).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from marathon import aristotle_runtime, probes_aristotle
from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.probes_aristotle import (
    DEFAULT_MAX_PROBES,
    PROBE_DIR_IN_STAGE,
    build_vacuity_goal,
    interpret_outcome,
    load_submitted_index,
    run_one_probe,
    run_probes,
    select_targets,
    stage_probe_bundle,
    vacuity_state_dir,
    write_finding,
)


# --- builders (same style as test_kernel / test_trust) -----------------------


def mk_decl(
    name="Foo.bar",
    kind="theorem",
    module="Foo",
    status="ok",
    type_pp="∀ (n : Nat) (h : n < 0), n = n",
    value_pp=None,
    cone=(),
    axioms=(),
    has_sorry=False,
    tags=(),
    reason=None,
) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status,
        type_pp=type_pp, value_pp=value_pp, cone=list(cone),
        axioms=list(axioms), has_sorry=has_sorry, tags=list(tags),
        reason=reason,
    )


def mk_snapshot(decls, **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir="/r",
        modules=["Foo"],
        toolchain="leanprover/lean4:v4.28.0",
        lean_version="4.28.0",
        package_revs={},
        trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-13T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )
    defaults.update(kw)
    return AuditSnapshot(**defaults)


class _FakeTask:
    def __init__(self, status_value: str):
        self.status = SimpleNamespace(value=status_value)
        self.agent_task_id = "task-xyz"


class _FakeProject:
    def __init__(self):
        self.project_id = "proj-abc"


def _patch_submit(monkeypatch, status_value: str, recorder: list | None = None):
    """Replace the real Aristotle submit helper with a fake that returns a
    terminal status of our choosing — NO budget spent. Records the
    project_dir it was handed so the staging test can assert the bundle."""

    async def fake_submit(project_dir, goal_relpath, *, polling_interval=30,
                          prompt=None):
        if recorder is not None:
            recorder.append(
                {"project_dir": Path(project_dir), "goal_relpath": goal_relpath}
            )
        return _FakeProject(), _FakeTask(status_value)

    monkeypatch.setattr(
        aristotle_runtime, "submit_vacuity_probe", fake_submit
    )


# --- goal generation ---------------------------------------------------------


def test_goal_generation_multi_hypothesis_theorem():
    decl = mk_decl(
        name="Geo.foo",
        module="Geo.Chapter",
        type_pp="∀ (M : Manifold) (h₁ : IsSmooth M) (h₂ : Compact M), P M",
    )
    goal = build_vacuity_goal(decl)
    src = goal.lean_source
    # The exact pinned-pp statement is embedded (the prover's ground truth).
    assert "IsSmooth M" in src and "Compact M" in src
    # A valid import of the target's own module (every name resolves).
    assert "import Geo.Chapter" in src
    # Exactly one sorry to discharge, and a False conclusion (the vacuity ask).
    assert src.count("sorry") == 1
    assert "False" in src
    # Names the probe file under the dedicated scratch dir, never the repo.
    assert goal.relpath.startswith(PROBE_DIR_IN_STAGE + "/")
    assert goal.filename.endswith(".lean")
    # Hash is stable for the same decl+statement (dedup key).
    assert build_vacuity_goal(decl).goal_hash == goal.goal_hash


def test_goal_hash_changes_when_statement_changes():
    a = build_vacuity_goal(mk_decl(type_pp="∀ (n : Nat), n < 0"))
    b = build_vacuity_goal(mk_decl(type_pp="∀ (n : Nat), n < 1"))
    assert a.goal_hash != b.goal_hash


# --- governor: probeability, cap, dedup --------------------------------------


def test_select_skips_non_theorem_decls(tmp_path):
    snap = mk_snapshot([
        mk_decl(name="Foo.thm", kind="theorem"),
        mk_decl(name="Foo.aDef", kind="def", value_pp="fun x => x"),
        mk_decl(name="Foo.aStruct", kind="structure"),
    ])
    plan = select_targets(
        snap, ["Foo.thm", "Foo.aDef", "Foo.aStruct"], tmp_path
    )
    assert [g.decl_name for g in plan.to_run] == ["Foo.thm"]
    assert set(plan.skipped_unprobeable) == {"Foo.aDef", "Foo.aStruct"}


def test_select_skips_unknown_decls(tmp_path):
    snap = mk_snapshot([
        mk_decl(name="Foo.ok", kind="theorem"),
        mk_decl(name="Foo.bad", kind="theorem", status="unknown",
                type_pp=None, has_sorry=None),
    ])
    plan = select_targets(snap, ["Foo.ok", "Foo.bad"], tmp_path)
    assert [g.decl_name for g in plan.to_run] == ["Foo.ok"]
    assert plan.skipped_unprobeable == ["Foo.bad"]


def test_max_probes_cap_enforced(tmp_path):
    names = [f"Foo.t{i}" for i in range(5)]
    snap = mk_snapshot([mk_decl(name=n) for n in names])
    plan = select_targets(snap, names, tmp_path, max_probes=2)
    assert len(plan.to_run) == 2
    assert plan.skipped_cap == ["Foo.t2", "Foo.t3", "Foo.t4"]


def test_default_cap_is_three():
    assert DEFAULT_MAX_PROBES == 3


def test_dedup_skips_a_repeat_goal(tmp_path):
    snap = mk_snapshot([mk_decl(name="Foo.t")])
    # First plan: would run.
    first = select_targets(snap, ["Foo.t"], tmp_path)
    assert [g.decl_name for g in first.to_run] == ["Foo.t"]
    # Record it as submitted (what a real run does after a terminal status).
    goal = first.to_run[0]
    outcome = interpret_outcome(goal, "FAILED")
    probes_aristotle.record_submitted(tmp_path, outcome)
    # Second plan over the SAME decl now dedups (persisted index).
    second = select_targets(snap, ["Foo.t"], tmp_path)
    assert second.to_run == []
    assert second.skipped_dedup == ["Foo.t"]


def test_dedup_within_one_invocation(tmp_path):
    """A decl repeated in one invocation (or two selectors resolving to the
    same decl) must dedup to a SINGLE goal — never double-spend the budget.
    The persisted index is empty here, so this exercises in-invocation dedup,
    not cross-run."""
    snap = mk_snapshot([mk_decl(name="Foo.t")])
    plan = select_targets(snap, ["Foo.t", "Foo.t", "Foo.t"], tmp_path,
                          max_probes=5)
    assert [g.decl_name for g in plan.to_run] == ["Foo.t"]
    assert plan.skipped_dedup == ["Foo.t", "Foo.t"]


# --- evidence semantics: asymmetry -------------------------------------------


def test_complete_status_is_broken_spec_high_signal():
    goal = build_vacuity_goal(mk_decl(name="Foo.vac"))
    outcome = interpret_outcome(goal, "COMPLETE")
    assert outcome.broken_spec is True
    assert outcome.inconclusive is False
    assert "BROKEN SPEC" in outcome.summary


@pytest.mark.parametrize(
    "status", ["COMPLETE_WITH_ERRORS", "FAILED", "CANCELED", "OUT_OF_BUDGET",
               "UNKNOWN"],
)
def test_non_complete_status_is_inconclusive_weak(status):
    goal = build_vacuity_goal(mk_decl(name="Foo.ok"))
    outcome = interpret_outcome(goal, status)
    assert outcome.broken_spec is False
    assert outcome.inconclusive is True
    assert "inconclusive" in outcome.summary
    # The asymmetry: a failure-to-disprove must say it changes no tier.
    assert "no tier change" in outcome.summary


def test_write_finding_only_for_disproof(tmp_path):
    goal = build_vacuity_goal(mk_decl(name="Foo.vac"))
    broken = interpret_outcome(goal, "COMPLETE")
    path = write_finding(tmp_path, broken)
    assert path.is_file()
    payload = json.loads(path.read_text())
    assert payload["finding_kind"] == "vacuous_spec"
    # This phase NEVER auto-rejects — only writes the finding + prints.
    assert payload["auto_rejected"] is False
    # Writing a finding for an inconclusive outcome is forbidden (asymmetry).
    inconclusive = interpret_outcome(goal, "FAILED")
    with pytest.raises(ValueError):
        write_finding(tmp_path, inconclusive)


# --- run_one_probe: monkeypatched submission (no real Aristotle) -------------


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "Foo.lean").write_text("theorem foo : True := trivial\n")
    (repo / ".gitignore").write_text(".marathon/\nscratch/\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def test_successful_disproof_writes_finding(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    _patch_submit(monkeypatch, "COMPLETE")
    goal = build_vacuity_goal(mk_decl(name="Foo.vac", module="Foo"))
    outcome = asyncio.run(run_one_probe(repo, goal))
    assert outcome.broken_spec is True
    # The finding file landed under the self-gitignored vacuity dir.
    findings = list((vacuity_state_dir(repo) / "findings").glob("*.json"))
    assert len(findings) == 1
    # And the dedup index recorded the goal (so it won't resubmit).
    assert goal.goal_hash in load_submitted_index(repo)


def test_failed_disproof_is_inconclusive_no_finding(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _patch_submit(monkeypatch, "COMPLETE_WITH_ERRORS")
    goal = build_vacuity_goal(mk_decl(name="Foo.ok", module="Foo"))
    outcome = asyncio.run(run_one_probe(repo, goal))
    assert outcome.broken_spec is False
    assert outcome.inconclusive is True
    # NO finding written for weak negative evidence.
    findings_dir = vacuity_state_dir(repo) / "findings"
    assert not findings_dir.exists() or not list(findings_dir.glob("*.json"))
    # But the goal IS recorded so it won't be resubmitted (dedup persists
    # even an inconclusive run).
    assert goal.goal_hash in load_submitted_index(repo)


def test_run_probes_respects_cap_and_dedup_end_to_end(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _patch_submit(monkeypatch, "FAILED")
    names = [f"Foo.t{i}" for i in range(4)]
    snap = mk_snapshot([mk_decl(name=n, module="Foo") for n in names])
    plan, outcomes = asyncio.run(
        run_probes(repo, snap, names, max_probes=2)
    )
    # Cap honored: only 2 submitted, 2 skipped-over-cap.
    assert len(outcomes) == 2
    assert len(plan.skipped_cap) == 2
    # Re-running now dedups the 2 already-submitted; the cap then lets the
    # next 2 through.
    plan2, outcomes2 = asyncio.run(
        run_probes(repo, snap, names, max_probes=2)
    )
    submitted_names = {o.decl_name for o in outcomes} | {
        o.decl_name for o in outcomes2
    }
    assert submitted_names == set(names)
    assert set(plan2.skipped_dedup) == {o.decl_name for o in outcomes}


# --- opt-in only: planning / importing spends nothing ------------------------


def test_planning_never_submits(tmp_path, monkeypatch):
    """The governor (select_targets) must never reach the Aristotle seam —
    if it did, this booby-trapped submit would raise."""

    async def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("planning must not submit to Aristotle")

    monkeypatch.setattr(aristotle_runtime, "submit_vacuity_probe", boom)
    snap = mk_snapshot([mk_decl(name="Foo.t")])
    plan = select_targets(snap, ["Foo.t"], tmp_path)
    assert [g.decl_name for g in plan.to_run] == ["Foo.t"]  # planned, not run


# --- staging: probe added to a COPY; other probe files excluded --------------


def test_staged_probe_file_isolated_from_repo(tmp_path):
    repo = _git_repo(tmp_path)
    # An OTHER probe file + a gitignored cache that MUST NOT be bundled.
    (repo / "scratch").mkdir()
    (repo / "scratch" / "OtherProbe.lean").write_text("-- other probe\n")
    (repo / ".marathon").mkdir()
    (repo / ".marathon" / "junk.json").write_text("{}\n")

    goal = build_vacuity_goal(mk_decl(name="Foo.vac", module="Foo"))
    dest = tmp_path / "stage"
    probe_path = stage_probe_bundle(repo, goal, dest)

    # The probe landed ONLY in the staged copy, never in the live repo.
    assert probe_path.is_file()
    assert probe_path.parent.name == PROBE_DIR_IN_STAGE
    assert not (repo / PROBE_DIR_IN_STAGE).exists()
    # The repo's tracked file is in the bundle; the gitignored cache and the
    # other (gitignored) probe are NOT (gitignore filter, crit-feas §4).
    assert (dest / "Foo.lean").is_file()
    assert not (dest / ".marathon" / "junk.json").exists()
    assert not (dest / "scratch" / "OtherProbe.lean").exists()
    # The staged probe is the only thing under the probe dir.
    staged_probes = list((dest / PROBE_DIR_IN_STAGE).glob("*.lean"))
    assert staged_probes == [probe_path]
