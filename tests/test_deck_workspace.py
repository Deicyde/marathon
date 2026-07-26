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


def _install(cfg: ReviewConfig, *, revision: str | None = None) -> None:
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
        source_revision=revision,
        module_revisions=(
            {"Demo.Chapter14": revision} if revision is not None else {}
        ),
        attempted_modules=["Demo.Chapter14"],
    )
    save_snapshot(snapshot, cfg.repo_dir)


def _decl(name: str, cone: list[str], *, kind: str = "def") -> DeclAudit:
    return DeclAudit(
        name=name, kind=kind, module="Demo.Chapter14", status="ok",
        type_pp=name, value_pp=("0" if kind == "def" else None), cone=cone,
        axioms=["propext"], has_sorry=False, tags=[], reason=None,
    )


def _snapshot(
    cfg: ReviewConfig, decls: list[DeclAudit], *, revision: str | None = None
) -> AuditSnapshot:
    return AuditSnapshot(
        repo_dir=str(cfg.repo_dir), modules=["Demo.Chapter14"],
        toolchain="leanprover/lean4:v4.0.0", lean_version="4.0.0",
        package_revs={}, trusted_prefixes=["Mathlib", "Init"],
        created_at="2026-07-10T00:00:00+00:00", decls=decls, failures=[],
        source_revision=revision,
        module_revisions=(
            {"Demo.Chapter14": revision} if revision is not None else {}
        ),
        attempted_modules=["Demo.Chapter14"],
    )


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
    assert graph["topology"] == {
        "status": "fresh",
        "available": True,
        "source_revision": None,
        "current_revision": None,
        "authoritative": False,
        "notice": (
            "Dependency topology comes from the current audited source. "
            "Exploratory only: dependency topology never affects trust, build "
            "status, review readiness, or dependency gating."
        ),
        "targets": 2,
        "resolved_targets": 2,
        "declaration_edges": 1,
        "issue_edges": 1,
    }
    for edge in graph["edges"]:
        if edge["kind"] != "contains":
            assert edge["topology_status"] == "fresh"
            assert edge["freshness"] == "fresh"
            assert edge["authoritative"] is False
    assert graph["unresolved_edges"] == []
    assert graph["cyclic_edges"] == []


def test_stale_topology_remains_explorable_but_not_review_evidence(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    _install(cfg, revision="old-revision")
    _patch_issues(monkeypatch)
    import marathon.audit.engine as audit_engine

    monkeypatch.setattr(
        audit_engine, "source_state", lambda _repo: ("current-revision", True)
    )

    graph = build_project_graph(cfg)

    assert graph["audit"]["status"] == "stale"
    assert graph["topology"]["status"] == "stale"
    assert graph["topology"]["available"] is True
    assert graph["topology"]["source_revision"] == "old-revision"
    assert graph["topology"]["current_revision"] == "current-revision"
    assert graph["topology"]["authoritative"] is False
    assert "may differ from the current code" in graph["topology"]["notice"]
    declarations = [
        node for node in graph["nodes"] if node["type"] == "declaration"
    ]
    # The validated snapshot still fails closed for every review signal.
    assert {node["build_status"] for node in declarations} == {"stale"}
    assert {node["tier"] for node in declarations} == {"-"}
    dependency_edges = {
        (edge["source"], edge["target"], edge["kind"]): edge
        for edge in graph["edges"] if edge["kind"] != "contains"
    }
    declaration_key = (
        "decl:Foo", "decl:Bar", "declaration-dependency",
    )
    issue_key = ("issue:22", "issue:23", "issue-dependency")
    assert declaration_key in dependency_edges
    assert issue_key in dependency_edges
    for edge in dependency_edges.values():
        assert edge["topology_status"] == "stale"
        assert edge["freshness"] == "stale"
        assert edge["authoritative"] is False


def test_graph_projects_nearest_targets_and_rolls_edges_up_without_gating(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)

    def target(name, issue):
        return {
            "name": name, "issue": issue, "chapter": 14,
            "source_file": "Demo/Chapter14.lean", "source_line": issue,
            "statement_pp": name, "source_block": name, "kind": "def",
            "verdict": None, "tier": "T1", "build_status": "ok",
            "blocked_by": [], "kernel": [], "changed_since_review": False,
            "dependency_invalidated": False,
        }

    names = [("Demo.A", 10), ("Demo.B", 20), ("Demo.C", 30), ("Demo.D", 40)]
    catalog = {
        "repo": cfg.github_repo, "chapters": [14], "errors": [],
        "audit": {"status": "fresh", "created_at": None,
                  "source_revision": None, "failures": []},
        "issues": [
            {"id": issue, "title": name, "chapter": 14, "tier": "T1",
             "status": "ready", "build_status": "ok", "permalink": None,
             "blocked_reason": None, "graph_deps": []}
            for name, issue in names
        ],
        "targets": [target(name, issue) for name, issue in names],
    }
    topology_snapshot = _snapshot(cfg, [
        _decl("Demo.A", []),
        _decl("Demo.Helper", ["Demo.A"]),
        _decl("Demo.B", ["Demo.Helper"]),
        _decl("Demo.C", ["Demo.B"]),
        _decl("Demo.D", ["Demo.A"]),
    ])
    import marathon.audit.engine as audit_engine
    import marathon.deck.workspace as workspace

    monkeypatch.setattr(workspace, "_catalog", lambda _cfg, _chapter: catalog)
    monkeypatch.setattr(
        audit_engine, "load_snapshot", lambda *_args, **_kwargs: topology_snapshot
    )
    monkeypatch.setattr(audit_engine, "source_state", lambda _repo: (None, None))

    graph = build_project_graph(cfg)

    declaration_edges = {
        (edge["source"], edge["target"])
        for edge in graph["edges"]
        if edge["kind"] == "declaration-dependency"
    }
    assert declaration_edges == {
        ("decl:Demo.A", "decl:Demo.B"),
        ("decl:Demo.B", "decl:Demo.C"),
        ("decl:Demo.A", "decl:Demo.D"),
    }
    assert ("decl:Demo.A", "decl:Demo.C") not in declaration_edges
    issue_edges = {
        (edge["source"], edge["target"])
        for edge in graph["edges"] if edge["kind"] == "issue-dependency"
    }
    assert issue_edges == {
        ("issue:10", "issue:20"),
        ("issue:20", "issue:30"),
        ("issue:10", "issue:40"),
    }
    # Exploratory roll-up is not allowed to rewrite readiness supplied by the
    # validated catalog.
    assert {node["status"] for node in graph["nodes"] if node["type"] == "issue"} == {
        "ready"
    }


def test_issue_rollup_uses_canonical_owner_instead_of_citation_product(
    tmp_path, monkeypatch,
):
    """Duplicate citations cannot fabricate issue edges or issue cycles."""
    cfg = _cfg(tmp_path)

    def target(name, issue, registry_order):
        return {
            "name": name, "issue": issue, "chapter": 14,
            "source_file": "Demo/Chapter14.lean", "source_line": issue,
            "statement_pp": name, "source_block": name, "kind": "def",
            "verdict": None, "tier": "T1", "build_status": "ok",
            "blocked_by": [], "kernel": [], "changed_since_review": False,
            "dependency_invalidated": False,
            "registry_order": registry_order,
        }

    catalog = {
        "repo": cfg.github_repo, "chapters": [14], "errors": [],
        "audit": {"status": "fresh", "created_at": None,
                  "source_revision": None, "failures": []},
        "issues": [
            {"id": issue, "title": str(issue), "chapter": 14, "tier": "T1",
             "status": "ready", "build_status": "ok", "permalink": None,
             "blocked_reason": None, "graph_deps": []}
            for issue in (10, 20, 30)
        ],
        # #30 cites both declarations, but their canonical registered owners
        # are #10 and #20 respectively.
        "targets": [
            target("Demo.A", 10, 0), target("Demo.A", 30, 2),
            target("Demo.B", 20, 1), target("Demo.B", 30, 2),
        ],
    }
    topology_snapshot = _snapshot(
        cfg, [_decl("Demo.A", []), _decl("Demo.B", ["Demo.A"])]
    )
    import marathon.audit.engine as audit_engine
    import marathon.deck.workspace as workspace

    monkeypatch.setattr(workspace, "_catalog", lambda _cfg, _chapter: catalog)
    monkeypatch.setattr(
        audit_engine, "load_snapshot", lambda *_args, **_kwargs: topology_snapshot
    )
    monkeypatch.setattr(audit_engine, "source_state", lambda _repo: (None, None))

    graph = build_project_graph(cfg)
    issue_edges = {
        (edge["source"], edge["target"])
        for edge in graph["edges"] if edge["kind"] == "issue-dependency"
    }
    assert issue_edges == {("issue:10", "issue:20")}
    assert not [edge for edge in graph["cyclic_edges"] if edge["kind"] == "issue-dependency"]


def test_graph_does_not_transfer_old_edges_to_a_renamed_target(
    tmp_path, monkeypatch
):
    cfg = _cfg(tmp_path)
    catalog = {
        "repo": cfg.github_repo, "chapters": [14], "errors": [],
        "audit": {"status": "stale", "created_at": None,
                  "source_revision": "old", "failures": ["stale"]},
        "issues": [
            {"id": 22, "title": "Renamed", "chapter": 14, "tier": "-",
             "status": "blocked", "build_status": "stale", "permalink": None,
             "blocked_reason": "stale audit", "graph_deps": []},
            {"id": 23, "title": "Dependent", "chapter": 14, "tier": "-",
             "status": "blocked", "build_status": "stale", "permalink": None,
             "blocked_reason": "stale audit", "graph_deps": []},
        ],
        "targets": [
            {"name": "Demo.RenamedA", "issue": 22, "chapter": 14,
             "source_file": "Demo/Chapter14.lean", "source_line": 2,
             "statement_pp": "Demo.RenamedA", "source_block": "Demo.RenamedA",
             "kind": "def", "verdict": None, "tier": "-",
             "build_status": "stale", "blocked_by": [], "kernel": [],
             "changed_since_review": False, "dependency_invalidated": False},
            {"name": "Demo.B", "issue": 23, "chapter": 14,
             "source_file": "Demo/Chapter14.lean", "source_line": 3,
             "statement_pp": "Demo.B", "source_block": "Demo.B", "kind": "def",
             "verdict": None, "tier": "-", "build_status": "stale",
             "blocked_by": [], "kernel": [], "changed_since_review": False,
             "dependency_invalidated": False},
        ],
    }
    old_snapshot = _snapshot(
        cfg, [_decl("Demo.A", []), _decl("Demo.B", ["Demo.A"])],
        revision="old",
    )
    import marathon.audit.engine as audit_engine
    import marathon.deck.workspace as workspace

    monkeypatch.setattr(workspace, "_catalog", lambda _cfg, _chapter: catalog)
    monkeypatch.setattr(
        audit_engine, "load_snapshot", lambda *_args, **_kwargs: old_snapshot
    )
    monkeypatch.setattr(audit_engine, "source_state", lambda _repo: ("new", True))

    graph = build_project_graph(cfg)

    assert graph["topology"]["resolved_targets"] == 1
    assert not [
        edge for edge in graph["edges"] if edge["kind"] != "contains"
    ]


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
    import marathon.audit.engine as audit_engine

    monkeypatch.setattr(workspace, "_catalog", lambda _cfg, _chapter: catalog)
    topology_snapshot = _snapshot(cfg, [
        _decl("Demo.A", ["Demo.B", "Demo.Helper"]),
        _decl("Demo.B", ["Demo.A"]),
        _decl("Demo.Helper", []),
    ])
    monkeypatch.setattr(
        audit_engine, "load_snapshot", lambda *_args, **_kwargs: topology_snapshot
    )
    monkeypatch.setattr(audit_engine, "source_state", lambda _repo: (None, None))
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
        "topology_status": "fresh", "freshness": "fresh",
        "authoritative": False,
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
    assert graph["topology"]["status"] == "unavailable"
    assert graph["topology"]["available"] is False
    assert graph["topology"]["source_revision"] is None
    assert graph["topology"]["current_revision"] is None
    assert graph["topology"]["authoritative"] is False
    assert "No audited dependency topology is available" in graph["topology"]["notice"]
    assert graph["topology"]["resolved_targets"] == 0
    assert graph["topology"]["declaration_edges"] == 0
    assert graph["topology"]["issue_edges"] == 0
    declaration_nodes = [
        node for node in graph["nodes"] if node["type"] == "declaration"
    ]
    assert declaration_nodes
    assert {node["build_status"] for node in declaration_nodes} == {"unknown"}
    assert {edge["kind"] for edge in graph["edges"]} == {"contains"}


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
    foo = issue_verdicts(cfg, 22)["Demo.Chapter14.Foo"]
    bar = issue_verdicts(cfg, 23)["Demo.Chapter14.Bar"]
    assert foo["verdict"] == "verify"
    assert bar["verdict"] == "defer"
    assert len(foo["fingerprint"]) == len(bar["fingerprint"]) == 64

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
        bar_target = next(
            target
            for file in workspace["files"]
            for target in file["declarations"]
            if target["name"] == "Demo.Chapter14.Bar"
        )
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
                "decl_fingerprint": bar_target["context_fingerprint"],
            }]},
            server.token,
        )
        assert status == 200 and payload["count"] == 1
        stored = issue_verdicts(cfg, 23)["Demo.Chapter14.Bar"]
        assert len(stored["fingerprint"]) == 64
        assert stored["fingerprint_version"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
