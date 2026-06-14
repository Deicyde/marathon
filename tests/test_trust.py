"""Offline tests for the Phase-5b trust core (marathon.audit.trust +
the ledger's v2 ``decl_verdicts`` event log).

Binding rulings under test (docs/marathon-v2-plan.md §2 ruling 4;
crit-feas-verification-surface-first §3):

* tier is COMPUTED on read from (current snapshot + verdict events) —
  every rung of the ladder is exercised both ways;
* verdict events are append-only (schema triggers abort UPDATE/DELETE;
  re-pin/revoke append NEW events, history preserved);
* toolchain bumps neither silently invalidate nor silently re-validate
  ('stale-toolchain' qualifier in both directions);
* backfill reuses ``marathon.review.verified_decls``' extraction
  (monkeypatched gh — no network), refuses to write without --attest,
  and skips-with-reason instead of guessing;
* a v1 ledger db (built with the v1 DDL verbatim) upgrades in place to
  v2 with rows intact; the future-version guard still refuses v3+.

No subprocesses, no network, no Lean toolchain.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.audit.trust import (
    TIER_ORDER,
    BackfillSkip,
    TierResult,
    TrustError,
    apply_backfill,
    compute_tier,
    compute_tiers,
    plan_backfill,
    record_revocation,
    record_spec_verdict,
)
from marathon.ledger import SCHEMA_VERSION, Ledger, LedgerError
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


# --- builders (same style as test_audit_engine) ------------------------------


def mk_decl(
    name="Foo.bar",
    kind="theorem",
    module="Foo",
    status="ok",
    type_pp="Nat",
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


TOOLCHAIN = "leanprover/lean4:v4.28.0"


def mk_snapshot(decls, *, repo_dir="/r", failures=(), **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir=repo_dir,
        modules=["Foo"],
        toolchain=TOOLCHAIN,
        lean_version="4.28.0",
        package_revs={},
        trusted_prefixes=list(DEFAULT_TRUSTED_PREFIXES),
        created_at="2026-06-12T00:00:00+00:00",
        decls=list(decls),
        failures=list(failures),
    )
    defaults.update(kw)
    return AuditSnapshot(**defaults)


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger.for_repo(tmp_path)


def cone_pair():
    """A theorem whose statement cone contains one project-local def."""
    helper = mk_decl(
        name="Foo.helper", kind="def", type_pp="Nat → Nat",
        value_pp="fun n => n",
    )
    main = mk_decl(name="Foo.main", type_pp="Foo.helper 1 = 1",
                   cone=["Foo.helper"])
    return helper, main


# --- the ladder, machine rungs -----------------------------------------------


def test_absent_decl_is_unknown(ledger):
    snap = mk_snapshot([mk_decl()])
    result = compute_tier("Foo.gone", snap, ledger)
    assert result.tier == "UNKNOWN"
    assert result.qualifiers == []  # never punished
    assert any("absent" in line for line in result.evidence)


def test_status_unknown_is_unknown_even_with_verdicts(ledger):
    """A decl that stopped elaborating reports UNKNOWN — its old human
    verdict neither rescues nor penalizes it."""
    ok = mk_decl(name="Foo.flaky")
    record_spec_verdict(ledger, "Foo.flaky", mk_snapshot([ok]))
    broken = mk_decl(
        name="Foo.flaky", status="unknown", type_pp=None,
        has_sorry=None, reason="elaboration failed",
    )
    result = compute_tier("Foo.flaky", mk_snapshot([broken]), ledger)
    assert result.tier == "UNKNOWN"
    assert result.qualifiers == []
    assert any("did not elaborate" in line for line in result.evidence)


def test_axiom_dirt_blocks_t1(ledger):
    snap = mk_snapshot([mk_decl(axioms=["propext", "Foo.cheat_ax"])])
    result = compute_tier("Foo.bar", snap, ledger)
    assert result.tier == "T0"
    assert any("Foo.cheat_ax" in line for line in result.evidence)


def test_deception_tag_blocks_t1(ledger):
    snap = mk_snapshot([mk_decl(tags=["UNUSED_HYPOTHESES"])])
    result = compute_tier("Foo.bar", snap, ledger)
    assert result.tier == "T0"
    assert any("deception tags" in line for line in result.evidence)


def test_clean_decl_is_t1_and_sorry_is_accounted_not_failed(ledger):
    whitelisted = mk_snapshot([
        mk_decl(axioms=["propext", "Classical.choice", "Quot.sound"]),
    ])
    assert compute_tier("Foo.bar", whitelisted, ledger).tier == "T1"
    sorried = mk_snapshot([mk_decl(axioms=["sorryAx"], has_sorry=True)])
    result = compute_tier("Foo.bar", sorried, ledger)
    assert result.tier == "T1"
    assert any("accounted" in line for line in result.evidence)


def test_axiom_dirt_caps_tier_below_a_recorded_verdict(ledger):
    """The ladder is a ladder: a human verdict cannot lift a decl over
    a failed machine rung."""
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap)
    dirty = mk_snapshot([mk_decl(axioms=["Foo.cheat_ax"])])
    assert compute_tier("Foo.bar", dirty, ledger).tier == "T0"


# --- the ladder, human rungs -------------------------------------------------


def test_spec_verdict_with_matching_pins_is_t2(ledger):
    helper, main = cone_pair()
    snap = mk_snapshot([helper, main])
    record_spec_verdict(ledger, "Foo.main", snap, issue_num=14)
    result = compute_tier("Foo.main", snap, ledger)
    assert result.tier == "T2"
    assert result.qualifiers == []


def test_no_verdict_means_t1(ledger):
    snap = mk_snapshot([mk_decl()])
    result = compute_tier("Foo.bar", snap, ledger)
    assert result.tier == "T1"
    assert any("no human spec-verdict" in line for line in result.evidence)


def test_fingerprint_mismatch_degrades_t2_to_t1(ledger):
    snap = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", snap)
    changed = mk_snapshot([mk_decl(type_pp="Int")])
    result = compute_tier("Foo.bar", changed, ledger)
    assert result.tier == "T1"
    assert "fingerprint-changed" in result.qualifiers


def test_cone_member_change_degrades_and_names_the_member(ledger):
    helper, main = cone_pair()
    record_spec_verdict(ledger, "Foo.main", mk_snapshot([helper, main]))
    # Cone pins are TYPE fingerprints — change the member's type.
    changed_helper = mk_decl(
        name="Foo.helper", kind="def", type_pp="Int → Int",
        value_pp="fun n => n",
    )
    result = compute_tier(
        "Foo.main", mk_snapshot([changed_helper, main]), ledger
    )
    assert result.tier == "T1"
    assert "cone-changed:Foo.helper" in result.qualifiers


def test_cone_member_vanishing_degrades_as_cone_missing(ledger):
    helper, main = cone_pair()
    record_spec_verdict(ledger, "Foo.main", mk_snapshot([helper, main]))
    result = compute_tier("Foo.main", mk_snapshot([main]), ledger)
    assert result.tier == "T1"
    assert "cone-missing:Foo.helper" in result.qualifiers


def test_revoked_supersedes_and_repin_restores_with_history(ledger):
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap)
    assert compute_tier("Foo.bar", snap, ledger).tier == "T2"

    record_revocation(ledger, "Foo.bar", notes="verified in error")
    result = compute_tier("Foo.bar", snap, ledger)
    assert result.tier == "T1"
    assert any("revoked" in line for line in result.evidence)

    record_spec_verdict(ledger, "Foo.bar", snap, source="repin")
    assert compute_tier("Foo.bar", snap, ledger).tier == "T2"

    # Append-only: all three events survive, newest first.
    events = ledger.decl_verdict_events("Foo.bar")
    assert [e.verdict for e in events] == ["verified", "revoked", "verified"]
    assert [e.source for e in events] == ["repin", "cli", "cli"]
    assert [e.id for e in events] == sorted(
        (e.id for e in events), reverse=True
    )


def test_line_review_verdict_reaches_t3(ledger):
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap, tier="T2")
    record_spec_verdict(ledger, "Foo.bar", snap, tier="T3")
    result = compute_tier("Foo.bar", snap, ledger)
    assert result.tier == "T3"
    # Documented v1 limitation rides along in the evidence trail.
    assert any("proof bodies are not pinned" in line
               for line in result.evidence)


def test_t3_alone_subsumes_the_spec_rung(ledger):
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap, tier="T3")
    assert compute_tier("Foo.bar", snap, ledger).tier == "T3"


def test_revoking_t3_falls_back_to_t2(ledger):
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap, tier="T2")
    record_spec_verdict(ledger, "Foo.bar", snap, tier="T3")
    record_revocation(ledger, "Foo.bar", tier="T3")
    assert compute_tier("Foo.bar", snap, ledger).tier == "T2"


def test_t3_pins_degrade_like_t2_pins(ledger):
    snap = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", snap, tier="T3")
    changed = mk_snapshot([mk_decl(type_pp="Int")])
    result = compute_tier("Foo.bar", changed, ledger)
    assert result.tier == "T1"
    assert "fingerprint-changed" in result.qualifiers


def test_compute_tiers_batches_the_whole_snapshot(ledger):
    helper, main = cone_pair()
    other = mk_decl(name="Foo.other", tags=["PROOF_BY_TRUST_ME"])
    snap = mk_snapshot([helper, main, other])
    record_spec_verdict(ledger, "Foo.main", snap)
    results = {r.decl_name: r for r in compute_tiers(snap, ledger)}
    assert len(results) == 3
    assert results["Foo.main"].tier == "T2"
    assert results["Foo.helper"].tier == "T1"
    assert results["Foo.other"].tier == "T0"
    # Batched and per-decl computations agree.
    assert results["Foo.main"].tier == compute_tier(
        "Foo.main", snap, ledger
    ).tier


def test_compute_tiers_surfaces_verdict_bearing_absentee_as_unknown(ledger):
    """A decl a human verified that has since vanished from the snapshot
    must appear as an UNKNOWN row, not silently drop off the table
    (the 'absent = UNKNOWN, reported never hidden' ruling)."""
    decl = mk_decl(name="Foo.gone")
    record_spec_verdict(ledger, "Foo.gone", mk_snapshot([decl]))
    # New snapshot no longer contains Foo.gone.
    other = mk_decl(name="Foo.present")
    results = {r.decl_name: r for r in compute_tiers(mk_snapshot([other]), ledger)}
    assert set(results) == {"Foo.present", "Foo.gone"}
    assert results["Foo.gone"].tier == "UNKNOWN"


# --- toolchain staleness -----------------------------------------------------


def test_stale_toolchain_qualifier_when_pins_still_match(ledger):
    snap = mk_snapshot([mk_decl()])
    record_spec_verdict(ledger, "Foo.bar", snap)
    bumped = mk_snapshot([mk_decl()], toolchain="leanprover/lean4:v4.29.0")
    result = compute_tier("Foo.bar", bumped, ledger)
    # Matching project-local fingerprints across toolchains keep the
    # tier, but never silently: the qualifier stays until re-pinned.
    assert result.tier == "T2"
    assert "stale-toolchain" in result.qualifiers


def test_stale_toolchain_mismatch_withholds_without_claiming_change(ledger):
    snap = mk_snapshot([mk_decl(type_pp="Nat")])
    record_spec_verdict(ledger, "Foo.bar", snap)
    bumped = mk_snapshot(
        [mk_decl(type_pp="Nat ")],  # pp drift, plausibly toolchain-caused
        toolchain="leanprover/lean4:v4.29.0",
    )
    result = compute_tier("Foo.bar", bumped, ledger)
    assert result.tier == "T1"
    assert "stale-toolchain" in result.qualifiers
    # A cross-toolchain mismatch is unverifiable, not a detected
    # meaning change — no silent invalidation under a change claim.
    assert "fingerprint-changed" not in result.qualifiers
    assert any("re-pin" in line for line in result.evidence)


# --- recording refusals ------------------------------------------------------


def test_record_refuses_absent_decl(ledger):
    with pytest.raises(TrustError, match="didn't elaborate"):
        record_spec_verdict(ledger, "Foo.gone", mk_snapshot([mk_decl()]))
    assert ledger.decl_verdict_events("Foo.gone") == []


def test_record_refuses_unknown_decl(ledger):
    broken = mk_decl(status="unknown", type_pp=None, has_sorry=None,
                     reason="kaboom")
    with pytest.raises(TrustError, match="didn't elaborate"):
        record_spec_verdict(ledger, "Foo.bar", mk_snapshot([broken]))


def test_record_refuses_unpinnable_cone(ledger):
    _, main = cone_pair()  # Foo.helper deliberately absent
    with pytest.raises(TrustError, match="Foo.helper"):
        record_spec_verdict(ledger, "Foo.main", mk_snapshot([main]))


def test_record_refuses_bogus_tier(ledger):
    with pytest.raises(TrustError, match="tier"):
        record_spec_verdict(
            ledger, "Foo.bar", mk_snapshot([mk_decl()]), tier="T9"
        )


def test_verdict_pins_current_fingerprints_and_toolchain(ledger):
    helper, main = cone_pair()
    snap = mk_snapshot([helper, main])
    record_spec_verdict(ledger, "Foo.main", snap, issue_num=14,
                        notes="spec read")
    (event,) = ledger.decl_verdict_events("Foo.main")
    assert event.fingerprint_type == main.fingerprint_type
    assert event.cone == [
        {"name": "Foo.helper", "fingerprint": helper.fingerprint_type},
    ]
    assert event.toolchain == TOOLCHAIN
    assert event.issue_num == 14
    assert event.tier_claimed == "T2"
    assert event.verdict == "verified"
    assert event.notes == "spec read"


# --- ledger: schema v2, migration, append-only -------------------------------

# The v1 DDL verbatim (what the v1 code path's executescript produced),
# so the migration test runs against a byte-faithful v1 db file.
_V1_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_num         INTEGER PRIMARY KEY,
    chapter           INTEGER,
    status            TEXT NOT NULL
                      CHECK (status IN ('rejected', 'verified', 'stalled')),
    verdict_ts        TEXT NOT NULL,
    notes             TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    last_iteration_ts TEXT
);

CREATE TABLE IF NOT EXISTS verdict_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_num INTEGER NOT NULL,
    verdict   TEXT NOT NULL CHECK (verdict IN ('rejected', 'verified')),
    notes     TEXT,
    ts        TEXT NOT NULL,
    source    TEXT NOT NULL CHECK (source IN ('cli', 'import', 'sync'))
);

CREATE UNIQUE INDEX IF NOT EXISTS verdict_events_dedup
    ON verdict_events (issue_num, verdict, ts, source);

CREATE TABLE IF NOT EXISTS chapters (
    chapter      INTEGER PRIMARY KEY,
    target_path  TEXT,
    entries_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS wall_time (
    project_id  TEXT PRIMARY KEY,
    seconds     INTEGER NOT NULL,
    recorded_at TEXT
);

CREATE TABLE IF NOT EXISTS prompt_log (
    project_id TEXT PRIMARY KEY,
    url        TEXT,
    first_seen TEXT
);

CREATE TABLE IF NOT EXISTS skeleton_chapters (
    workdir          TEXT NOT NULL,
    input_file       TEXT NOT NULL,
    output_folder    TEXT,
    project_id       TEXT,
    agent_task_id    TEXT,
    status           TEXT,
    started_at       TEXT,
    completed_at     TEXT,
    duration_seconds REAL,
    output_path      TEXT,
    note             TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (workdir, input_file)
);

CREATE TABLE IF NOT EXISTS refine_runs (
    workdir               TEXT PRIMARY KEY,
    target_folder         TEXT,
    iterations_completed  INTEGER NOT NULL DEFAULT 0,
    current_iteration_idx INTEGER NOT NULL DEFAULT 0,
    project_id            TEXT,
    agent_task_id         TEXT,
    status                TEXT,
    started_at            TEXT,
    completed_at          TEXT,
    duration_seconds      REAL,
    attempts              INTEGER NOT NULL DEFAULT 0,
    output_path           TEXT,
    note                  TEXT
);
"""


def write_v1_db(repo_dir: Path) -> Path:
    """A real v1 db file: v1 DDL verbatim, version stamp '1', and a
    couple of live rows that the migration must carry through."""
    db_path = repo_dir / ".marathon" / "marathon.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_V1_SCHEMA_SQL)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
        )
        conn.execute(
            "INSERT INTO issues (issue_num, status, verdict_ts) "
            "VALUES (14, 'verified', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO verdict_events (issue_num, verdict, ts, source) "
            "VALUES (14, 'verified', '2026-01-01T00:00:00', 'cli')"
        )
    return db_path


def test_v1_db_upgrades_in_place_with_rows_intact(tmp_path):
    db_path = write_v1_db(tmp_path)
    ledger = Ledger.for_repo(tmp_path)
    info = ledger.status()  # any op migrates on open
    assert info["schema_version"] == SCHEMA_VERSION == 3
    assert info["tables"]["issues"] == 1
    assert info["tables"]["verdict_events"] == 1
    assert info["tables"]["decl_verdicts"] == 0
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone() == ("3",)
    # And the new table is live.
    ledger.append_decl_verdict(
        "Foo.bar", tier_claimed="T2", verdict="verified",
        fingerprint_type="abc", cone=[], toolchain=TOOLCHAIN,
        ts="2026-06-12T00:00:00+00:00", source="cli",
    )
    assert len(ledger.decl_verdict_events("Foo.bar")) == 1


def test_migration_is_idempotent(tmp_path):
    write_v1_db(tmp_path)
    ledger = Ledger.for_repo(tmp_path)
    assert ledger.status() == ledger.status()
    assert ledger.status()["schema_version"] == 3


def test_future_schema_version_still_refused(tmp_path):
    ledger = Ledger.for_repo(tmp_path)
    ledger.init()
    with sqlite3.connect(ledger.db_path) as conn:
        conn.execute(
            "UPDATE meta SET value = '4' WHERE key = 'schema_version'"
        )
    with pytest.raises(LedgerError, match="newer"):
        ledger.status()
    with pytest.raises(LedgerError, match="newer"):
        ledger.decl_verdict_events("Foo.bar")


def test_decl_verdicts_is_append_only_at_the_schema_level(ledger):
    ledger.append_decl_verdict(
        "Foo.bar", tier_claimed="T2", verdict="verified",
        fingerprint_type="abc", cone=[], toolchain=TOOLCHAIN,
        ts="2026-06-12T00:00:00+00:00",
    )
    with sqlite3.connect(ledger.db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE decl_verdicts SET verdict = 'revoked'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM decl_verdicts")
    # And the API surface offers no rewrite path.
    assert not any(
        "decl_verdict" in name and ("update" in name or "delete" in name)
        for name in dir(ledger)
    )


def test_event_queries_are_newest_first_and_grouped(ledger):
    for i, name in enumerate(["Foo.a", "Foo.b", "Foo.a"]):
        ledger.append_decl_verdict(
            name, tier_claimed="T2", verdict="verified",
            fingerprint_type=f"fp{i}", cone=[], toolchain=TOOLCHAIN,
            ts=f"2026-06-12T00:00:0{i}+00:00",
        )
    events = ledger.decl_verdict_events("Foo.a")
    assert [e.fingerprint_type for e in events] == ["fp2", "fp0"]
    grouped = ledger.all_decl_verdict_events()
    assert set(grouped) == {"Foo.a", "Foo.b"}
    assert [e.fingerprint_type for e in grouped["Foo.a"]] == ["fp2", "fp0"]


def test_bad_source_is_rejected_by_check_constraint(ledger):
    with pytest.raises(sqlite3.IntegrityError):
        ledger.append_decl_verdict(
            "Foo.bar", tier_claimed="T2", verdict="verified",
            fingerprint_type="abc", cone=[], toolchain=TOOLCHAIN,
            ts="2026-06-12T00:00:00+00:00", source="import",
        )


# --- backfill ----------------------------------------------------------------


def make_cfg(tmp_path: Path) -> ReviewConfig:
    """Same fixture style as test_ledger.make_cfg."""
    return ReviewConfig(
        repo_dir=tmp_path,
        config_path=tmp_path / ".marathon" / "review" / "config.toml",
        github_repo="someone/SomeProject",
        parent_issue=1,
        referee_path=tmp_path / ".marathon" / "referee.md",
        target_path_template="SomeProject/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(14, "Lemma 14.7"), (15, "Proposition 14.8")],
            ),
        },
    )


def issue_meta(body: str, verified: bool = True) -> dict:
    labels = {"review:verified"} if verified else {"review:rejected"}
    return {"title": "t", "state": "OPEN", "body": body, "labels": labels}


@pytest.fixture
def fake_issues(monkeypatch):
    """Monkeypatch the ONE gh boundary the extraction reuses
    (verified_decls.fetch_issues_bulk); returns the setter."""

    def install(meta: dict[int, dict]):
        calls: list[tuple] = []

        def fake_bulk(nums, repo):
            calls.append((list(nums), repo))
            return {n: meta[n] for n in nums if n in meta}

        monkeypatch.setattr(
            "marathon.review.verified_decls.fetch_issues_bulk", fake_bulk
        )
        return calls

    return install


def snap_decls() -> list[DeclAudit]:
    """The two-decl consumer-project snapshot the backfill tests share."""
    return [
        mk_decl(name="SomeProject.foo_thm", type_pp="1 + 1 = 2",
                module="SomeProject"),
        mk_decl(name="SomeProject.bar_def", kind="def", type_pp="Nat",
                value_pp="0", module="SomeProject"),
    ]


def test_backfill_reuses_extraction_and_resolves_suffixes(
    tmp_path, ledger, fake_issues
):
    cfg = make_cfg(tmp_path)
    calls = fake_issues({
        14: issue_meta("### Lean signatures\n```lean\n"
                       "theorem foo_thm : 1 + 1 = 2 := by simp\n```"),
        15: issue_meta("```lean\ndef gone_def : Nat := 0\n```"),
    })
    snap = mk_snapshot(snap_decls())
    plan = plan_backfill(cfg, snap, ledger, chapters=[14])
    # One bulk gh call for the registry — the verified_decls path.
    assert calls == [([14, 15], "someone/SomeProject")]
    # 'foo_thm' (unqualified in the issue body) resolved to the unique
    # snapshot suffix match; never guessed.
    assert [(i.decl_name, i.issue_num, i.cone_size) for i in plan.items] == [
        ("SomeProject.foo_thm", 14, 0),
    ]
    assert plan.items[0].fingerprint_type == snap_decls()[0].fingerprint_type
    # The decl cited by #15 is missing from the snapshot:
    # skipped-with-reason.
    assert plan.skipped == [
        BackfillSkip("gone_def", 15, "not in the latest snapshot"),
    ]
    # Planning writes nothing.
    assert ledger.all_decl_verdict_events() == {}


def test_backfill_skips_unverified_issues_and_unknown_decls(
    tmp_path, ledger, fake_issues
):
    cfg = make_cfg(tmp_path)
    fake_issues({
        14: issue_meta("```lean\ntheorem foo_thm : 1 + 1 = 2 := rfl\n```",
                       verified=False),
        15: issue_meta("```lean\ndef bar_def : Nat := 0\n```"),
    })
    decls = [
        mk_decl(name="SomeProject.foo_thm", module="SomeProject"),
        mk_decl(name="SomeProject.bar_def", kind="def", status="unknown",
                type_pp=None, has_sorry=None, module="SomeProject"),
    ]
    plan = plan_backfill(cfg, mk_snapshot(decls), ledger, chapters=[14])
    assert plan.items == []
    # Unverified issue contributes nothing at all (label gate upstream);
    # the verified issue's decl failed to elaborate: skip-with-reason.
    assert [(s.name, s.reason) for s in plan.skipped] == [
        ("bar_def", "status=unknown — did not elaborate"),
    ]


def test_apply_backfill_writes_t2_events_and_rerun_is_a_noop(
    tmp_path, ledger, fake_issues
):
    cfg = make_cfg(tmp_path)
    fake_issues({
        14: issue_meta("```lean\ntheorem foo_thm : 1 + 1 = 2 := rfl\n```"),
        15: issue_meta("```lean\ndef bar_def : Nat := 0\n```"),
    })
    snap = mk_snapshot(snap_decls())
    plan = plan_backfill(cfg, snap, ledger)  # chapters=None → all
    assert len(plan.items) == 2
    assert apply_backfill(ledger, snap, plan) == 2
    for name, issue_num in [
        ("SomeProject.foo_thm", 14), ("SomeProject.bar_def", 15),
    ]:
        (event,) = ledger.decl_verdict_events(name)
        assert event.source == "backfill"
        assert event.tier_claimed == "T2"
        assert event.issue_num == issue_num
        assert compute_tier(name, snap, ledger).tier == "T2"
    # Re-planning finds everything already pinned — a second attest
    # pass appends no duplicate events.
    replan = plan_backfill(cfg, snap, ledger)
    assert replan.items == []
    assert all(
        "already pinned" in s.reason for s in replan.skipped
    ) and len(replan.skipped) == 2


def write_consumer_repo(tmp_path: Path, snap: AuditSnapshot) -> None:
    """A consumer repo the CLI handler can run against: saved snapshot
    + minimal review config.toml."""
    from marathon.audit.engine import save_snapshot

    save_snapshot(snap, tmp_path)
    cfg_path = tmp_path / ".marathon" / "review" / "config.toml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        'github_repo = "someone/SomeProject"\n'
        "parent_issue = 1\n"
        'target_path_template = "SomeProject/Chapter{chapter}"\n'
        "\n"
        "[[chapters]]\n"
        "chapter = 14\n"
        'entries = [[14, "Lemma 14.7"], [15, "Proposition 14.8"]]\n'
    )


def test_cli_backfill_requires_attest_to_write(
    tmp_path, ledger, fake_issues, capsys
):
    from marathon.__main__ import _run_audit_backfill

    fake_issues({
        14: issue_meta("```lean\ntheorem foo_thm : 1 + 1 = 2 := rfl\n```"),
        15: issue_meta("```lean\ndef bar_def : Nat := 0\n```"),
    })
    write_consumer_repo(tmp_path, mk_snapshot(snap_decls()))

    args = argparse.Namespace(repo_dir=tmp_path, chapter=14, attest=False)
    _run_audit_backfill(args)
    out = capsys.readouterr().out
    # The full would-be-pinned table prints (decl, issue, fingerprint
    # prefix, cone size) — but nothing is written without --attest.
    assert "SomeProject.foo_thm" in out
    assert "#14" in out
    assert snap_decls()[0].fingerprint_type[:12] in out
    assert "dry run" in out
    assert ledger.all_decl_verdict_events() == {}

    _run_audit_backfill(
        argparse.Namespace(repo_dir=tmp_path, chapter=14, attest=True)
    )
    out = capsys.readouterr().out
    assert "wrote 2 T2 verdict event(s)" in out
    assert len(ledger.all_decl_verdict_events()) == 2


# --- CLI tiers ---------------------------------------------------------------


def test_cli_tiers_table_and_summary(tmp_path, ledger, capsys):
    from marathon.__main__ import _run_audit_tiers
    from marathon.audit.engine import save_snapshot

    helper, main = cone_pair()
    tagged = mk_decl(name="Foo.fishy", tags=["UNUSED_HYPOTHESES"])
    snap = mk_snapshot([helper, main, tagged])
    save_snapshot(snap, tmp_path)
    record_spec_verdict(ledger, "Foo.main", snap, issue_num=14)

    _run_audit_tiers(
        argparse.Namespace(repo_dir=tmp_path, target=None)
    )
    out = capsys.readouterr().out
    assert "Foo.main" in out and "T2" in out
    assert "Foo.fishy" in out and "T0" in out
    assert "summary: UNKNOWN=0  T0=1  T1=1  T2=1  T3=0" in out


def test_cli_tiers_requires_a_snapshot(tmp_path, capsys):
    from marathon.__main__ import _run_audit_tiers

    with pytest.raises(SystemExit):
        _run_audit_tiers(
            argparse.Namespace(repo_dir=tmp_path, target=None)
        )
    assert "no latest audit snapshot" in capsys.readouterr().out


def test_tier_order_constant_matches_the_ladder():
    assert TIER_ORDER == ("UNKNOWN", "T0", "T1", "T2", "T3")
    # TierResult defaults: no qualifiers, no evidence — computed, not
    # stored, so there is deliberately no persistence hook to test.
    result = TierResult("Foo.bar", "T1")
    assert result.qualifiers == [] and result.evidence == []
