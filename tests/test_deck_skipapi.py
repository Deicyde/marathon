"""Tests for the SKIP-API toggle (marathon.deck.subdeck + server).

Contract under test (the deck-features task spec + CONTRACT ADDITIONS):

* ``build_subdeck(cfg, issue, skip_api=True)`` — ``cards`` holds ONLY the
  MAIN-result decls; ``auto_approved`` holds the easy-API DeclCards; progress
  / complete count over MAIN only. ``skip_api=False`` keeps the current
  behavior (all decls in ``cards``, ``auto_approved=[]``). PURE — assembling
  the view (incl. the auto_approved LIST) writes NOTHING and fires no verdict;
* ``ship_issue(cfg, issue, skip_api=True)`` — on a complete non-partial review,
  every API decl WITHOUT an explicit human verdict is AUTO-APPROVED (folded
  into KEEP). A partial reject suppresses auto-approval and leaves hidden API
  declarations in WAIT. A human's explicit REJECT of an API decl overrides →
  it stays MAIN and goes to FIX. Auto-approval is realized ONLY at this
  explicit ship — NEVER on a GET or in decl-verdict;
* the server — GET /api/subdeck?skip_api=1 is a PURE read (no store write, no
  cmd_*); POST /api/ship threads skip_api from the body.

Fully offline: gh (fetch_issues_bulk) and the committed verdict handlers
(cmd_verify / cmd_reject) are monkeypatched. The HTTP layer is driven through a
threaded server on an ephemeral 127.0.0.1 port via urllib.
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
# Fixtures / builders
# ---------------------------------------------------------------------------


def make_cfg(tmp_path) -> ReviewConfig:
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
                entries=[(22, "Headline"), (23, "Other")],
            ),
        },
    )


def _decl(name, *, kind="theorem", type_pp="Type", axioms=("propext",),
          tags=()) -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module="Demo.Chapter14", status="ok",
        type_pp=type_pp, value_pp=None, cone=[], axioms=list(axioms),
        has_sorry=False, tags=list(tags), reason=None,
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
    lines = "\n".join(f"def {d} : T := ?" for d in decls)
    return f"### Lean signatures\n\n```lean\n{lines}\n```"


# Issue 22 bundles a MAIN headline (Theorem, T1, textbook name), an easy-API
# instance, and an easy-API `_apply` projection lemma — all at the T1 floor so
# they are eligible. The headline is the worst-tier decl; here all are T1 so
# the headline is the first by name. To make the headline deterministic we use
# a name that sorts first AND is clearly main.
def make_mix_cfg(tmp_path, monkeypatch, *, issue=22):
    cfg = make_cfg(tmp_path)
    install_snapshot(cfg, make_snapshot([
        # MAIN textbook result (the headline).
        _decl("Demo.Chapter14.AbelTheorem", kind="theorem", type_pp="P"),
        # easy-API instance.
        _decl("Demo.Chapter14.instFoo", kind="instance", type_pp="Inst"),
        # easy-API projection lemma (_apply).
        _decl("Demo.Chapter14.foo_apply", kind="theorem", type_pp="Q"),
    ]))
    patch_bulk(monkeypatch, {
        issue: {
            "title": f"#{issue} mix", "state": "OPEN",
            "body": _bundle_body("AbelTheorem", "instFoo", "foo_apply"),
            "labels": set(),
        },
    })
    return cfg


# ---------------------------------------------------------------------------
# build_subdeck — skip_api partition (PURE)
# ---------------------------------------------------------------------------


def test_skipapi_false_keeps_all_cards(tmp_path, monkeypatch):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    sd = subdeck.build_subdeck(cfg, 22)  # default skip_api=False
    assert {c["name"] for c in sd["cards"]} == {
        "Demo.Chapter14.AbelTheorem",
        "Demo.Chapter14.instFoo",
        "Demo.Chapter14.foo_apply",
    }
    assert sd["auto_approved"] == []
    assert sd["progress"] == {"reviewed": 0, "total": 3}


def test_skipapi_true_splits_main_and_api(tmp_path, monkeypatch):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    sd = subdeck.build_subdeck(cfg, 22, skip_api=True)
    # cards = MAIN only (the textbook headline); auto_approved = the two API.
    assert {c["name"] for c in sd["cards"]} == {"Demo.Chapter14.AbelTheorem"}
    assert {c["name"] for c in sd["auto_approved"]} == {
        "Demo.Chapter14.instFoo",
        "Demo.Chapter14.foo_apply",
    }
    # The auto_approved cards carry their api/api_reason.
    by_name = {c["name"]: c for c in sd["auto_approved"]}
    assert by_name["Demo.Chapter14.instFoo"]["api"] is True
    assert by_name["Demo.Chapter14.instFoo"]["api_reason"] == "instance"
    assert by_name["Demo.Chapter14.foo_apply"]["api"] is True
    assert "_apply" in by_name["Demo.Chapter14.foo_apply"]["api_reason"]
    # Progress is over MAIN only: one card, zero reviewed.
    assert sd["progress"] == {"reviewed": 0, "total": 1}
    assert sd["complete"] is False


def test_skipapi_complete_over_mains_only(tmp_path, monkeypatch):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    # Verify ONLY the main headline; leave the two API decls unreviewed.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.AbelTheorem", "verify")
    sd = subdeck.build_subdeck(cfg, 22, skip_api=True)
    assert sd["progress"] == {"reviewed": 1, "total": 1}
    assert sd["complete"] is True  # the API decls don't block completion


def test_skipapi_explicit_api_reject_moves_to_main(tmp_path, monkeypatch):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    # A human explicitly rejects an API decl → it becomes a MAIN card.
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.foo_apply", "reject", note="wrong simp form"
    )
    sd = subdeck.build_subdeck(cfg, 22, skip_api=True)
    assert "Demo.Chapter14.foo_apply" in {c["name"] for c in sd["cards"]}
    assert "Demo.Chapter14.foo_apply" not in {
        c["name"] for c in sd["auto_approved"]
    }
    # Only the still-easy instance remains auto-approvable.
    assert {c["name"] for c in sd["auto_approved"]} == {"Demo.Chapter14.instFoo"}


def test_skipapi_explicit_api_defer_stays_visible_and_blocks_ship(
    tmp_path, monkeypatch
):
    """A deferred API card cannot disappear and become auto-approved on reload."""
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.AbelTheorem", "verify"
    )
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.foo_apply", "defer"
    )

    sd = subdeck.build_subdeck(cfg, 22, skip_api=True)
    cards = {card["name"]: card for card in sd["cards"]}
    assert "Demo.Chapter14.foo_apply" in cards
    assert cards["Demo.Chapter14.foo_apply"]["verdict"] == "deferred"
    assert "Demo.Chapter14.foo_apply" not in {
        card["name"] for card in sd["auto_approved"]
    }

    out = subdeck.ship_issue(cfg, 22, skip_api=True)
    assert out["ok"] is False
    assert calls == {}


def test_skipapi_explicit_api_verify_is_main_not_double_autoapproved(
    tmp_path, monkeypatch
):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.AbelTheorem", "verify"
    )
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.foo_apply", "verify"
    )

    sd = subdeck.build_subdeck(cfg, 22, skip_api=True)
    assert "Demo.Chapter14.foo_apply" in {card["name"] for card in sd["cards"]}
    assert {card["name"] for card in sd["auto_approved"]} == {
        "Demo.Chapter14.instFoo"
    }
    assert sd["complete"] is True

    out = subdeck.ship_issue(cfg, 22, skip_api=True)
    assert out["ok"] is True and "verify" in calls
    assert out["auto_approved"] == ["Demo.Chapter14.instFoo"]


def test_partial_reject_carries_explicit_api_verify_with_fingerprint(
    tmp_path, monkeypatch
):
    """A kept API approval is invalidated when its audited meaning changes."""
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.AbelTheorem", "reject", note="bad theorem"
    )
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.instFoo", "verify"
    )

    out = subdeck.ship_issue(cfg, 22, skip_api=True)
    assert out["ok"] is True and out["partial"] is True
    carried = subdeck.issue_verdicts(cfg, 22)["Demo.Chapter14.instFoo"]
    assert carried["fingerprint"]

    install_snapshot(cfg, make_snapshot([
        _decl("Demo.Chapter14.AbelTheorem", kind="theorem", type_pp="P"),
        _decl("Demo.Chapter14.instFoo", kind="instance", type_pp="ChangedInst"),
        _decl("Demo.Chapter14.foo_apply", kind="theorem", type_pp="Q"),
    ]))
    refreshed = subdeck.build_subdeck(cfg, 22, skip_api=True)
    inst = next(
        card for card in refreshed["cards"]
        if card["name"] == "Demo.Chapter14.instFoo"
    )
    assert inst["verdict"] is None
    assert inst["changed_since_review"] is True
    assert inst["carried_over"] is False


def test_skipapi_build_is_pure_no_store_write(tmp_path, monkeypatch):
    """Assembling the skip_api view (including the auto_approved LIST) writes
    nothing — the store is untouched."""
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    subdeck.build_subdeck(cfg, 22, skip_api=True)
    subdeck.build_subdeck(cfg, 22, skip_api=True)
    # No verdicts were recorded by the assembly — the sidecar stays absent.
    assert subdeck.issue_verdicts(cfg, 22) == {}
    assert not (cfg.repo_dir / subdeck.SUBDECK_RELPATH).exists()


# ---------------------------------------------------------------------------
# ship_issue — auto-approval realized ONLY here
# ---------------------------------------------------------------------------


def test_ship_skipapi_autoapproves_unreviewed_api_into_keep(tmp_path, monkeypatch):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    # The human reviews only the MAIN headline (verify); API left unreviewed.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.AbelTheorem", "verify")

    out = subdeck.ship_issue(cfg, 22, skip_api=True)
    assert out["ok"] is True
    assert out["outcome"] == "verified"  # no reject → verify path
    # cmd_verify routed; cmd_reject NOT.
    assert "verify" in calls and "reject" not in calls
    # The two API decls were auto-approved and named in the result.
    assert set(out["auto_approved"]) == {
        "Demo.Chapter14.instFoo", "Demo.Chapter14.foo_apply",
    }
    assert "auto-approved 2 API declaration(s)" in out["message"]
    assert "Demo.Chapter14.instFoo" in out["message"]
    # The store is cleared on a successful ship.
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_skipapi_partial_reject_suppresses_autoapproval(
    tmp_path, monkeypatch
):
    """Hidden API declarations remain untouched after the first rejection."""
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    # Reject the MAIN headline; leave the two API decls unreviewed.
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.AbelTheorem", "reject", note="bad bound"
    )

    out = subdeck.ship_issue(cfg, 22, skip_api=True)
    assert out["ok"] is True
    assert out["outcome"] == "shipped"
    assert "reject" in calls and "verify" not in calls
    note = calls["reject"].notes
    # Partial review: hidden API decls are NOT silently folded into KEEP.
    assert "KEEP these declarations exactly as written" in note
    assert "- Demo.Chapter14.instFoo" not in note
    assert "- Demo.Chapter14.foo_apply" not in note
    # The rejected MAIN decl is in the FIX list, note verbatim.
    assert "FIX these declarations (rejected):" in note
    assert "- Demo.Chapter14.AbelTheorem: bad bound" in note
    assert out["partial"] is True
    assert out["auto_approved"] == []
    assert set(out["waiting"]) == {
        "Demo.Chapter14.instFoo", "Demo.Chapter14.foo_apply",
    }


def test_ship_skipapi_respects_explicit_api_reject(tmp_path, monkeypatch):
    """An explicitly-rejected API decl goes to FIX (NOT auto-approved)."""
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.AbelTheorem", "verify")
    subdeck.record_decl_verdict(
        cfg, 22, "Demo.Chapter14.foo_apply", "reject", note="wrong simp form"
    )
    # The other API decl (instFoo) stays unreviewed → WAIT, not auto-approved,
    # because the issue has entered a partial reject round.

    out = subdeck.ship_issue(cfg, 22, skip_api=True)
    assert out["ok"] is True
    assert out["outcome"] == "shipped"  # the explicit reject forces FIX
    assert "reject" in calls
    note = calls["reject"].notes
    # foo_apply rejected → FIX with its verbatim note.
    assert "- Demo.Chapter14.foo_apply: wrong simp form" in note
    # instFoo waits untouched; AbelTheorem verified → KEEP.
    assert "- Demo.Chapter14.instFoo" not in note
    assert "- Demo.Chapter14.AbelTheorem" in note
    assert out["auto_approved"] == []
    assert out["waiting"] == ["Demo.Chapter14.instFoo"]


def test_ship_without_skipapi_does_not_autoapprove(tmp_path, monkeypatch):
    """skip_api=False: the API decls must be reviewed like any other — an
    unreviewed API decl keeps the subdeck incomplete (no auto-approval)."""
    cfg = make_mix_cfg(tmp_path, monkeypatch)
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.AbelTheorem", "verify")

    out = subdeck.ship_issue(cfg, 22)  # skip_api=False
    assert out["ok"] is False  # two API decls still unreviewed
    assert calls == {}  # NO irreversible action fired


# ---------------------------------------------------------------------------
# Server — GET skip_api is pure; POST ship threads skip_api
# ---------------------------------------------------------------------------


@pytest.fixture
def running_server(tmp_path, monkeypatch):
    cfg = make_mix_cfg(tmp_path, monkeypatch)
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


def test_get_subdeck_skipapi_is_pure(running_server, monkeypatch):
    server, base, _token, cfg = running_server
    from marathon.review import review as review_mod

    monkeypatch.setattr(
        review_mod, "cmd_verify",
        lambda args: pytest.fail("GET subdeck must not verify"),
    )
    monkeypatch.setattr(
        review_mod, "cmd_reject",
        lambda args: pytest.fail("GET subdeck must not reject"),
    )
    status, body = _get(base + "/api/subdeck/22?skip_api=1")
    assert status == 200
    payload = json.loads(body)
    # cards = MAIN only; auto_approved lists the API decls.
    assert {c["name"] for c in payload["cards"]} == {"Demo.Chapter14.AbelTheorem"}
    assert {c["name"] for c in payload["auto_approved"]} == {
        "Demo.Chapter14.instFoo", "Demo.Chapter14.foo_apply",
    }
    assert payload["progress"] == {"reviewed": 0, "total": 1}
    # PURE: assembling the view wrote nothing to the store.
    assert subdeck.issue_verdicts(cfg, 22) == {}
    assert not (cfg.repo_dir / subdeck.SUBDECK_RELPATH).exists()


def test_get_subdeck_default_no_skipapi(running_server):
    _server, base, _token, _cfg = running_server
    status, body = _get(base + "/api/subdeck/22")
    assert status == 200
    payload = json.loads(body)
    # No skip_api → all decls in cards, auto_approved empty.
    assert len(payload["cards"]) == 3
    assert payload["auto_approved"] == []


def test_ship_endpoint_threads_skipapi_from_body(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    # Review only the MAIN headline; ship with skip_api in the body.
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.AbelTheorem", "verify")

    status, body = _post(
        base + "/api/ship", {"issue": 22, "skip_api": True},
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["outcome"] == "verified"
    assert set(payload["auto_approved"]) == {
        "Demo.Chapter14.instFoo", "Demo.Chapter14.foo_apply",
    }
    assert "verify" in calls and "reject" not in calls
    assert subdeck.issue_verdicts(cfg, 22) == {}


def test_ship_endpoint_without_skipapi_refuses_unreviewed_api(running_server, monkeypatch):
    server, base, token, cfg = running_server
    calls = patch_no_verdict_handlers(monkeypatch)
    subdeck.record_decl_verdict(cfg, 22, "Demo.Chapter14.AbelTheorem", "verify")

    status, body = _post(
        base + "/api/ship", {"issue": 22},  # no skip_api
        headers={deck_server.TOKEN_HEADER: token},
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is False  # API decls unreviewed → incomplete
    assert calls == {}
