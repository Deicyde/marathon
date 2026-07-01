"""marathon repair — broken-module discovery + the repair directive."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import marathon.repair as repair


def test_broken_modules_from_audit_failures(monkeypatch):
    snap = SimpleNamespace(failures=[
        "module GeometricAnalysis.LeeSM.Chapter16.IntegrationManifold failed to build; excluded",
        "module GeometricAnalysis.LeeSM.Chapter16.StokesTheorem failed to build; excluded",
        "module GeometricAnalysis.LeeSM.Chapter15.Foo failed to build; excluded",  # other chapter
        "lake env lean exited 1: some other run-level failure",
    ])
    monkeypatch.setattr("marathon.audit.engine.load_snapshot", lambda repo_dir: snap)
    cfg = SimpleNamespace(repo_dir=Path("/repo"))
    assert repair.broken_modules(cfg, 16) == [
        "GeometricAnalysis.LeeSM.Chapter16.IntegrationManifold",
        "GeometricAnalysis.LeeSM.Chapter16.StokesTheorem",
    ]


def test_broken_modules_none_when_no_snapshot(monkeypatch):
    monkeypatch.setattr("marathon.audit.engine.load_snapshot", lambda repo_dir: None)
    assert repair.broken_modules(SimpleNamespace(repo_dir=Path("/r")), 16) == []


def test_broken_modules_never_raises(monkeypatch):
    def boom(repo_dir):
        raise RuntimeError("snapshot unreadable")

    monkeypatch.setattr("marathon.audit.engine.load_snapshot", boom)
    assert repair.broken_modules(SimpleNamespace(repo_dir=Path("/r")), 16) == []


def test_repair_directive_carries_errors_and_forbids_weakening():
    d = repair._build_repair_directive([
        ("A.B.C", "error: unknown identifier `IsOrientedAtlasChart`"),
    ])
    assert "A.B.C" in d
    assert "unknown identifier `IsOrientedAtlasChart`" in d
    assert "PRESERVE the mathematical MEANING" in d
    assert "NEVER weaken a statement" in d
    assert "leave its proof body `sorry`" in d  # sorry-faithful over compiling-weakened


def test_chapter_of_target():
    assert repair._chapter_of_target(Path("GeometricAnalysis/LeeSM/Chapter16")) == 16
    assert repair._chapter_of_target(Path("x/Chapter7/")) == 7
    assert repair._chapter_of_target(Path("foo/bar")) is None
