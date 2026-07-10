"""Wide-angle deck review: source-order workspace, DAG, and batch verdicts."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from threading import Thread

import pytest

from marathon.audit.engine import save_snapshot
from marathon.audit.records import AuditSnapshot, DeclAudit
from marathon.deck import server as deck_server
from marathon.deck.subdeck import (
    issue_verdicts,
    record_decl_verdict,
    record_decl_verdict_batch,
)
from marathon.deck.workspace import (
    _cyclic_edge_keys,
    _source_line_index,
    build_project_graph,
    build_review_workspace,
)
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels


def _cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "Demo/Chapter14.lean"
    source.parent.mkdir(parents=True)
    source.write_text(
        "namespace Demo.Chapter14\n"
        "def Foo : Nat := 0\n"
        "theorem Bar : Foo = Foo := rfl\n"
        "end Demo.Chapter14\n"
    )
    # Registry order is deliberately the reverse of source/dependency order.
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="example/Demo",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={
            14: ChapterRegistry(
                chapter=14,
                entries=[(23, "Bar result"), (22, "Foo definition")],
            ),
        },
    )


def _install(cfg: ReviewConfig) -> None:
    snapshot = AuditSnapshot(
        repo_dir=str(cfg.repo_dir),
        modules=["Demo.Chapter14"],
        toolchain="leanprover/lean4:v4.0.0",
        lean_version="4.0.0",
        package_revs={},
        trusted_prefixes=["Mathlib", "Init"],
        created_at="2026-07-10T00:00:00+00:00",
        decls=[
            DeclAudit(
                name="Demo.Chapter14.Foo", kind="def", module="Demo.Chapter14",
                status="ok", type_pp="Nat", value_pp="0", cone=[],
                axioms=["propext"], has_sorry=False, tags=[], reason=None,
            ),
            DeclAudit(
                name="Demo.Chapter14.Bar", kind="theorem", module="Demo.Chapter14",
                status="ok", type_pp="Foo = Foo", value_pp=None,
                cone=["Demo.Chapter14.Foo"], axioms=["propext"],
                has_sorry=False, tags=[], reason=None,
            ),
        ],
        failures=[],
    )
    save_snapshot(snapshot, cfg.repo_dir)


def _patch_issues(monkeypatch):
    calls = []
    metadata = {
        22: {
            "title": "Definition Foo", "state": "OPEN", "labels": set(),
            "body": "### Lean signatures\n```lean\ndef Foo : Nat := 0\n```",
        },
        23: {
            "title": "Theorem Bar", "state": "OPEN", "labels": set(),
            "body": "### Lean signatures\n```lean\ntheorem Bar : Foo = Foo := rfl\n```",
        },
    }

    def fake(numbers, repo):
        calls.append((list(numbers), repo))
        return {number: metadata[number] for number in numbers if number in metadata}

    import marathon.review.github as github

    monkeypatch.setattr(github, "fetch_issues_bulk", fake)
    return calls


def _ready(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _install(cfg)
    calls = _patch_issues(monkeypatch)
    return cfg, calls


def test_workspace_is_source_ordered_and_bulk_loads_once(tmp_path, monkeypatch):
    cfg, calls = _ready(tmp_path, monkeypatch)

    payload = build_review_workspace(cfg, 14)

    assert len(calls) == 1
    assert payload["chapter"] == 14
    assert payload["progress"] == {
        "reviewed": 0, "total": 2, "verified": 0,
        "rejected": 0, "deferred": 0,
    }
    assert [file["path"] for file in payload["files"]] == ["Demo/Chapter14.lean"]
    declarations = payload["files"][0]["declarations"]
    assert [item["name"] for item in declarations] == [
        "Demo.Chapter14.Foo", "Demo.Chapter14.Bar",
    ]
    assert [item["source_line"] for item in declarations] == [2, 3]
    # The source order beats the deliberately-reversed registry order.
    assert [item["issue"] for item in declarations] == [22, 23]


def test_workspace_pauses_cross_issue_dependents_after_reject(tmp_path, monkeypatch):
    cfg, _calls = _ready(tmp_path, monkeypatch)
    record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Foo", "reject", "Foo has the wrong definition"
    )

    payload = build_review_workspace(cfg, 14)
    declarations = {
        item["name"]: item
        for file in payload["files"]
        for item in file["declarations"]
    }
    assert declarations["Demo.Chapter14.Foo"]["verdict"] == "rejected"
    assert declarations["Demo.Chapter14.Bar"]["blocked_by"] == [
        "Demo.Chapter14.Foo"
    ]


def test_project_graph_connects_issues_declarations_and_kernel(tmp_path, monkeypatch):
    cfg, calls = _ready(tmp_path, monkeypatch)

    graph = build_project_graph(cfg)

    assert len(calls) == 1
    node_ids = {node["id"] for node in graph["nodes"]}
    assert {
        "issue:22", "issue:23", "decl:Demo.Chapter14.Foo",
        "decl:Demo.Chapter14.Bar",
    } <= node_ids
    edges = {
        (edge["source"], edge["target"], edge["kind"])
        for edge in graph["edges"]
    }
    assert ("issue:22", "issue:23", "issue-dependency") in edges
    assert (
        "decl:Demo.Chapter14.Foo", "decl:Demo.Chapter14.Bar",
        "declaration-dependency",
    ) in edges
    assert ("issue:22", "decl:Demo.Chapter14.Foo", "contains") in edges
    assert graph["counts"]["by_type"] == {"declaration": 2, "issue": 2}
    assert graph["audit"]["status"] == "fresh"
    assert graph["unresolved_edges"] == []
    assert graph["cyclic_edges"] == []


def test_graph_reports_cycles_unreviewed_targets_and_changed_impact(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)

    def target(name, issue, kernel, *, changed=False):
        return {
            "name": name, "issue": issue, "chapter": 14,
            "source_file": "Demo/Chapter14.lean", "source_line": issue,
            "statement_pp": name, "source_block": name, "kind": "def",
            "verdict": None, "tier": "T1", "build_status": "ok",
            "blocked_by": [], "kernel": [{"name": item} for item in kernel],
            "changed_since_review": changed,
            "dependency_invalidated": False,
        }

    catalog = {
        "repo": cfg.github_repo, "chapters": [14], "errors": [],
        "audit": {"status": "fresh", "created_at": None,
                  "source_revision": None, "failures": []},
        "issues": [
            {"id": 22, "title": "A", "chapter": 14, "tier": "T1",
             "status": "ready", "build_status": "ok", "permalink": None,
             "blocked_reason": None, "graph_deps": [23]},
            {"id": 23, "title": "B", "chapter": 14, "tier": "T1",
             "status": "ready", "build_status": "ok", "permalink": None,
             "blocked_reason": None, "graph_deps": [22]},
        ],
        "targets": [
            target("Demo.A", 22, ["Demo.B", "Demo.Helper"], changed=True),
            target("Demo.B", 23, ["Demo.A"]),
        ],
    }
    import marathon.deck.definitions as definitions
    import marathon.deck.workspace as workspace

    monkeypatch.setattr(workspace, "_catalog", lambda _cfg, _chapter: catalog)
    monkeypatch.setattr(
        definitions, "_current_source_index",
        lambda _repo: {"Demo.A": [{}], "Demo.B": [{}], "Demo.Helper": [{}]},
    )

    first = build_project_graph(cfg)
    second = build_project_graph(cfg)
    assert first["edges"] == second["edges"]
    assert first["unresolved_edges"] == [{
        "source": "decl:Demo.Helper", "target": "decl:Demo.A",
        "kind": "declaration-dependency", "dependency": "Demo.Helper",
        "status": "unreviewed-target",
        "reason": "current project dependency has no registered review target",
    }]
    assert len(first["cyclic_edges"]) == 4  # two issue + two declaration edges
    nodes = {node["id"]: node for node in first["nodes"]}
    assert nodes["decl:Demo.A"]["impact"] == "changed"
    assert nodes["decl:Demo.B"]["impact"] == "downstream"
    assert first["counts"]["unresolved_edges"] == 1
    assert first["counts"]["cyclic_edges"] == 4


def test_cycle_detection_handles_a_deep_project_graph_without_recursion():
    edges = [
        {"source": f"decl:D{i}", "target": f"decl:D{i + 1}",
         "kind": "declaration-dependency"}
        for i in range(1500)
    ]
    assert _cyclic_edge_keys(edges) == set()
    edges.append({
        "source": "decl:D1500", "target": "decl:D0",
        "kind": "declaration-dependency",
    })
    assert len(_cyclic_edge_keys(edges)) == 1501


def test_graph_degrades_honestly_without_audit_snapshot(tmp_path, monkeypatch):
    cfg, _calls = _ready(tmp_path, monkeypatch)
    (cfg.repo_dir / ".marathon/audit/latest.json").unlink()

    graph = build_project_graph(cfg)

    assert graph["audit"]["status"] == "missing"
    declaration_nodes = [
        node for node in graph["nodes"] if node["type"] == "declaration"
    ]
    assert declaration_nodes
    assert {node["build_status"] for node in declaration_nodes} == {"unknown"}


def test_source_line_index_refuses_traversal_and_symlink_escape(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.lean"
    outside.write_text("def Secret : Nat := 1\n")
    (repo / "escape.lean").symlink_to(outside)

    indexed = _source_line_index(repo, ["../outside.lean", "escape.lean"])

    assert indexed == {"../outside.lean": {}, "escape.lean": {}}


def test_batch_store_is_atomic_and_rejects_duplicate_targets(tmp_path, monkeypatch):
    cfg, _calls = _ready(tmp_path, monkeypatch)
    stored = record_decl_verdict_batch(cfg, [
        {"issue": 22, "decl": "Demo.Chapter14.Foo", "verdict": "verify"},
        {"issue": 23, "decl": "Demo.Chapter14.Bar", "verdict": "defer"},
    ])
    assert len(stored) == 2
    assert issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]["verdict"] == "verify"
    assert issue_verdicts(cfg, 23)["Demo.Chapter14.Bar"]["verdict"] == "defer"

    before = json.loads((cfg.review_dir / "deck-subdecks.json").read_text())
    with pytest.raises(ValueError, match="duplicate declaration target"):
        record_decl_verdict_batch(cfg, [
            {"issue": 22, "decl": "Demo.Chapter14.Foo", "verdict": "verify"},
            {"issue": 22, "decl": "Demo.Chapter14.Foo", "verdict": "defer"},
        ])
    assert json.loads((cfg.review_dir / "deck-subdecks.json").read_text()) == before


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _post(url, body, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers[deck_server.TOKEN_HEADER] = token
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def test_workspace_routes_and_batch_dependency_guard(tmp_path, monkeypatch):
    cfg, _calls = _ready(tmp_path, monkeypatch)
    record_decl_verdict(
        cfg, 22, "Demo.Chapter14.Foo", "reject", "repair Foo first"
    )
    server = deck_server.make_server(cfg, port=0, default_chapter=14)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, workspace = _get(base + "/api/review-workspace?chapter=14")
        assert status == 200 and workspace["chapter"] == 14
        status, source = _get(
            base + "/api/review-source?path=Demo%2FChapter14.lean"
        )
        assert status == 200 and "theorem Bar" in source["source"]
        status, _error = _get(base + "/api/review-source?path=..%2Foutside.lean")
        assert status == 400
        status, graph = _get(base + "/api/project-graph")
        assert status == 200 and graph["counts"]["nodes"] == 4

        # Token guard applies to batch local writes too.
        status, _payload = _post(base + "/api/batch-decl-verdict", {
            "items": [{"issue": 23, "decl": "Demo.Chapter14.Bar", "verdict": "verify"}]
        })
        assert status == 403

        # KEEP/FIX are always per-declaration human decisions; the batch seam
        # is fail-closed to DEFER even before dependency validation.
        status, payload = _post(
            base + "/api/batch-decl-verdict",
            {"items": [{
                "issue": 23, "decl": "Demo.Chapter14.Bar", "verdict": "verify",
            }]},
            server.token,
        )
        assert status == 400
        assert "only 'defer'" in payload["error"]
        assert issue_verdicts(cfg, 23) == {}

        # Defer is intentionally still available as a non-decision.
        status, payload = _post(
            base + "/api/batch-decl-verdict",
            {"items": [{
                "issue": 23, "decl": "Demo.Chapter14.Bar", "verdict": "defer",
            }]},
            server.token,
        )
        assert status == 200 and payload["count"] == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
