"""Offline tests for the referee's TEETH (plan §2 "referee with teeth"):
ledger-fed inputs, structured fix-task emission, self-accountability, and
fingerprint-based cross-chapter dedup wired through referee.py.

Binding behaviors under test:

* the structured digest reads the ledger (targets/status, prior referee
  tasks) and the latest audit snapshot (deception census, tier dist, dedup
  groups) and summarizes COUNTS — it does not dump the census;
* `--emit-tasks` persists referee-origin ledger rows; the prose-only
  default persists nothing (nothing changes without the flag);
* dedup fix-tasks are generated DIRECTLY from fingerprints even when the
  (monkeypatched) Claude misses them entirely;
* self-accountability marks a now-resolved duplicate's task DONE and
  ESCALATES a still-unresolved one (bumps overdue + severity) — the
  coordinateCoframe-survives-twelve-iterations fix;
* `marathon referee tasks` lists referee-origin tasks with overdue counts;
* the v4 ledger migration is additive (v3 db upgrades in place; future
  version refused).

Claude is monkeypatched (no network), the audit snapshot is written to disk
directly (no Lean), and git is stubbed. Fully offline.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import marathon.referee as referee
from marathon.audit.engine import save_snapshot
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.ledger import Ledger, RefereeTask


# --- builders ---------------------------------------------------------------


def mk_decl(name, *, kind="def", module="Ch1", status="ok",
            type_pp="MyType", value_pp="myvalue", cone=(), tags=()) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module=module, status=status,
        type_pp=type_pp, value_pp=value_pp, cone=list(cone),
        axioms=[], has_sorry=False, tags=list(tags), reason=None,
    )


def mk_snapshot(repo, decls) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir=str(repo), modules=["Ch1", "Ch2"], toolchain="lean4:v1",
        lean_version="v1", package_revs={}, trusted_prefixes=["Mathlib"],
        created_at="2026-06-14T00:00:00+00:00", decls=list(decls), failures=[],
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    """A consumer-repo dir with a .git marker so referee_command's guard
    passes; no real git operations are exercised (commit is stubbed)."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def install_claude(monkeypatch, response: str, *, capture=None):
    """Patch referee.run_claude to return ``response`` as stdout."""
    def fake_run(prompt, *, model=None, timeout=None, extra_args=()):
        if capture is not None:
            capture.append(prompt)
        return subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=response, stderr=""
        )
    monkeypatch.setattr(referee, "run_claude", fake_run)
    # claude must look present on PATH for _invoke_claude_referee.
    monkeypatch.setattr(referee.shutil, "which", lambda _n: "/usr/bin/claude")


def stub_git(monkeypatch):
    """Stub the commit path so update_referee never shells out to git."""
    monkeypatch.setattr(referee, "_commit_referee", lambda *a, **k: None)


# --- dedup tasks generated even when Claude misses them ----------------------


def test_emit_generates_dedup_task_even_when_claude_misses_it(repo, monkeypatch):
    # Two equal-fingerprint defs across two chapters.
    snap = mk_snapshot(repo, [
        mk_decl("Ch11.IsPositivelyOriented", module="Ch11"),
        mk_decl("Ch12.IsPositivelyOriented", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    # Claude emits a prose tail and an EMPTY task list — it missed the dup.
    install_claude(monkeypatch, "## tail\n\n- nothing\n\n```json\n"
                   '{"tasks": []}\n```')
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    assert result.ok
    assert result.task_emission is not None
    assert result.task_emission.dedup_tasks == 1
    assert result.task_emission.claude_tasks == 0

    tasks = Ledger.for_repo(repo).all_referee_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t.kind == "dedup"
    assert t.origin == "referee"
    assert t.blocks_target == "Ch11.IsPositivelyOriented"
    assert set(t.target_decls) == {
        "Ch11.IsPositivelyOriented", "Ch12.IsPositivelyOriented"
    }


def test_two_def_dups_sharing_type_but_not_value_both_persist(repo, monkeypatch):
    # Regression (mandatory fix): two SEPARATE cross-chapter def-duplicate
    # groups that share an elaborated TYPE but differ in BODY (the
    # IsPositivelyOriented wrapper-class case) must each become their own
    # task. Before the fix the def-group key folded in only the type
    # fingerprint, so both groups produced the SAME dedup_key and the second
    # upsert silently overwrote the first — only one of two genuine
    # duplicates got a task, and the reported counter (2) disagreed with the
    # 1 row actually persisted.
    snap = mk_snapshot(repo, [
        mk_decl("Ch1.a", module="Ch1", type_pp="Nat", value_pp="2"),
        mk_decl("Ch2.a", module="Ch2", type_pp="Nat", value_pp="2"),
        mk_decl("Ch3.b", module="Ch3", type_pp="Nat", value_pp="5"),
        mk_decl("Ch4.b", module="Ch4", type_pp="Nat", value_pp="5"),
    ])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n- nothing\n\n```json\n"
                   '{"tasks": []}\n```')
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    # Counter and persisted rows must AGREE — two distinct duplicates.
    assert result.task_emission.dedup_tasks == 2
    tasks = Ledger.for_repo(repo).all_referee_tasks()
    dedup = sorted(
        (t for t in tasks if t.kind == "dedup"),
        key=lambda t: t.blocks_target,
    )
    assert len(dedup) == 2
    # Both groups survive — distinct keys, distinct rows, distinct targets.
    assert {t.blocks_target for t in dedup} == {"Ch1.a", "Ch3.b"}
    keys = {t.dedup_key for t in dedup}
    assert len(keys) == 2  # the two keys are genuinely distinct
    assert {tuple(sorted(t.target_decls)) for t in dedup} == {
        ("Ch1.a", "Ch2.a"), ("Ch3.b", "Ch4.b"),
    }


def test_prose_only_default_persists_no_tasks(repo, monkeypatch):
    snap = mk_snapshot(repo, [
        mk_decl("Ch11.IsPositivelyOriented", module="Ch11"),
        mk_decl("Ch12.IsPositivelyOriented", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n- prose only\n")
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=False,  # the default
    )
    assert result.ok
    assert result.task_emission is None
    # The ledger may exist (gather not run) but holds no referee tasks.
    assert Ledger.for_repo(repo).all_referee_tasks() == []


def test_claude_proposed_task_persisted(repo, monkeypatch):
    snap = mk_snapshot(repo, [
        mk_decl("Ch3.sneaky", tags=["placeholder_type"]),
    ])
    save_snapshot(snap, repo)
    claude = (
        "## tail\n\n- watch Ch3.sneaky\n\n```json\n"
        + json.dumps({"tasks": [{
            "title": "Ch3.sneaky uses a placeholder type",
            "kind": "deception",
            "target_decls": ["Ch3.sneaky"],
            "severity": "critical",
            "rationale": "type lies about the statement",
        }]})
        + "\n```"
    )
    install_claude(monkeypatch, claude)
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    assert result.task_emission.claude_tasks == 1
    tasks = [t for t in Ledger.for_repo(repo).all_referee_tasks()
             if t.kind == "deception"]
    assert len(tasks) == 1
    assert tasks[0].severity == "critical"
    assert tasks[0].target_decls == ["Ch3.sneaky"]


def test_prose_tail_strips_task_json(repo, monkeypatch):
    snap = mk_snapshot(repo, [mk_decl("Ch1.foo")])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n- item one\n\n```json\n"
                   '{"tasks": []}\n```')
    stub_git(monkeypatch)
    ref_path = repo / ".marathon" / "standing-items.md"
    ref_path.parent.mkdir(parents=True, exist_ok=True)

    referee.update_referee(
        repo_dir=repo, referee_path=ref_path,
        auto_commit=False, emit_tasks=True,
    )
    written = ref_path.read_text()
    assert "item one" in written
    assert "```json" not in written  # the JSON block never lands in the file


# --- self-accountability: resolve gone dup, escalate surviving one ----------


def test_self_accountability_resolves_gone_dup_and_escalates_survivor(
    repo, monkeypatch
):
    ledger = Ledger.for_repo(repo)
    # Prior pass filed two dedup tasks. One duplicate is now gone; one
    # survives. Both pre-exist as OPEN tasks (the coordinateCoframe case:
    # an item that keeps surviving must get LOUDER).
    # Surviving dup: Ch11/Ch12 IsPositivelyOriented (still in snapshot).
    surviving_key = "dedup:def:" + referee_fp("MyType", "myvalue")
    # Gone dup: a key that no longer matches any live group.
    gone_key = "dedup:def:deadbeef-no-longer-present"
    ledger.upsert_referee_task(RefereeTask(
        dedup_key=surviving_key, kind="dedup", title="survivor",
        target_decls=["Ch11.IsPositivelyOriented", "Ch12.IsPositivelyOriented"],
        severity="high", passes_overdue=1,  # already overdue once
    ))
    ledger.upsert_referee_task(RefereeTask(
        dedup_key=gone_key, kind="dedup", title="resolved-me",
        target_decls=["Old.removed"], severity="high",
    ))

    snap = mk_snapshot(repo, [
        mk_decl("Ch11.IsPositivelyOriented", module="Ch11"),
        mk_decl("Ch12.IsPositivelyOriented", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n```json\n{\"tasks\": []}\n```")
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    em = result.task_emission
    assert em.resolved == 1  # the gone dup
    assert em.escalated == 1  # the surviving one

    survivor = ledger.referee_task(surviving_key)
    assert survivor.status == "open"
    assert survivor.passes_overdue == 2  # bumped from 1
    # overdue >= _ESCALATE_AFTER_PASSES so severity climbed a rung.
    assert survivor.severity == "critical"

    gone = ledger.referee_task(gone_key)
    assert gone.status == "done"
    assert gone.resolved_at is not None


def test_self_accountability_resolves_cleared_deception(repo, monkeypatch):
    ledger = Ledger.for_repo(repo)
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="deception:Ch3.sneaky", kind="deception",
        title="placeholder", target_decls=["Ch3.sneaky"], severity="high",
    ))
    # Snapshot now shows Ch3.sneaky WITHOUT any deception tag — defect gone.
    snap = mk_snapshot(repo, [mk_decl("Ch3.sneaky", tags=[])])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n```json\n{\"tasks\": []}\n```")
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    assert result.task_emission.resolved == 1
    assert ledger.referee_task("deception:Ch3.sneaky").status == "done"


def test_deception_task_with_tag_still_present_escalates(repo, monkeypatch):
    ledger = Ledger.for_repo(repo)
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="deception:Ch3.sneaky", kind="deception",
        title="placeholder", target_decls=["Ch3.sneaky"], severity="high",
        passes_overdue=0,
    ))
    snap = mk_snapshot(repo, [mk_decl("Ch3.sneaky", tags=["placeholder_type"])])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n```json\n{\"tasks\": []}\n```")
    stub_git(monkeypatch)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    assert result.task_emission.escalated == 1
    t = ledger.referee_task("deception:Ch3.sneaky")
    assert t.status == "open"
    assert t.passes_overdue == 1


# --- digest is counts-first / ledger-fed ------------------------------------


def test_digest_reads_ledger_and_snapshot_counts(repo, monkeypatch):
    ledger = Ledger.for_repo(repo)
    from marathon.ledger import Target
    ledger.upsert_target(Target(name="T.blocked", kind="def", status="planned"))
    ledger.set_target_status("T.blocked", "blocked")
    snap = mk_snapshot(repo, [
        mk_decl("Ch11.dup", module="Ch11"),
        mk_decl("Ch12.dup", module="Ch12"),
        mk_decl("Ch3.bad", tags=["axiom_smuggle"]),
    ])
    save_snapshot(snap, repo)

    digest = referee.gather_referee_inputs(repo)
    assert digest.target_status_counts.get("blocked") == 1
    assert "T.blocked" in digest.blocked_targets
    assert len(digest.duplicate_groups) == 1
    assert "axiom_smuggle" in digest.deception_census
    # Markdown is a compact counts block, not a census dump.
    assert "Ledger + audit digest" in digest.markdown
    assert "MECHANICAL" in digest.markdown


def test_digest_caps_offender_lists(repo):
    # Many deception-tagged decls — the census per tag is capped at top_n.
    decls = [mk_decl(f"Ch.bad{i}", tags=["t"]) for i in range(50)]
    save_snapshot(mk_snapshot(repo, decls), repo)
    digest = referee.gather_referee_inputs(repo, top_n=5)
    assert len(digest.deception_census["t"]) == 5


def test_gather_degrades_with_no_ledger_or_snapshot(tmp_path):
    # No ledger db, no snapshot — gather must not crash; digest is "first
    # structured pass".
    digest = referee.gather_referee_inputs(tmp_path)
    assert "first structured pass" in digest.markdown


# --- prompt carries the digest + structured-task request --------------------


def test_emit_tasks_prompt_includes_digest_and_request(repo, monkeypatch):
    snap = mk_snapshot(repo, [
        mk_decl("Ch11.dup", module="Ch11"),
        mk_decl("Ch12.dup", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    captured: list[str] = []
    install_claude(monkeypatch, "## tail\n\n```json\n{\"tasks\": []}\n```",
                   capture=captured)
    stub_git(monkeypatch)

    referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    prompt = captured[0]
    assert "Ledger + audit digest" in prompt
    # The injected request section (distinct from the system-prompt's own
    # mention of the contract).
    assert "After the prose tail, emit the structured fix-task JSON block" in prompt


def test_no_emit_prompt_has_no_task_request(repo, monkeypatch):
    save_snapshot(mk_snapshot(repo, [mk_decl("Ch1.foo")]), repo)
    captured: list[str] = []
    install_claude(monkeypatch, "## tail\n\n- item\n", capture=captured)
    stub_git(monkeypatch)
    referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=False,
    )
    assert "After the prose tail, emit the structured fix-task JSON block" \
        not in captured[0]
    assert "Ledger + audit digest" not in captured[0]


def test_emit_claude_call_carries_digest_but_no_tool_access(repo, monkeypatch):
    """Binding (plan §2): the referee --emit-tasks Claude call carries the
    STRUCTURED ledger digest in its (stdin) prompt but grants NO tool
    access. Exercised against the REAL run_claude command construction (not
    the install_claude stub) so the ``--tools ""`` guarantee is asserted
    where it actually lives — the referee derives the digest, Claude only
    ranks/annotates it; it never gets to touch the repo/ledger itself."""
    import marathon.claude_proc as claude_proc

    snap = mk_snapshot(repo, [
        mk_decl("Ch11.IsPositivelyOriented", module="Ch11"),
        mk_decl("Ch12.IsPositivelyOriented", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    stub_git(monkeypatch)

    seen = {}
    real_run = subprocess.run

    def fake_subprocess_run(cmd, **kwargs):
        # Only intercept the claude exec; let the referee's own git reads
        # (e.g. `git ls-files` in _read_repo_lean) run for real.
        if cmd and str(cmd[0]).endswith("claude"):
            seen["cmd"] = list(cmd)
            seen["input"] = kwargs.get("input")
            seen["env"] = kwargs.get("env") or {}
            return subprocess.CompletedProcess(
                cmd, 0, stdout="## tail\n\n```json\n{\"tasks\": []}\n```",
                stderr="",
            )
        return real_run(cmd, **kwargs)

    # Drive the genuine run_claude (no slot dir contention in tests).
    monkeypatch.setenv("MARATHON_CLAUDE_SLOT_DIR", str(repo / "slots"))
    monkeypatch.setattr(claude_proc.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr(claude_proc.subprocess, "run", fake_subprocess_run)

    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    assert result.ok

    cmd = seen["cmd"]
    # No tool access: the shared run_claude flag set pins --tools "" (empty
    # grant) and never names a tool. This is the whole "Claude is not the
    # scheduler / cannot touch the repo" guarantee for the referee.
    assert "--tools" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    # The digest rides in the STDIN prompt (never argv), and the structured
    # fix-task request is present — the call IS the emit-tasks call.
    assert "Ledger + audit digest" in (seen["input"] or "")
    assert "MECHANICAL" in (seen["input"] or "")
    # Pay-per-token API key is scrubbed (flat-rate Max session, not API).
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    # The mechanical dedup task was still persisted — Claude annotated, the
    # referee's Python derived and wrote the teeth.
    assert result.task_emission.dedup_tasks == 1


# --- idempotent re-emit: dedup tasks don't accrete --------------------------


def test_reemit_does_not_duplicate_dedup_tasks(repo, monkeypatch):
    snap = mk_snapshot(repo, [
        mk_decl("Ch11.dup", module="Ch11"),
        mk_decl("Ch12.dup", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n```json\n{\"tasks\": []}\n```")
    stub_git(monkeypatch)
    ref = repo / ".marathon" / "standing-items.md"

    referee.update_referee(repo_dir=repo, referee_path=ref,
                           auto_commit=False, emit_tasks=True)
    referee.update_referee(repo_dir=repo, referee_path=ref,
                           auto_commit=False, emit_tasks=True)
    tasks = Ledger.for_repo(repo).all_referee_tasks()
    # Still ONE dedup task — the second pass escalated, not re-filed.
    dedup = [t for t in tasks if t.kind == "dedup"]
    assert len(dedup) == 1


def test_claude_dedup_task_shadowing_mechanical_is_dropped(repo, monkeypatch):
    snap = mk_snapshot(repo, [
        mk_decl("Ch11.dup", module="Ch11"),
        mk_decl("Ch12.dup", module="Ch12"),
    ])
    save_snapshot(snap, repo)
    claude = (
        "## tail\n\n```json\n"
        + json.dumps({"tasks": [{
            "title": "dup again", "kind": "dedup",
            "target_decls": ["Ch11.dup", "Ch12.dup"], "severity": "high",
        }]})
        + "\n```"
    )
    install_claude(monkeypatch, claude)
    stub_git(monkeypatch)
    referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    tasks = Ledger.for_repo(repo).all_referee_tasks()
    assert len([t for t in tasks if t.kind == "dedup"]) == 1


# --- malformed Claude output never crashes ----------------------------------


def test_malformed_task_json_is_recorded_not_raised(repo, monkeypatch):
    snap = mk_snapshot(repo, [mk_decl("Ch1.foo")])
    save_snapshot(snap, repo)
    install_claude(monkeypatch, "## tail\n\n```json\n{not valid json\n```")
    stub_git(monkeypatch)
    result = referee.update_referee(
        repo_dir=repo, referee_path=repo / ".marathon" / "standing-items.md",
        auto_commit=False, emit_tasks=True,
    )
    assert result.ok
    assert result.task_emission.parse_error is not None


# --- v4 ledger migration is additive ----------------------------------------


def test_v3_db_upgrades_to_v4_additively(tmp_path):
    import sqlite3
    from marathon.ledger import LedgerError, SCHEMA_VERSION

    dbp = tmp_path / ".marathon" / "marathon.db"
    dbp.parent.mkdir(parents=True)
    c = sqlite3.connect(dbp)
    c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    c.execute(
        "CREATE TABLE targets (id INTEGER PRIMARY KEY, name TEXT UNIQUE, "
        "kind TEXT, source_ref TEXT, lean_file TEXT, lean_decl TEXT, "
        "gate_policy TEXT, status TEXT, created_at TEXT, notes TEXT)"
    )
    c.execute(
        "INSERT INTO targets (name, kind, gate_policy, status, created_at) "
        "VALUES ('T.foo', 'def', 'auto', 'planned', 'now')"
    )
    c.commit()
    c.close()

    ledger = Ledger.for_repo(tmp_path)
    st = ledger.status()
    assert st["schema_version"] == SCHEMA_VERSION == 4
    assert st["tables"]["targets"] == 1  # v3 row preserved
    assert st["tables"]["referee_tasks"] == 0  # new table empty

    # Future-version guard still fires.
    c = sqlite3.connect(dbp)
    c.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    c.commit()
    c.close()
    with pytest.raises(LedgerError):
        Ledger.for_repo(tmp_path).status()


def test_upsert_referee_task_keeps_higher_severity(repo):
    ledger = Ledger.for_repo(repo)
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="k", kind="naming", title="v1", target_decls=[],
        severity="high",
    ))
    # Re-emit at LOWER severity must not de-escalate.
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="k", kind="naming", title="v2", target_decls=[],
        severity="low",
    ))
    t = ledger.referee_task("k")
    assert t.severity == "high"
    assert t.title == "v2"  # annotation columns still update


# --- CLI: `marathon referee tasks` listing ----------------------------------


def test_referee_tasks_command_lists(repo, capsys):
    ledger = Ledger.for_repo(repo)
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="dedup:def:abc", kind="dedup", title="dup A",
        target_decls=["Ch11.x", "Ch12.x"], severity="high",
        blocks_target="Ch11.x",
    ))
    ledger.escalate_referee_task("dedup:def:abc")  # one pass overdue

    import argparse
    args = argparse.Namespace(
        referee_command="tasks", repo_dir=repo, open_only=False,
    )
    referee.referee_command(args)
    out = capsys.readouterr().out
    assert "dup A" in out
    assert "overdue" in out
    assert "blocks Ch11.x" in out


def test_referee_tasks_command_open_only(repo, capsys):
    ledger = Ledger.for_repo(repo)
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="k1", kind="naming", title="open one", target_decls=[],
    ))
    ledger.upsert_referee_task(RefereeTask(
        dedup_key="k2", kind="naming", title="done one", target_decls=[],
    ))
    ledger.resolve_referee_task("k2")

    import argparse
    args = argparse.Namespace(
        referee_command="tasks", repo_dir=repo, open_only=True,
    )
    referee.referee_command(args)
    out = capsys.readouterr().out
    assert "open one" in out
    assert "done one" not in out


# --- helper: reproduce the dedup key the module would compute ---------------


def referee_fp(type_pp, value_pp):
    """Reproduce the SUFFIX of a def-group dedup key (everything after the
    ``dedup:def:`` prefix). A def group's identity is BOTH fingerprints —
    the type fingerprint then a hash of the value fingerprint — so two def
    duplicates sharing a type but differing in body get DISTINCT keys (the
    collision the mandatory fix closes). Mirrors referee._dedup_task_key."""
    import hashlib

    from marathon.audit.records import fingerprint
    type_fp = fingerprint(type_pp)
    value_fp = fingerprint(value_pp) if value_pp is not None else ""
    value_hash = hashlib.sha256(value_fp.encode("utf-8")).hexdigest()[:16]
    return f"{type_fp}:{value_hash}"
