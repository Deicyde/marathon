"""Tests for the Phase-8 SUBDECK backend (marathon.deck.subdeck).

Contract under test (the subdeck task spec + the shared API contract):

* the per-(issue,decl) verdict store — records verify/reject/defer LOCALLY
  (no Aristotle / GitHub / ledger); a reject REQUIRES a non-empty note;
  ``clear_issue`` drops a shipped issue's store;
* ``build_subdeck`` — reuses cards.build_card_detail for the per-decl views,
  overlays the local verdict/note, orders UNREVIEWED-first then worst-tier
  then name, and computes progress (defer does NOT count reviewed),
  completeness, and the {kept, rejected, deferred} summary;
* ``compose_ship_note`` — the exact KEEP/FIX composite (pure);
* ``ship_issue`` — refuses an incomplete subdeck (NO cmd_*); all-verified
  routes cmd_verify (NOT cmd_reject); any-rejected routes cmd_reject with
  the composed note VERBATIM (NOT cmd_verify); clears the store on success;
* the server — GET /api/subdeck never fires a verdict; POST /api/decl-verdict
  records locally (never cmd_*) and needs the token; POST /api/ship is the
  only new irreversible route and needs the token.

Fully offline: gh (fetch_issues_bulk) and the committed verdict handlers
(cmd_verify / cmd_reject) are monkeypatched; no network, no lake, no
Aristotle, no real browser. The HTTP layer is driven through a threaded
server on an ephemeral 127.0.0.1 port via urllib.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from threading import Thread

import pytest

import marathon.deck.cards as cards
import marathon.deck.server as deck_server
import marathon.deck.subdeck as subdeck
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels

REPO_SLUG = "example/Demo"


# ---------------------------------------------------------------------------
# Fixtures / builders (mirroring tests/test_deck_backend.py)
# ---------------------------------------------------------------------------


def make_cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo=REPO_SLUG,
        parent_issue=1,
        referee_path=repo / ".marathon/referee.md",
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


def install_snapshot(cfg, snapshot) -> None:
    from marathon.audit.engine import save_snapshot

    save_snapshot(snapshot, cfg.repo_dir)


def patch_bulk(monkeypatch, meta: dict) -> None:
    def fake_bulk(nums, repo):
        return {n: meta[n] for n in nums if n in meta}

    monkeypatch.setattr(cards, "fetch_issues_bulk", fake_bulk, raising=False)
    import marathon.review.github as gh_mod

    monkeypatch.setattr(gh_mod, "fetch_issues_bulk", fake_bulk)


def patch_no_verdict_handlers(monkeypatch) -> dict:
    """Monkeypatch the committed verdict handlers to record (not perform)
    their calls. Returns a ``calls`` dict the test asserts against."""
    calls: dict = {}
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: calls.setdefault("verify", args),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: calls.setdefault("reject", args),
    )
    return calls


def _bundle_body(*decls: str) -> str:
    """An issue body citing several decls in one ```lean block (the parser
    the deck reuses), one short ``def`` per name."""
    lines = "\n".join(f"def {d} : T := ?" for d in decls)
    return f"### Lean signatures\n\n```lean\n{lines}\n```"


def make_bundle_cfg(tmp_path, monkeypatch, *, issue=22):
    """A cfg + installed snapshot where issue ``issue`` (registered in
    chapter 14) bundles three decls: Foo (T1), Bar (T0 — a non-whitelist
    axiom caps it), Baz (T1). Returns the cfg."""
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.Foo", kind="def", type_pp="Nat", value_pp="0"),
        _decl("Demo.Chapter14.Bar", kind="def", type_pp="Int", value_pp="1",
              axioms=["propext", "Demo.cheat"]),
        _decl("Demo.Chapter14.Baz", kind="theorem", type_pp="Foo = Foo"),
    ]))
    patch_bulk(monkeypatch, {
        issue: {
            "title": f"#{issue} bundle", "state": "OPEN",
            "body": _bundle_body("Foo", "Bar", "Baz"), "labels": set(),
        },
    })
    return cfg


# ---------------------------------------------------------------------------
# Per-(issue,decl) verdict store
# ---------------------------------------------------------------------------


def test_store_records_and_reads_back(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 30, "Demo.Chapter14.Bar", "reject", note="needs ≥, not >"
    )
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Baz", "defer")

    recs = subdeck.issue_verdicts(cfg, 30)
    assert recs["Demo.Chapter14.Foo"]["verdict"] == "verify"
    assert recs["Demo.Chapter14.Bar"]["verdict"] == "reject"
    assert recs["Demo.Chapter14.Bar"]["note"] == "needs ≥, not >"
    assert recs["Demo.Chapter14.Baz"]["verdict"] == "defer"
    # Each record carries a timestamp.
    assert recs["Demo.Chapter14.Foo"]["ts"]
    # The sidecar lives in the .marathon/review dir (additive — not state.json).
    assert (cfg.repo_dir / subdeck.SUBDECK_RELPATH).is_file()


def test_store_reject_requires_note(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Bar", "reject")
    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(
            cfg, 30, "Demo.Chapter14.Bar", "reject", note="   "
        )
    # Nothing was written for the refused decl.
    assert "Demo.Chapter14.Bar" not in subdeck.issue_verdicts(cfg, 30)


def test_store_unknown_verdict_refused(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError):
        subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "maybe")


def test_store_verdict_supersedes(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "defer")
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    recs = subdeck.issue_verdicts(cfg, 30)
    assert recs["Demo.Chapter14.Foo"]["verdict"] == "verify"


def test_clear_issue_drops_store(tmp_path):
    cfg = make_cfg(tmp_path)
    subdeck.record_decl_verdict(cfg, 30, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 31, "Demo.Chapter14.Other", "verify")
    subdeck.clear_issue(cfg, 30)
    assert subdeck.issue_verdicts(cfg, 30) == {}
    # A sibling issue's store is untouched.
    assert subdeck.issue_verdicts(cfg, 31)["Demo.Chapter14.Other"]["verdict"] \
        == "verify"
    # Clearing an absent issue is a no-op.
    subdeck.clear_issue(cfg, 999)


# ---------------------------------------------------------------------------
# build_subdeck — overlay, ordering, progress / complete / summary
# ---------------------------------------------------------------------------


def test_build_subdeck_overlays_and_orders(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)

    sd = subdeck.build_subdeck(cfg, 22)
    # One card per cited decl, card_tier = worst over them (Bar is T0).
    assert {c["name"] for c in sd["cards"]} == {
        "Demo.Chapter14.Foo", "Demo.Chapter14.Bar", "Demo.Chapter14.Baz",
    }
    assert sd["card_tier"] == "T0"
    assert sd["issue"] == 22
    assert sd["chapter"] == 14
    # No verdicts yet → all cards unreviewed, ordered worst-tier first.
    assert all(c["verdict"] is None for c in sd["cards"])
    assert sd["cards"][0]["name"] == "Demo.Chapter14.Bar"  # T0 leads
    assert sd["progress"] == {"reviewed": 0, "total": 3}
    assert sd["complete"] is False

    # Review one decl (verify Foo): it moves AFTER the unreviewed cards.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    sd = subdeck.build_subdeck(cfg, 22)
    by_name = {c["name"]: c for c in sd["cards"]}
    # The SERIALIZED card carries the PAST-TENSE wire verdict the frontend
    # renders/styles (dv-verified) — the store stays present-tense ('verify').
    assert by_name["Demo.Chapter14.Foo"]["verdict"] == "verified"
    # Unreviewed-first ordering: the two unreviewed (Bar, Baz) precede Foo.
    order = [c["name"] for c in sd["cards"]]
    assert order.index("Demo.Chapter14.Foo") == 2  # reviewed → last
    assert sd["progress"] == {"reviewed": 1, "total": 3}
    assert sd["complete"] is False


def test_build_subdeck_defer_not_counted_reviewed(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Bar", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "defer")

    sd = subdeck.build_subdeck(cfg, 22)
    # Two verify + one defer → reviewed = 2 (defer does NOT count), incomplete.
    assert sd["progress"] == {"reviewed": 2, "total": 3}
    assert sd["complete"] is False
    assert sd["summary"]["kept"] == ["Demo.Chapter14.Bar", "Demo.Chapter14.Foo"]
    assert sd["summary"]["deferred"] == ["Demo.Chapter14.Baz"]
    assert sd["summary"]["rejected"] == []


def test_build_subdeck_complete_and_summary(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject", note="wrong bound"
    )
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "verify")

    sd = subdeck.build_subdeck(cfg, 22)
    assert sd["progress"] == {"reviewed": 3, "total": 3}
    assert sd["complete"] is True
    assert sd["summary"]["kept"] == ["Demo.Chapter14.Baz", "Demo.Chapter14.Foo"]
    assert sd["summary"]["rejected"] == [
        {"name": "Demo.Chapter14.Bar", "note": "wrong bound"},
    ]
    assert sd["summary"]["deferred"] == []
    # next_unreviewed_card is None when every decl is reviewed.
    assert subdeck.next_unreviewed_card(sd) is None


def test_next_unreviewed_card_points_at_first_open(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Bar", "verify")
    sd = subdeck.build_subdeck(cfg, 22)
    nxt = subdeck.next_unreviewed_card(sd)
    assert nxt is not None
    assert nxt["verdict"] is None
    assert nxt["name"] != "Demo.Chapter14.Bar"  # the reviewed one is skipped


# ---------------------------------------------------------------------------
# compose_ship_note — the exact KEEP / FIX format
# ---------------------------------------------------------------------------


def test_compose_ship_note_exact_format():
    summary = {
        "kept": ["A.Foo", "A.Baz"],
        "rejected": [
            {"name": "A.Bar", "note": "hypothesis h : x > 0 should be x ≥ 0"},
            {"name": "A.Qux", "note": "missing finiteness assumption"},
        ],
        "deferred": [],
    }
    note = subdeck.compose_ship_note(7, summary)
    expected = (
        "Per-declaration review of issue #7.\n"
        "\n"
        "KEEP these declarations exactly as written (human-verified — do NOT "
        "change their statements or signatures):\n"
        "- A.Foo\n"
        "- A.Baz\n"
        "\n"
        "FIX these declarations (rejected):\n"
        "- A.Bar: hypothesis h : x > 0 should be x ≥ 0\n"
        "- A.Qux: missing finiteness assumption\n"
        "\n"
        "Leave all other code untouched."
    )
    assert note == expected


# ---------------------------------------------------------------------------
# ship_issue — incomplete refusal; verify path; reject path (verbatim note)
# ---------------------------------------------------------------------------


def test_ship_refuses_incomplete_no_side_effect(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    # Only one of three decls reviewed → incomplete.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is False
    assert "reason" in out
    # NO committed verdict handler ran.
    assert calls == {}
    # The store is intact (the refusal is non-destructive).
    assert subdeck.issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]["verdict"] \
        == "verify"


def test_ship_refuses_when_a_decl_only_deferred(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Bar", "verify")
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "defer")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is False
    assert calls == {}  # a deferred decl keeps the issue from shipping


def test_ship_all_verified_routes_cmd_verify(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    for d in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(cfg, 22, f"Demo.Chapter14.{d}", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True
    assert out["outcome"] == "verified"
    # cmd_verify routed; cmd_reject NOT.
    assert "verify" in calls and "reject" not in calls
    assert calls["verify"].issue_num == 22
    assert calls["verify"].close is False  # deck keeps the issue OPEN
    # The store is cleared on a successful ship.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_any_rejected_routes_cmd_reject_verbatim(tmp_path, monkeypatch):
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Bar", "reject",
        note="hypothesis h : x > 0 should be x ≥ 0",
    )
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Baz", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True
    assert out["outcome"] == "shipped"
    # cmd_reject routed; cmd_verify NOT.
    assert "reject" in calls and "verify" not in calls
    assert calls["reject"].issue_num == 22
    assert calls["reject"].no_refine is False

    # The note reaching cmd_reject is the composed KEEP/FIX note, VERBATIM:
    note = calls["reject"].notes
    assert "Per-declaration review of issue #22." in note
    assert "KEEP these declarations exactly as written" in note
    assert "- Demo.Chapter14.Baz" in note  # kept
    assert "- Demo.Chapter14.Foo" in note  # kept
    assert "FIX these declarations (rejected):" in note
    assert (
        "- Demo.Chapter14.Bar: hypothesis h : x > 0 should be x ≥ 0" in note
    )
    assert "Leave all other code untouched." in note
    # The exact composite is what ship sent (no Claude in the loop) — the
    # whole note equals compose_ship_note over the reviewed summary.
    expected_note = subdeck.compose_ship_note(22, {
        "kept": ["Demo.Chapter14.Baz", "Demo.Chapter14.Foo"],
        "rejected": [{
            "name": "Demo.Chapter14.Bar",
            "note": "hypothesis h : x > 0 should be x ≥ 0",
        }],
        "deferred": [],
    })
    assert note == expected_note
    # The store is cleared on a successful ship.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_advances_to_next_ready_issue(tmp_path, monkeypatch):
    """After shipping, advanced_to_issue is the next ready issue in the
    chapter (reusing the queue's readiness)."""
    cfg = make_cfg(tmp_path)
    # Each issue's decl lives in its OWN namespace (FooNs/BarNs/BazNs) so the
    # reconciliation's namespace surfacing never pulls another issue's decl into
    # issue 22's subdeck — this test exercises ship → advanced_to_issue (queue
    # readiness), not surfacing, so #22 must stay a single-card complete subdeck.
    install_snapshot(cfg, make_snapshot([
        _decl("Demo.FooNs.Foo", kind="def", value_pp="x"),
        _decl("Demo.BarNs.Bar", kind="def", value_pp="y"),
        _decl("Demo.BazNs.Baz", kind="def", value_pp="0"),
    ]))
    patch_bulk(monkeypatch, {
        22: {"title": "#22", "state": "OPEN",
             "body": _bundle_body("Foo"), "labels": set()},
        23: {"title": "#23", "state": "OPEN",
             "body": _bundle_body("Bar"), "labels": set()},
        24: {"title": "#24", "state": "OPEN",
             "body": _bundle_body("Baz"), "labels": set()},
    })
    patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.FooNs.Foo", "verify")

    out = subdeck.ship_issue(cfg, 22)
    assert out["ok"] is True
    # Some other ready issue in the chapter (23 or 24), never 22 itself.
    assert out["advanced_to_issue"] in (23, 24)


# ---------------------------------------------------------------------------
# Server — the three endpoints (binding, GET-is-pure, token gate)
# ---------------------------------------------------------------------------


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    """A DeckServer on an ephemeral 127.0.0.1 port over the bundle cfg (issue
    22 bundles Foo/Bar/Baz). Yields (server, base_url, token, cfg)."""
    cfg = make_bundle_cfg(tmp_path, monkeypatch)
    server = deck_server.make_server(cfg, port=0, default_chapter=14)
    host, port = server.server_address[:2]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://{host}:{port}", server.token, cfg
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


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


def test_get_subdeck_is_pure_no_verdict(running_server, monkeypatch):
    server, base, _token, _cfg = running_server
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("GET subdeck must not verify"),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: pytest.fail("GET subdeck must not reject"),
    )
    status, body = _get(base + "/api/subdeck/22")
    assert status == 200
    payload = json.loads(body)
    assert payload["issue"] == 22
    assert payload["card_tier"] == "T0"
    assert {c["name"] for c in payload["cards"]} == {
        "Demo.Chapter14.Foo", "Demo.Chapter14.Bar", "Demo.Chapter14.Baz",
    }
    assert payload["progress"] == {"reviewed": 0, "total": 3}
    assert payload["complete"] is False


def test_decl_verdict_endpoint_records_locally(running_server, monkeypatch):
    server, base, token, cfg = running_server
    # A decl-verdict must NEVER route to the committed handlers.
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("decl-verdict must not verify"),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: pytest.fail("decl-verdict must not reject"),
    )
    status, body = _post(
        base + "/api/decl-verdict",
        {"issue": 22, "decl": "Demo.Chapter14.Foo", "verdict": "verify"},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["progress"] == {"reviewed": 1, "total": 3}
    assert payload["complete"] is False
    assert payload["next_decl"] is not None
    assert payload["next_decl"]["verdict"] is None
    # Recorded to the LOCAL sidecar.
    assert subdeck.issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]["verdict"] \
        == "verify"


def test_decl_verdict_reject_without_note_is_400(running_server):
    _server, base, token, _cfg = running_server
    status, _ = _post(
        base + "/api/decl-verdict",
        {"issue": 22, "decl": "Demo.Chapter14.Bar", "verdict": "reject"},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 400


def test_decl_verdict_without_token_is_403(running_server):
    _server, base, _token, cfg = running_server
    status, body = _post(
        base + "/api/decl-verdict",
        {"issue": 22, "decl": "Demo.Chapter14.Foo", "verdict": "verify"},
    )
    assert status == 403
    assert json.loads(body)["ok"] is False
    # Nothing recorded — the token gate fired before any write.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_endpoint_requires_token(running_server):
    _server, base, _token, _cfg = running_server
    status, _ = _post(base + "/api/ship", {"issue": 22})
    assert status == 403


def test_ship_endpoint_routes_through_verdict_path(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    for d in ("Foo", "Bar", "Baz"):
        subdeck.record_decl_verdict(cfg, 22, f"Demo.Chapter14.{d}", "verify")

    status, body = _post(
        base + "/api/ship", {"issue": 22},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["outcome"] == "verified"
    # Routed through the committed cmd_verify; cmd_reject NOT.
    assert "verify" in calls and "reject" not in calls
    assert calls["verify"].issue_num == 22
    # The store is cleared on a successful ship.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_endpoint_incomplete_returns_ok_false(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.Foo", "verify")

    status, body = _post(
        base + "/api/ship", {"issue": 22},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200  # a graceful refusal, not an error
    payload = json.loads(body)
    assert payload["ok"] is False
    assert "reason" in payload
    assert calls == {}  # NO irreversible action fired
