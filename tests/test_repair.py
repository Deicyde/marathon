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


def test_invalidate_module_removes_cached_artifacts(tmp_path):
    """`_invalidate_module` deletes every cached lake artifact for the module so
    the next `lake build` cannot short-circuit on a stale olean."""
    module = "GeometricAnalysis.LeeSM.Chapter16.StokesTheorem"
    arts = repair._module_artifacts(tmp_path, module)
    for p in arts:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stale")
    # sanity: the olean and lake's content-hash trace are among them
    names = {p.name for p in arts}
    assert "StokesTheorem.olean" in names and "StokesTheorem.trace" in names
    repair._invalidate_module(tmp_path, module)
    assert not any(p.exists() for p in arts)


def test_invalidate_module_never_raises_when_absent(tmp_path):
    """Best-effort: invalidating a module with no cached artifacts is a no-op."""
    repair._invalidate_module(tmp_path, "A.B.C")  # must not raise


def test_module_build_errors_forces_fresh_build_not_stale_olean(monkeypatch, tmp_path):
    """The diagnostic build must delete the stale olean BEFORE building, so a
    module that fails a fresh elaboration is reported broken rather than passed
    on its leftover trace-valid olean (the StokesTheorem false-OK)."""
    module = "GeometricAnalysis.LeeSM.Chapter16.StokesTheorem"
    olean = repair._module_artifacts(tmp_path, module)[0]
    olean.parent.mkdir(parents=True, exist_ok=True)
    olean.write_text("stale-but-trace-valid")

    seen = {}

    def fake_build(repo_dir, timeout, targets=None):
        from types import SimpleNamespace
        # capture whether the stale olean was invalidated before the build ran
        seen["olean_present_at_build"] = olean.exists()
        return SimpleNamespace(ok=False, log_tail="error: unknown identifier `IsOrientedAtlasChart`")

    monkeypatch.setattr("marathon.post_pipeline.run_lake_build", fake_build)
    out = repair._module_build_errors(tmp_path, module, timeout=60)
    assert seen["olean_present_at_build"] is False        # invalidated first
    assert "unknown identifier" in out                    # real error surfaced
    assert not out.startswith(repair._ALREADY_BUILDS)     # NOT a false green


def test_repair_exits_early_when_everything_already_builds(monkeypatch, tmp_path, capsys):
    """Stale-snapshot re-dispatch: when the harvest build shows every module
    already green, the repair exits success WITHOUT calling Aristotle."""
    import asyncio
    from types import SimpleNamespace

    called = {}
    monkeypatch.setattr(
        repair, "_module_build_errors",
        lambda repo, m, timeout=1800: repair._ALREADY_BUILDS + " — stale)",
    )

    async def boom(args):
        called["refine"] = True

    monkeypatch.setattr("marathon.refine.refine_command", boom)
    args = SimpleNamespace(
        repo_dir=tmp_path, target=tmp_path / "Chapter16",
        modules=["A.Chapter16.M1", "A.Chapter16.M2"], file=None,
        build_timeout=60, max_iterations=0, skeleton=False,
    )
    asyncio.run(repair._run_repair(args))
    assert "refine" not in called          # Aristotle never dispatched
    assert "already build" in capsys.readouterr().out


def test_repair_proceeds_when_any_module_is_broken(monkeypatch, tmp_path):
    """One green + one broken → the repair still dispatches (partial staleness)."""
    import asyncio
    from types import SimpleNamespace

    called = {}
    errs = {"A.Chapter16.M1": repair._ALREADY_BUILDS + " — stale)",
            "A.Chapter16.M2": "error: unknown identifier foo"}
    monkeypatch.setattr(
        repair, "_module_build_errors",
        lambda repo, m, timeout=1800: errs[m],
    )

    async def fake_refine(args):
        called["directive"] = args.focus_directive

    monkeypatch.setattr("marathon.refine.refine_command", fake_refine)
    args = SimpleNamespace(
        repo_dir=tmp_path, target=tmp_path / "Chapter16",
        modules=["A.Chapter16.M1", "A.Chapter16.M2"], file=None,
        build_timeout=60, max_iterations=0, skeleton=False,
    )
    asyncio.run(repair._run_repair(args))
    assert "unknown identifier foo" in called["directive"]


def test_prepare_branch_continues_remote_for_repairs(monkeypatch, tmp_path):
    """continue_remote_branch=True resets onto origin/<branch> when it exists
    (stacked partial progress preserved); plain runs reset onto origin/<base>."""
    from marathon.post_pipeline import prepare_auto_pr_branch

    calls = []

    def fake_run(cmd, cwd=None, capture_output=True, text=True, check=False):
        from types import SimpleNamespace
        calls.append(list(cmd))
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr("marathon.post_pipeline.subprocess.run", fake_run)

    ok, branch, msg = prepare_auto_pr_branch(
        tmp_path, "Chapter16", None, base="main", kind="repair",
        continue_remote_branch=True,
    )
    assert ok and branch == "marathon/repair-c16"
    checkout = [c for c in calls if c[:2] == ["git", "checkout"]][0]
    assert checkout[-1] == "origin/marathon/repair-c16"   # stacked head
    assert "reset to origin/marathon/repair-c16" in msg

    calls.clear()
    ok, branch, msg = prepare_auto_pr_branch(
        tmp_path, "Chapter15", 170, base="main", kind="refine",
    )
    assert ok
    checkout = [c for c in calls if c[:2] == ["git", "checkout"]][0]
    assert checkout[-1] == "origin/main"                  # refine unchanged
