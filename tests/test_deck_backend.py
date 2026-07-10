"""Tests for the Phase-8 deck backend (marathon.deck).

Contract under test (the deck's task spec + the shared API contract):

* cards.build_queue — ready / non-ready classification, dep-ordering
  (topological, predecessors first), blocked cards carry a blocked_reason,
  and an honest degrade with NO audit snapshot (the Ch.12 case);
* verdicts.apply_verdict — ROUTES through the committed
  review.cmd_verify / cmd_reject (asserted by monkeypatching THOSE and
  checking the args, incl. the verbatim reject note); defer touches no
  Aristotle / GitHub (only a local marker);
* server — a GET never triggers a verdict; POST /api/verdict without the
  session token is 403; the server binds 127.0.0.1; the status endpoint
  emits from fabricated jobs.json / landings.jsonl.

Fully offline: gh (fetch_issues_bulk) and the committed verdict handlers
are monkeypatched; no network, no lake, no Aristotle, no real browser.
The HTTP layer is driven through a threaded server on an ephemeral port
via urllib — no browser, loopback only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from threading import Thread

import pytest

import marathon.deck.cards as cards
import marathon.deck.server as deck_server
import marathon.deck.verdicts as verdicts
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels

REPO_SLUG = "example/Demo"


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def make_cfg(tmp_path) -> ReviewConfig:
    """Minimal in-memory ReviewConfig rooted at tmp_path/repo. One chapter
    (14) with three issues; issue 23's decl depends (statement cone) on
    issue 22's decl, issue 24 is independent."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG,
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(22, "Def Foo"), (23, "Lemma Bar"), (24, "Def Baz")],
            ),
        },
    )


def _decl(name, *, kind="theorem", type_pp="Type", cone=(), axioms=("propext",),
          has_sorry=False, value_pp=None, tags=()) -> DeclAudit:
    return DeclAudit(
        name=name,
        kind=kind,
        module="Demo.Chapter14",
        status="ok",
        type_pp=type_pp,
        value_pp=value_pp,
        cone=list(cone),
        axioms=list(axioms),
        has_sorry=has_sorry,
        tags=list(tags),
        reason=None,
    )


def make_snapshot(decls) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir="/repo",
        modules=["Demo.Chapter14"],
        toolchain="leanprover/lean4:v4.0.0",
        lean_version="4.0.0",
        package_revs={},
        trusted_prefixes=["Mathlib", "Init"],
        created_at="2026-06-14T00:00:00+00:00",
        decls=list(decls),
        failures=[],
    )


def _bodies_for(issue_decls: dict[int, str]) -> dict:
    """Build the {issue_num: meta} dict fetch_issues_bulk would return,
    each body citing its decl in a ```lean block (the parser the deck
    reuses)."""
    out = {}
    for num, decl in issue_decls.items():
        out[num] = {
            "title": f"#{num} {decl}",
            "state": "OPEN",
            "body": f"### Lean signatures\n\n```lean\ndef {decl} : T := ?\n```",
            "labels": set(),
        }
    return out


def install_snapshot(cfg, snapshot) -> None:
    from marathon.audit.engine import save_snapshot

    save_snapshot(snapshot, cfg.repo_dir)


def patch_bulk(monkeypatch, meta: dict) -> None:
    """Monkeypatch the gh bulk fetch (the ONLY network read on the read
    path) to return ``meta`` for the requested issues."""
    def fake_bulk(nums, repo):
        return {n: meta[n] for n in nums if n in meta}

    monkeypatch.setattr(cards, "fetch_issues_bulk", fake_bulk, raising=False)
    # cards imports it inside _load_context, so patch the source too.
    import marathon.review.github as gh_mod

    monkeypatch.setattr(gh_mod, "fetch_issues_bulk", fake_bulk)
    monkeypatch.setattr(gh_mod, "fetch_issues_rest_bulk", lambda nums, repo: {})


def test_load_context_rest_fallback_on_whole_graphql_failure(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    import marathon.review.github as gh_mod

    graphql_calls = []
    rest_calls = []
    monkeypatch.setattr(
        gh_mod, "fetch_issues_bulk",
        lambda nums, repo: graphql_calls.append(list(nums)) or None,
    )
    recovered = _bodies_for({22: "Foo", 23: "Bar"})
    monkeypatch.setattr(
        gh_mod, "fetch_issues_rest_bulk",
        lambda nums, repo: (
            rest_calls.append(list(nums))
            or {n: recovered[n] for n in nums if n in recovered}
        ),
    )

    ctx = cards._load_context(cfg, [22, 23])

    assert graphql_calls == [[22, 23]]
    assert rest_calls == [[22, 23]]
    assert ctx.issue_meta == recovered


def test_load_context_rest_fallback_only_fetches_partial_graphql_gaps(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    import marathon.review.github as gh_mod

    metadata = _bodies_for({22: "Foo", 23: "Bar"})
    monkeypatch.setattr(
        gh_mod, "fetch_issues_bulk", lambda nums, repo: {22: metadata[22]}
    )
    rest_calls = []
    monkeypatch.setattr(
        gh_mod, "fetch_issues_rest_bulk",
        lambda nums, repo: (
            rest_calls.append(list(nums)) or {23: metadata[23]}
        ),
    )

    ctx = cards._load_context(cfg, [22, 23])

    assert rest_calls == [[23]]
    assert ctx.issue_meta == metadata


def test_load_context_rest_failure_is_fail_soft(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    import marathon.review.github as gh_mod

    monkeypatch.setattr(gh_mod, "fetch_issues_bulk", lambda nums, repo: None)

    def unavailable(nums, repo):
        raise RuntimeError("REST unavailable")

    monkeypatch.setattr(gh_mod, "fetch_issues_rest_bulk", unavailable)

    ctx = cards._load_context(cfg, [22])

    assert ctx.issue_meta == {}
    assert ctx.verdict_status == {}


def test_load_context_complete_graphql_fast_path_skips_rest(
    tmp_path, monkeypatch,
):
    cfg = make_cfg(tmp_path)
    import marathon.review.github as gh_mod

    metadata = _bodies_for({22: "Foo"})
    monkeypatch.setattr(
        gh_mod, "fetch_issues_bulk", lambda nums, repo: metadata
    )

    def unexpected(nums, repo):
        raise AssertionError("REST fallback ran despite complete GraphQL data")

    monkeypatch.setattr(gh_mod, "fetch_issues_rest_bulk", unexpected)

    ctx = cards._load_context(cfg, [22])

    assert ctx.issue_meta == metadata


# ---------------------------------------------------------------------------
# build_queue — ready / non-ready / dep-ordering
# ---------------------------------------------------------------------------


def test_queue_ready_and_dep_ordering(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # Foo (T1: axiom-clean, no tags). Bar's statement cone references Foo
    # (so issue 23 depends on issue 22). Baz independent (T1).
    snapshot = make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="fun x => x"),
        _decl("Demo.Chapter14.Bar", cone=["Demo.Chapter14.Foo"]),
        _decl("Demo.Chapter14.Baz", kind="def", value_pp="0"),
    ])
    install_snapshot(cfg, snapshot)
    patch_bulk(monkeypatch, _bodies_for({22: "Foo", 23: "Bar", 24: "Baz"}))

    queue = cards.build_queue(cfg, chapter=14)
    by_id = {c.id: c for c in queue.cards}

    # All three cards present, each T1.
    assert set(by_id) == {22, 23, 24}
    assert all(c.tier == "T1" for c in queue.cards)

    # Dep-ordering: Foo (22) — Bar's predecessor — comes before Bar (23).
    order = [c.id for c in queue.cards]
    assert order.index(22) < order.index(23)

    # Foo (22) and Baz (24) have no unresolved predecessors → ready.
    assert by_id[22].ready is True
    assert by_id[24].ready is True
    # Bar (23) waits on its predecessor Foo (22) (unreviewed) → NON-ready.
    assert by_id[23].ready is False
    assert by_id[23].blocked_reason is not None
    assert "#22" in by_id[23].blocked_reason


def test_blocked_card_carries_blocked_reason_below_floor(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # Foo carries a non-whitelist axiom → blocked at T0, below the T1 floor.
    snapshot = make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x",
              axioms=["propext", "Demo.badAxiom"]),
    ])
    install_snapshot(cfg, snapshot)
    # Only issue 22's body is fetchable here; 23/24 degrade to '-' (absent
    # from the bulk meta) — the test asserts solely on card 22.
    patch_bulk(monkeypatch, _bodies_for({22: "Foo"}))

    queue = cards.build_queue(cfg, chapter=14)
    card = next(c for c in queue.cards if c.id == 22)
    assert card.tier == "T0"
    assert card.ready is False
    assert card.blocked_reason is not None
    assert "below the machine-audit floor" in card.blocked_reason


def _cfg_one_issue(tmp_path, issue, label) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return ReviewConfig(
        repo_dir=repo, config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG, parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:", labels=ReviewLabels(),
        chapters={14: ChapterRegistry(chapter=14, entries=[(issue, label)])},
    )


def _snap_with_failure(cfg, decls, failed_module) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir=str(cfg.repo_dir), modules=["Demo.Chapter14"],
        toolchain="x", lean_version="4", package_revs={},
        trusted_prefixes=["Mathlib", "Init"], created_at="2026-06-14T00:00:00+00:00",
        decls=list(decls),
        failures=[f"module {failed_module} failed to build; excluded from audit"],
    )


def test_gating_deps_breaks_cycles_keeps_outside_edges():
    """A mutual/cyclic dependency (#237↔#243) can't be resolved by ordering, so
    it must not gate either card — but each still gates on predecessors OUTSIDE
    the cycle. Non-cyclic edges are untouched."""
    from marathon.deck.cards import _gating_deps
    deps = {238: [], 237: [238, 243], 243: [238, 237], 239: [238]}
    g = _gating_deps(deps)
    assert set(g[237]) == {238}   # cyclic 243 dropped, outside 238 kept
    assert set(g[243]) == {238}   # cyclic 237 dropped, outside 238 kept
    assert g[239] == [238]        # non-cyclic edge unchanged
    assert g[238] == []


def test_gating_deps_breaks_a_longer_cycle():
    """Cycles of length > 2 are broken too (transitive reachability)."""
    from marathon.deck.cards import _gating_deps
    deps = {1: [2], 2: [3], 3: [1], 4: [1]}  # 1->2->3->1 cycle; 4 depends on 1
    g = _gating_deps(deps)
    assert all(len(g[n]) == 0 for n in (1, 2, 3))  # every intra-cycle edge dropped
    assert g[4] == [1]                              # 4 is not in the cycle


def test_build_broken_issue_floored_and_blocked(tmp_path, monkeypatch):
    """The #244 Stokes case: an issue whose module fails to build must NOT read
    as a ready T1 card by inheriting the tier of an upstream decl it merely
    references. It is floored to '-' and blocked with a build-failure reason."""
    cfg = _cfg_one_issue(tmp_path, 25, "Broken Thm")
    # the failed module's SOURCE exists (it just won't compile).
    src = cfg.repo_dir / "Demo/Chapter14/Broken.lean"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("theorem BrokenThm : True := by exact upstreamRef\n")
    # Upstream is built (resolvable, T1); the Broken module is a FAILURE, so
    # BrokenThm is ABSENT from the snapshot — exactly #244's shape.
    install_snapshot(cfg, _snap_with_failure(
        cfg, [_decl("Demo.Chapter14.Upstream")], "Demo.Chapter14.Broken"))
    patch_bulk(monkeypatch, {25: {
        "title": "#25 Stokes-like", "state": "OPEN",
        "body": "### Lean signatures\n```lean\ntheorem BrokenThm : True\n```\n"
                "### Lean declarations\n- Upstream\n",
        "labels": set(),
    }})
    card = next(c for c in cards.build_queue(cfg, chapter=14).cards if c.id == 25)
    assert card.tier == "-"                       # NOT T1 inherited from Upstream
    assert card.ready is False
    assert "fails to build" in (card.blocked_reason or "")
    assert "BrokenThm" in (card.blocked_reason or "")


def test_built_decl_sharing_name_with_broken_module_not_blocked(tmp_path, monkeypatch):
    """False-positive guard: a card citing a BUILT decl whose bare name collides
    with a decl in a failed module is NOT blocked — build-broken requires the
    cited decl to be genuinely ABSENT from the snapshot."""
    cfg = _cfg_one_issue(tmp_path, 26, "Shared")
    src = cfg.repo_dir / "Demo/Chapter14/Broken.lean"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("theorem Shared : True := sorry\n")   # broken module ALSO has `Shared`
    # but the issue's `Shared` is the BUILT one (present in the snapshot).
    install_snapshot(cfg, _snap_with_failure(
        cfg, [_decl("Demo.Chapter14.Shared")], "Demo.Chapter14.Broken"))
    patch_bulk(monkeypatch, {26: {
        "title": "#26 Shared", "state": "OPEN",
        "body": "### Lean signatures\n```lean\ndef Shared : T := ?\n```",
        "labels": set(),
    }})
    card = next(c for c in cards.build_queue(cfg, chapter=14).cards if c.id == 26)
    assert card.tier != "-"        # resolves to the built decl → not floored
    assert card.ready is True      # not falsely blocked


def _two_chapter_cfg(tmp_path, chapters) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return ReviewConfig(
        repo_dir=repo, config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG, parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:", labels=ReviewLabels(),
        chapters=chapters,
    )


def _snap(decls) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir="/repo", modules=["Demo"], toolchain="x", lean_version="4",
        package_revs={}, trusted_prefixes=["Mathlib", "Init"],
        created_at="2026-06-14T00:00:00+00:00", decls=list(decls), failures=[],
    )


def test_predecessor_drops_later_chapter_citer(tmp_path, monkeypatch):
    """#171←#244 regression: a LATER-chapter issue that merely CITES an upstream
    decl is not a predecessor (deps flow forward). #40 (ch15) depends on `Shared`
    (defined by #22, ch15); #99 (ch16) also cites `Shared`. #40's only
    predecessor is #22 — never the downstream #99."""
    cfg = _two_chapter_cfg(tmp_path, {
        14: ChapterRegistry(chapter=14, entries=[]),
        15: ChapterRegistry(chapter=15, entries=[(22, "Shared"), (40, "Dependent")]),
        16: ChapterRegistry(chapter=16, entries=[(99, "Downstream")]),
    })
    install_snapshot(cfg, _snap([
        _decl("Demo.Chapter15.Shared", kind="def", value_pp="x"),
        _decl("Demo.Chapter15.Dependent", cone=["Demo.Chapter15.Shared"]),
        _decl("Demo.Chapter16.Downstream", cone=["Demo.Chapter15.Shared"]),
    ]))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": "```lean\ndef Shared : T := ?\n```", "labels": set()},
        40: {"title": "#40", "state": "OPEN",
             "body": "```lean\ndef Dependent : T := ?\n```", "labels": set()},
        99: {"title": "#99", "state": "OPEN",  # cites Shared as an upstream ref
             "body": "```lean\ndef Downstream : T := ?\n```\n"
                     "### Lean declarations\n- Shared\n", "labels": set()},
    })
    by = {c.id: c for c in cards.build_queue(cfg, chapter=None).cards}
    assert "#22" in (by[40].blocked_reason or "")    # depends on its definer
    assert "#99" not in (by[40].blocked_reason or "")  # NOT the downstream citer


def test_predecessor_ignores_own_decl_cited_downstream(tmp_path, monkeypatch):
    """#170←#244 regression: an issue that DEFINES a decl is not blocked behind a
    SAME-chapter issue that merely cites it. #22 defines `Foo` (its `Baz` cones
    onto `Foo`); #40 (same chapter) cites `Foo`. #22 stays ready."""
    cfg = _two_chapter_cfg(tmp_path, {
        15: ChapterRegistry(chapter=15, entries=[(22, "Foo"), (40, "Other")]),
    })
    install_snapshot(cfg, _snap([
        _decl("Demo.Chapter15.Foo", kind="def", value_pp="x"),
        _decl("Demo.Chapter15.Baz", cone=["Demo.Chapter15.Foo"]),   # #22's own, cones onto Foo
        _decl("Demo.Chapter15.Other", kind="def", value_pp="x"),
    ]))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": "```lean\ndef Foo : T := ?\ndef Baz : T := ?\n```", "labels": set()},
        40: {"title": "#40", "state": "OPEN",  # cites #22's Foo as an upstream ref
             "body": "```lean\ndef Other : T := ?\n```\n"
                     "### Lean declarations\n- Foo\n", "labels": set()},
    })
    card = next(c for c in cards.build_queue(cfg, chapter=15).cards if c.id == 22)
    assert card.ready is True                # NOT blocked behind its own dependent #40
    assert card.blocked_reason is None


def test_dep_predecessor_resolved_makes_card_ready(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    snapshot = make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x"),
        _decl("Demo.Chapter14.Bar", cone=["Demo.Chapter14.Foo"]),
        _decl("Demo.Chapter14.Baz", kind="def", value_pp="0"),
    ])
    install_snapshot(cfg, snapshot)
    patch_bulk(monkeypatch, _bodies_for({22: "Foo", 23: "Bar", 24: "Baz"}))

    # Verify the predecessor (issue 22) via the committed state path.
    from marathon.review.state import record_verification

    record_verification(cfg, 22)

    queue = cards.build_queue(cfg, chapter=14)
    by_id = {c.id: c for c in queue.cards}
    # 22 is now verified → not swipeable (already verified).
    assert by_id[22].ready is False
    assert by_id[22].blocked_reason == "already verified"
    # 23's only predecessor is resolved → ready now.
    assert by_id[23].ready is True
    assert by_id[23].blocked_reason is None


def test_queue_degrades_without_snapshot(tmp_path, monkeypatch):
    """The Ch.12 case: no audit snapshot at all. Cards still appear (body
    titles + decls), tier is '-', and every card is non-ready with the
    'no audit snapshot' reason — the deck works on an unaudited chapter."""
    cfg = make_cfg(tmp_path)
    # No install_snapshot — there is no .marathon/audit/latest.json.
    patch_bulk(monkeypatch, _bodies_for({22: "Foo", 23: "Bar", 24: "Baz"}))

    queue = cards.build_queue(cfg, chapter=14)
    assert {c.id for c in queue.cards} == {22, 23, 24}
    for c in queue.cards:
        assert c.tier == "-"
        assert c.ready is False
        assert c.blocked_reason is not None
        assert "no audit snapshot" in c.blocked_reason
        # The card still names its decl + title (from the issue body).
        assert c.decl == {22: "Foo", 23: "Bar", 24: "Baz"}[c.id]
        assert c.title


def test_card_detail_reuses_spec_card(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    snapshot = make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", type_pp="Nat", value_pp="0"),
        _decl("Demo.Chapter14.Bar", type_pp="Foo = Foo",
              cone=["Demo.Chapter14.Foo"]),
    ])
    install_snapshot(cfg, snapshot)
    patch_bulk(monkeypatch, _bodies_for({22: "Foo", 23: "Bar"}))

    detail = cards.build_card_detail(cfg, 23)
    assert detail.id == 23
    assert detail.decl == "Demo.Chapter14.Bar"
    assert detail.statement_pp == "Foo = Foo"
    # Kernel = the project-local def(s) in Bar's statement cone — Foo.
    assert [k.name for k in detail.kernel] == ["Demo.Chapter14.Foo"]
    # The permalink is derived purely from the repo slug (no network).
    assert detail.permalink == "https://github.com/example/Demo/issues/23"
    # Deps resolve back to the predecessor card (issue 22 owns Foo).
    assert [d.id for d in detail.deps] == [22]


def test_card_detail_includes_every_cited_decl(tmp_path, monkeypatch):
    """An issue bundles several declarations; the card must carry a DeclView
    for EACH (not just the headline), ordered worst-tier first, with the
    card-level tier = the worst over them."""
    cfg = make_cfg(tmp_path)
    snapshot = make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", type_pp="Nat", value_pp="0"),
        # A non-whitelisted axiom caps Bar at T0 (the worst of the three).
        _decl("Demo.Chapter14.Bar", kind="def", type_pp="Int", value_pp="1",
              axioms=["propext", "Demo.cheat"]),
        _decl("Demo.Chapter14.Baz", kind="theorem", type_pp="Foo = Foo"),
    ])
    install_snapshot(cfg, snapshot)
    body = (
        "### Lean signatures\n\n```lean\n"
        "def Foo : Nat := 0\n"
        "def Bar : Int := 1\n"
        "theorem Baz : Foo = Foo := by rfl\n"
        "```"
    )
    patch_bulk(monkeypatch, {
        30: {"title": "#30 bundle", "state": "OPEN", "body": body, "labels": set()},
    })

    detail = cards.build_card_detail(cfg, 30)

    # One view per cited declaration — not just the headline.
    assert {v.name for v in detail.decls} == {
        "Demo.Chapter14.Foo", "Demo.Chapter14.Bar", "Demo.Chapter14.Baz",
    }
    # Worst tier gates the card and leads the list; headline mirrors it.
    assert detail.tier == "T0"
    assert detail.decls[0].name == "Demo.Chapter14.Bar"
    assert detail.decls[0].tier == "T0"
    assert detail.decl == "Demo.Chapter14.Bar"
    # Each view carries its OWN tier (not the card-level worst).
    by_name = {v.name: v for v in detail.decls}
    assert by_name["Demo.Chapter14.Foo"].tier == "T1"
    assert by_name["Demo.Chapter14.Baz"].tier == "T1"
    # The top-level scalar fields mirror the headline view (backward-compat).
    assert detail.statement_pp == detail.decls[0].statement_pp


# ---------------------------------------------------------------------------
# apply_verdict — routes through cmd_verify / cmd_reject; defer is local
# ---------------------------------------------------------------------------


def test_verify_routes_through_cmd_verify(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x"),
    ]))
    patch_bulk(monkeypatch, _bodies_for({22: "Foo"}))

    calls = {}

    def fake_cmd_verify(args):
        calls["verify"] = args

    def fake_cmd_reject(args):  # must NOT be called for a verify
        calls["reject"] = args

    from marathon.review import review as review_mod

    monkeypatch.setattr(review_mod, "cmd_verify", fake_cmd_verify)
    monkeypatch.setattr(review_mod, "cmd_reject", fake_cmd_reject)

    result = verdicts.apply_verdict(cfg, 22, "verify")
    assert result.ok and result.verdict == "verify"
    # Routed through the committed cmd_verify with the right args.
    assert "verify" in calls and "reject" not in calls
    assert calls["verify"].issue_num == 22
    assert calls["verify"].close is False  # deck keeps the issue OPEN


def test_reject_routes_note_verbatim(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x"),
    ]))
    patch_bulk(monkeypatch, _bodies_for({22: "Foo"}))

    captured = {}

    def fake_cmd_reject(args):
        captured["args"] = args

    def fake_cmd_verify(args):  # must NOT be called for a reject
        captured["verify"] = args

    from marathon.review import review as review_mod

    monkeypatch.setattr(review_mod, "cmd_reject", fake_cmd_reject)
    monkeypatch.setattr(review_mod, "cmd_verify", fake_cmd_verify)

    note = "The hypothesis `h : x > 0` is wrong; it should be `x ≥ 0`."
    result = verdicts.apply_verdict(cfg, 22, "reject", note=note)
    assert result.ok and result.verdict == "reject"
    assert "verify" not in captured
    # The note reaches cmd_reject VERBATIM (no Claude in the loop — the
    # committed reject path's bypass is what carries it to Aristotle).
    assert captured["args"].notes == note
    assert captured["args"].issue_num == 22
    assert captured["args"].no_refine is False


def test_reject_with_empty_note_refused_before_side_effect(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    called = {}

    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: called.setdefault("reject", True),
    )
    with pytest.raises(verdicts.VerdictError):
        verdicts.apply_verdict(cfg, 22, "reject", note="   ")
    # The side effect never fired.
    assert "reject" not in called


def test_defer_touches_no_aristotle_or_github(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x"),
    ]))
    patch_bulk(monkeypatch, _bodies_for({22: "Foo"}))

    fired = {}

    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: fired.setdefault("verify", True),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: fired.setdefault("reject", True),
    )
    # gh must never be invoked by a defer — make any gh call explode.
    import marathon.review.github as gh_mod

    monkeypatch.setattr(
        gh_mod, "gh",
        lambda *a, **k: pytest.fail("defer must not call gh"),
    )

    result = verdicts.apply_verdict(cfg, 22, "defer")
    assert result.ok and result.verdict == "defer"
    # No committed verdict handler ran.
    assert fired == {}
    # The defer is recorded as a LOCAL marker only.
    assert 22 in verdicts.deferred_issue_nums(cfg)
    # And the cards layer overlays it as a non-ready 'deferred' status.
    queue = cards.build_queue(cfg, chapter=14)
    card = next(c for c in queue.cards if c.id == 22)
    assert card.ready is False
    assert card.blocked_reason == "deferred"


def test_unknown_verdict_refused(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(verdicts.VerdictError):
        verdicts.apply_verdict(cfg, 22, "maybe")


# ---------------------------------------------------------------------------
# server — binding, GET-is-pure, token gate, status
# ---------------------------------------------------------------------------


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """A DeckServer on an ephemeral 127.0.0.1 port, with the bulk fetch
    patched and a snapshot installed. Yields (server, base_url, token)."""
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x"),
        _decl("Demo.Chapter14.Bar", cone=["Demo.Chapter14.Foo"]),
        _decl("Demo.Chapter14.Baz", kind="def", value_pp="0"),
    ]))
    patch_bulk(monkeypatch, _bodies_for({22: "Foo", 23: "Bar", 24: "Baz"}))

    server = deck_server.make_server(cfg, port=0, default_chapter=14)
    host, port = server.server_address[:2]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}", server.token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(url, body: dict, headers=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_server_binds_loopback_only(running_server):
    server, _base, _token = running_server
    assert server.server_address[0] == "127.0.0.1"
    assert deck_server.BIND_HOST == "127.0.0.1"


def test_index_injects_token(running_server):
    _server, base, token = running_server
    status, html = _get(base + "/")
    assert status == 200
    # The placeholder is gone, the real token is present.
    assert deck_server.TOKEN_PLACEHOLDER not in html
    assert token in html


def test_get_queue_is_pure_no_verdict(running_server, monkeypatch):
    """Hitting any GET must never fire a verdict side effect."""
    server, base, _token = running_server
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("GET must not verify"),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: pytest.fail("GET must not reject"),
    )
    status, body = _get(base + "/api/queue?chapter=14")
    assert status == 200
    payload = json.loads(body)
    assert "cards" in payload and "building" in payload
    assert "landed_today" in payload
    ids = {c["id"] for c in payload["cards"]}
    assert ids == {22, 23, 24}
    # Also a card detail + status — all pure reads.
    status, _ = _get(base + "/api/card/22")
    assert status == 200
    status, sbody = _get(base + "/api/status")
    assert status == 200
    assert "jobs" in json.loads(sbody)


def test_definition_lookup_and_term_index_routes(running_server):
    server, base, _token = running_server
    source = server.cfg.repo_dir / "Demo" / "Chapter14.lean"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "namespace Demo.Chapter14\n"
        "def Foo : Nat := 1\n"
        "end Demo.Chapter14\n"
    )

    status, raw = _get(
        base + "/api/definition?name=Demo.Chapter14.Foo&issue=22"
    )
    payload = json.loads(raw)
    assert status == 200
    assert payload["status"] == "exact"
    assert payload["matches"][0]["source_line"] == 2
    assert payload["matches"][0]["owners"][0]["issue"] == 22

    status, raw = _get(base + "/api/definition-terms")
    terms = json.loads(raw)
    assert status == 200
    assert "Demo.Chapter14.Foo" in terms["names"]
    assert terms["short"]["Foo"] == ["Demo.Chapter14.Foo"]
    assert len(terms["source_token"]) == 64


def test_reconciliation_preview_and_guarded_decision_routes(
    running_server, monkeypatch
):
    _server, base, token = running_server
    from marathon.deck import refresh as refresh_mod

    proposal = {"old": "A.old", "new": "A.new"}
    preview = {
        "ok": True, "updated": False,
        "proposed_renames": [proposal], "proposed_splits": [],
        "proposal_token": "a" * 64, "message": "review",
    }
    monkeypatch.setattr(
        refresh_mod, "reconciliation_preview", lambda cfg, issue: preview
    )
    status, raw = _get(base + "/api/reconciliation/22")
    assert status == 200
    assert json.loads(raw)["proposed_renames"] == [proposal]

    seen = {}
    monkeypatch.setattr(
        refresh_mod,
        "confirm_reconciliation_proposal",
        lambda cfg, issue, confirmation: (
            seen.update(issue=issue, confirmation=confirmation)
            or {"ok": True, "updated": True, "renamed": [proposal]}
        ),
    )
    status, raw = _post(
        base + "/api/reconcile-proposal",
        {
            "issue": 22,
            "proposal_token": "a" * 64,
            "action": "confirm",
            "kind": "rename",
            "old": "A.old",
            "new": "A.new",
        },
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200 and json.loads(raw)["updated"] is True
    assert seen["issue"] == 22
    assert seen["confirmation"]["renames"] == [proposal]

    status, _raw = _post(
        base + "/api/reconcile-proposal",
        {
            "issue": 22, "proposal_token": "a" * 64,
            "action": "dismiss", "kind": "rename",
            "old": "A.old", "new": "A.new",
        },
    )
    assert status == 403


def test_post_verdict_without_token_is_403(running_server, monkeypatch):
    server, base, _token = running_server
    from marathon.review import review as review_mod

    # A tokenless POST must NOT route to the committed handlers at all.
    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("tokenless POST must not verify"),
    )
    status, body = _post(base + "/api/verdict", {"id": 22, "verdict": "verify"})
    assert status == 403
    assert json.loads(body)["ok"] is False


def test_post_verdict_with_wrong_token_is_403(running_server):
    _server, base, _token = running_server
    status, _ = _post(
        base + "/api/verdict",
        {"id": 22, "verdict": "verify"},
        headers={deck_server.TOKEN_HEADER: "not-the-token"},
    )
    assert status == 403


def test_post_verdict_with_token_routes(running_server, monkeypatch):
    server, base, token = running_server
    calls = {}
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: calls.setdefault("verify", args),
    )
    status, body = _post(
        base + "/api/verdict",
        {"id": 22, "verdict": "verify"},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True and payload["verdict"] == "verify"
    # Routed through the committed cmd_verify.
    assert calls["verify"].issue_num == 22


def test_post_verdict_cross_origin_refused(running_server):
    _server, base, token = running_server
    # A correct token but a foreign Origin → still refused (the loopback /
    # same-origin guard fires before the token is even consulted).
    status, _ = _post(
        base + "/api/verdict",
        {"id": 22, "verdict": "verify"},
        headers={
            deck_server.TOKEN_HEADER: token,
            "Origin": "http://evil.example.com",
        },
    )
    assert status == 403


def test_status_emits_from_runtime_files(tmp_path, monkeypatch):
    """The status poll blob reads conductor jobs.json purely from disk. (The
    landing queue — landings.jsonl / bounces — was deleted; the poll blob's
    landings/bounces are now always empty. Real merged-PR landings live on the
    Status tab via conductor_status._recent_landings.)"""
    cfg = make_cfg(tmp_path)
    from marathon.conductor import ConductorJob, write_jobs_snapshot

    job = ConductorJob(
        issue_num=23, chapter=14, target="Demo/Chapter14",
        worktree="/wt", workdir="/wd", branch="marathon/refine-c14-i23",
        pid=4242, started_ts="2026-06-14T01:00:00+00:00",
        status="running", project_id="abc-123",
        aristotle_status="RUNNING",
    )
    write_jobs_snapshot(cfg.repo_dir, [job], concurrency=2)

    status = deck_server.collect_status(cfg)
    assert status["building"] == 1
    assert len(status["jobs"]) == 1
    assert status["jobs"][0]["issue_num"] == 23
    assert status["jobs"][0]["aristotle_status"] == "RUNNING"
    assert status["landings"] == [] and status["bounces"] == []
    assert len(status["source_token"]) == 64

    # landed_today is 0 now (its landing-queue source is gone; merged-PR count
    # is a follow-up). The queue still builds + counts building jobs.
    patch_bulk(monkeypatch, _bodies_for({22: "Foo", 23: "Bar", 24: "Baz"}))
    queue = cards.build_queue(cfg, chapter=14)
    assert queue.landed_today == 0
    assert queue.building == 1


def test_sse_events_endpoint_emits_status(running_server):
    _server, base, _token = running_server
    status, body = _get(base + "/api/events")
    assert status == 200
    # SSE frame shape: "event: status\ndata: {...}\n\n".
    assert body.startswith("event: status")
    assert "data: " in body
    data_line = [l for l in body.splitlines() if l.startswith("data: ")][0]
    payload = json.loads(data_line[len("data: "):])
    assert "jobs" in payload and "landings" in payload


# ---------------------------------------------------------------------------
# Per-user credentials: forwarded GitHub token + trusted proxy origins
# ---------------------------------------------------------------------------

def test_post_binds_forwarded_actor_github_token(running_server, monkeypatch):
    """A proxy-forwarded GitHub token (X-Forwarded-Access-Token) is bound as the
    acting gh identity for the duration of the verdict's handler."""
    _server, base, token = running_server
    import marathon.review.github as github
    from marathon.review import review as review_mod

    seen = {}
    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: seen.setdefault("actor", github._ACTOR_GH_TOKEN.get()),
    )
    status, _ = _post(
        base + "/api/verdict", {"id": 22, "verdict": "verify"},
        headers={deck_server.TOKEN_HEADER: token,
                 deck_server.ACCESS_TOKEN_HEADER: "ghu_collab"},
    )
    assert status == 200
    assert seen["actor"] == "ghu_collab"        # collaborator's token was bound
    # and it does NOT leak past the request
    assert github._ACTOR_GH_TOKEN.get() is None


def test_post_without_forwarded_token_is_ambient(running_server, monkeypatch):
    """No forwarded token (the local single-user path) => ambient host gh auth."""
    _server, base, token = running_server
    import marathon.review.github as github
    from marathon.review import review as review_mod

    seen = {}
    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: seen.setdefault("actor", github._ACTOR_GH_TOKEN.get()),
    )
    _post(base + "/api/verdict", {"id": 22, "verdict": "verify"},
          headers={deck_server.TOKEN_HEADER: token})
    assert seen["actor"] is None


def test_trusted_origin_widens_same_origin_guard(tmp_path, monkeypatch):
    """A configured --trusted-origin is accepted by the POST same-origin guard;
    an untrusted foreign origin is still refused."""
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", value_pp="x")]))
    patch_bulk(monkeypatch, _bodies_for({22: "Foo"}))
    from marathon.review import review as review_mod
    monkeypatch.setattr(review_mod, "cmd_verify", lambda args: None)

    server = deck_server.make_server(
        cfg, port=0, default_chapter=14,
        trusted_origins=["https://marathon.example.com"])
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ok, _ = _post(
            base + "/api/verdict", {"id": 22, "verdict": "verify"},
            headers={deck_server.TOKEN_HEADER: server.token,
                     "Origin": "https://marathon.example.com"})
        assert ok == 200                         # trusted foreign origin allowed
        bad, _ = _post(
            base + "/api/verdict", {"id": 22, "verdict": "verify"},
            headers={deck_server.TOKEN_HEADER: server.token,
                     "Origin": "https://evil.example.com"})
        assert bad == 403                        # untrusted foreign origin refused
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
