"""Offline tests for the phase-5 audit engine (Python side).

Everything here runs without a Lean toolchain: the committed golden file
``tests/golden/audit_fixture_output.txt`` (produced by the real script on
the fixture package) is the parse fixture, and every subprocess boundary
is monkeypatched.  The toolchain-gated round-trip lives in
``tests/test_audit_lean_script.py``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from marathon.audit import engine
from marathon.audit.lean_template import DEFAULT_TRUSTED_PREFIXES
from marathon.audit.records import AuditSnapshot, DeclAudit, fingerprint

TESTS_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = TESTS_DIR / "fixtures" / "audit_fixture"
GOLDEN_TEXT = (TESTS_DIR / "golden" / "audit_fixture_output.txt").read_text()

FIXTURE_MODULES = ["AuditFixture.Basic", "AuditFixture.Deception"]


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

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


def mk_snapshot(decls, *, repo_dir="/r", failures=(), **kw) -> AuditSnapshot:
    defaults = dict(
        repo_dir=repo_dir,
        modules=["Foo"],
        toolchain="leanprover/lean4:v4.28.0",
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
def fake_lake(monkeypatch):
    """Install a fake `lake` whose `subprocess.run` returns canned output.
    Returns the call-recording list."""

    def install(stdout=GOLDEN_TEXT, returncode=0, stderr="", exc=None):
        monkeypatch.setattr(engine, "_find_lake", lambda: "/fake/lake")
        calls: list[tuple[list, dict]] = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(
                cmd, returncode, stdout=stdout, stderr=stderr
            )

        monkeypatch.setattr(engine.subprocess, "run", fake_run)
        return calls

    return install


# ---------------------------------------------------------------------------
# Golden end-to-end
# ---------------------------------------------------------------------------

class TestGoldenEndToEnd:
    def test_snapshot_counts_and_context(self, fake_lake):
        fake_lake()
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture", timeout=5)
        assert snap.failures == []
        assert len(snap.decls) == 26
        assert snap.modules == FIXTURE_MODULES
        assert snap.toolchain == "leanprover/lean4:v4.28.0"
        assert snap.lean_version == "4.28.0"
        assert snap.package_revs == {}  # fixture manifest has no packages
        assert set(DEFAULT_TRUSTED_PREFIXES) <= set(snap.trusted_prefixes)
        assert all(d.status == "ok" for d in snap.decls)

    def test_spot_fields(self, fake_lake):
        fake_lake()
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture", timeout=5)
        by = snap.by_name()
        dbl = by["AuditFixture.double"]
        assert dbl.kind == "def"
        assert dbl.type_pp == "Nat → Nat"
        assert dbl.value_pp == "fun n => HAdd.hAdd n n"
        thm = by["AuditFixture.double_eq"]
        assert thm.kind == "theorem"
        assert thm.cone == ["AuditFixture.double"]
        assert thm.value_pp is None and thm.fingerprint_value is None
        sorried = by["AuditFixture.double_is_two_mul"]
        assert sorried.has_sorry is True
        assert sorried.axioms == ["sorryAx"]
        axuser = by["AuditFixture.le_double"]
        assert axuser.axioms == ["AuditFixture.double_growth"]
        assert axuser.has_sorry is False
        assert by["AuditFixture.instCollapsiblePUnit"].tags == [
            "trivial_instance"
        ]
        assert (
            "_private.AuditFixture.Basic.0.AuditFixture.hiddenHelper" in by
        )

    def test_fingerprints_match_pp_hashes(self, fake_lake):
        fake_lake()
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture", timeout=5)
        for d in snap.decls:
            assert d.fingerprint_type == hashlib.sha256(
                d.type_pp.encode()
            ).hexdigest()
            if d.value_pp is not None:
                assert d.fingerprint_value == hashlib.sha256(
                    d.value_pp.encode()
                ).hexdigest()

    def test_script_runs_outside_repo_with_repo_cwd(self, fake_lake):
        calls = fake_lake()
        engine.run_audit(FIXTURE_DIR, "AuditFixture", timeout=5)
        assert len(calls) == 1
        cmd, kwargs = calls[0]
        assert cmd[:3] == ["/fake/lake", "env", "lean"]
        script_path = Path(cmd[3])
        assert not str(script_path).startswith(str(FIXTURE_DIR.resolve()))
        assert kwargs["cwd"] == str(FIXTURE_DIR.resolve())
        assert kwargs["timeout"] == 5

    def test_snapshot_json_roundtrip(self, fake_lake):
        fake_lake()
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture", timeout=5)
        restored = AuditSnapshot.from_json(
            json.loads(json.dumps(snap.to_json()))
        )
        assert restored == snap


# ---------------------------------------------------------------------------
# Tolerant parsing
# ---------------------------------------------------------------------------

GOOD_LINE = "AUDIT|Foo.t|theorem|Foo|ok|TmF0|-|-|-|false|-|-"  # type "Nat"


def _wrap(*lines, done=None):
    body = [
        "AUDIT_BEGIN",
        "AUDIT_META|schema|1",
        "AUDIT_META|lean_version|4.28.0",
        *lines,
    ]
    if done is not None:
        body.append(f"AUDIT_DONE|{done}")
    return "\n".join(body) + "\n"


class TestTolerantParsing:
    def test_malformed_lines_become_failures_never_crash(self):
        text = _wrap(
            "random build noise",
            "warning: declaration uses sorry",
            GOOD_LINE,
            "AUDIT|bad|onlyfour|fields",  # wrong field count
            "AUDIT|Foo.x|theorem|Foo|ok|!!!|-|-|-|false|-|-",  # bad base64
            "AUDIT|Foo.y|gadget|Foo|ok|TmF0|-|-|-|false|-|-",  # bad kind
            "AUDIT|Foo.z|theorem|Foo|maybe|TmF0|-|-|-|false|-|-",  # bad status
            done=5,
        )
        parsed = engine.parse_audit_output(text)
        assert [d.name for d in parsed.decls] == ["Foo.t"]
        assert len(parsed.failures) == 4
        assert all("malformed" in f for f in parsed.failures)

    def test_unknown_decl_preserved_verbatim(self):
        reason_b64 = "ZWxhYm9yYXRpb24gZmFpbGVk"  # "elaboration failed"
        text = _wrap(
            f"AUDIT|Foo.u|def|Foo|unknown|-|-|-|-|-|-|{reason_b64}",
            done=1,
        )
        parsed = engine.parse_audit_output(text)
        assert parsed.failures == []
        (decl,) = parsed.decls
        assert decl.status == "unknown"
        assert decl.reason == "elaboration failed"
        assert decl.type_pp is None
        assert decl.fingerprint_type is None
        assert decl.fingerprint_value is None
        assert decl.has_sorry is None  # absence of evidence, not False

    def test_missing_trailer_is_truncation(self):
        parsed = engine.parse_audit_output(_wrap(GOOD_LINE))
        assert any("truncated" in f for f in parsed.failures)

    def test_inconsistent_trailer_is_truncation(self):
        parsed = engine.parse_audit_output(_wrap(GOOD_LINE, done=7))
        assert any("truncated" in f for f in parsed.failures)

    def test_duplicate_decl_keeps_first(self):
        other = GOOD_LINE.replace("TmF0", "Qm9vbA==")  # type "Bool"
        parsed = engine.parse_audit_output(_wrap(GOOD_LINE, other, done=2))
        assert [d.name for d in parsed.decls] == ["Foo.t"]
        assert parsed.decls[0].type_pp == "Nat"
        assert any("duplicate" in f for f in parsed.failures)

    def test_info_prefix_folding_on_first_line(self):
        text = "/tmp/s.lean:500:0: info: AUDIT_BEGIN\n" + _wrap(
            GOOD_LINE, done=1
        ).split("\n", 1)[1]
        parsed = engine.parse_audit_output(text)
        assert parsed.saw_contract
        assert [d.name for d in parsed.decls] == ["Foo.t"]
        assert parsed.failures == []

    def test_no_contract_lines_at_all(self):
        parsed = engine.parse_audit_output("error: something exploded\n")
        assert not parsed.saw_contract
        assert parsed.decls == []

    def test_schema_drift_flagged(self):
        text = "AUDIT_BEGIN\nAUDIT_META|schema|99\nAUDIT_DONE|0\n"
        parsed = engine.parse_audit_output(text)
        assert any("schema" in f for f in parsed.failures)


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

class TestFingerprints:
    def test_same_pp_same_hash(self):
        a = mk_decl(type_pp="∀ (n : Nat), Eq n n")
        b = mk_decl(type_pp="∀ (n : Nat), Eq n n")
        assert a.fingerprint_type == b.fingerprint_type
        assert a.fingerprint_type == fingerprint("∀ (n : Nat), Eq n n")

    def test_different_pp_different_hash(self):
        a = mk_decl(type_pp="Nat")
        b = mk_decl(type_pp="Nat ")  # whitespace is part of the string
        assert a.fingerprint_type != b.fingerprint_type

    def test_theorem_never_gets_value_fingerprint(self):
        # Even with a value_pp smuggled in, kind gates the value hash:
        # proof irrelevance — a theorem's meaning is its type.
        thm = mk_decl(kind="theorem", value_pp="some proof term")
        assert thm.fingerprint_value is None

    def test_def_like_kinds_get_value_fingerprint(self):
        for kind in ("def", "instance", "abbrev"):
            d = mk_decl(kind=kind, value_pp="fun n => n")
            assert d.fingerprint_value == fingerprint("fun n => n")

    def test_value_change_leaves_type_fingerprint_alone(self):
        a = mk_decl(kind="def", type_pp="Nat → Nat", value_pp="fun n => n")
        b = mk_decl(kind="def", type_pp="Nat → Nat", value_pp="fun n => 0")
        assert a.fingerprint_type == b.fingerprint_type
        assert a.fingerprint_value != b.fingerprint_value

    def test_json_roundtrip_recomputes_and_preserves(self):
        d = mk_decl(
            kind="def",
            type_pp="Nat →\n  Nat  ",  # embedded newline + trailing spaces
            value_pp="fun n =>\n  n",
        )
        payload = d.to_json()
        payload["fingerprint_type"] = "tampered"  # never trusted from disk
        payload["fingerprint_value"] = "tampered"
        restored = DeclAudit.from_json(payload)
        assert restored == d
        assert restored.fingerprint_type == d.fingerprint_type
        assert restored.fingerprint_value == d.fingerprint_value


# ---------------------------------------------------------------------------
# diff_snapshots
# ---------------------------------------------------------------------------

class TestDiffSnapshots:
    def test_every_change_class(self):
        old = mk_snapshot([
            mk_decl(name="Foo.keep", type_pp="A"),
            mk_decl(name="Foo.gone", type_pp="B"),
            mk_decl(name="Foo.tchange", type_pp="C"),
            mk_decl(name="Foo.vchange", kind="def", type_pp="D",
                    value_pp="fun x => x"),
            mk_decl(name="Foo.axchange", type_pp="E", axioms=["propext"]),
            mk_decl(name="Foo.lost", type_pp="F"),
        ])
        new = mk_snapshot([
            mk_decl(name="Foo.keep", type_pp="A"),
            mk_decl(name="Foo.fresh", type_pp="G"),
            mk_decl(name="Foo.tchange", type_pp="C'"),
            mk_decl(name="Foo.vchange", kind="def", type_pp="D",
                    value_pp="fun x => 0"),
            mk_decl(name="Foo.axchange", type_pp="E",
                    axioms=["propext", "sorryAx"], has_sorry=True),
            mk_decl(name="Foo.lost", status="unknown", type_pp=None,
                    has_sorry=None, reason="import failed"),
        ])
        diff = engine.diff_snapshots(old, new)
        assert diff["added"] == ["Foo.fresh"]
        assert diff["removed"] == ["Foo.gone"]
        assert diff["type_changed"] == ["Foo.tchange"]
        assert diff["value_changed"] == ["Foo.vchange"]
        assert diff["axioms_changed"] == ["Foo.axchange"]
        assert diff["status_changed"] == ["Foo.lost"]
        assert diff["warnings"] == []
        # Going unknown is a status change, never a phantom type/value/
        # axiom change (absence of evidence is not evidence of change).
        for key in ("type_changed", "value_changed", "axioms_changed"):
            assert "Foo.lost" not in diff[key]
        # And the untouched decl appears nowhere.
        for key in engine.DIFF_KEYS:
            assert "Foo.keep" not in diff[key]

    def test_cross_version_comparisons_flagged_not_trusted(self):
        old = mk_snapshot(
            [mk_decl()],
            toolchain="leanprover/lean4:v4.28.0",
            lean_version="4.28.0",
            package_revs={"mathlib": "aaa"},
        )
        new = mk_snapshot(
            [mk_decl()],
            toolchain="leanprover/lean4:v4.29.0",
            lean_version="4.29.0",
            package_revs={"mathlib": "bbb"},
        )
        diff = engine.diff_snapshots(old, new)
        assert any("toolchain" in w for w in diff["warnings"])
        assert any("mathlib" in w for w in diff["warnings"])

    def test_same_version_no_warnings(self):
        diff = engine.diff_snapshots(
            mk_snapshot([mk_decl()]), mk_snapshot([mk_decl()])
        )
        assert diff["warnings"] == []


# ---------------------------------------------------------------------------
# Degradation (honest absence)
# ---------------------------------------------------------------------------

class TestDegradation:
    def test_lake_missing(self, monkeypatch):
        monkeypatch.setattr(engine, "_find_lake", lambda: None)
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture")
        assert snap.decls == []
        assert any("lake not found" in f for f in snap.failures)
        # Context is still recorded: the snapshot is honest, not empty.
        assert snap.toolchain == "leanprover/lean4:v4.28.0"
        assert snap.modules == FIXTURE_MODULES

    def test_nonzero_exit_records_failure_keeps_partial(self, fake_lake):
        partial = "\n".join(GOLDEN_TEXT.splitlines()[:7])  # one AUDIT line
        fake_lake(stdout=partial, returncode=1, stderr="error: kaboom")
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture")
        assert any("exited 1" in f and "kaboom" in f for f in snap.failures)
        assert any("truncated" in f for f in snap.failures)
        assert [d.name for d in snap.decls] == ["AuditFixture.Collapsible"]

    def test_timeout(self, fake_lake):
        fake_lake(exc=subprocess.TimeoutExpired(cmd="lake", timeout=5))
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture", timeout=5)
        assert snap.decls == []
        assert any("timed out" in f for f in snap.failures)

    def test_zero_exit_empty_stdout_is_flagged(self, fake_lake):
        fake_lake(stdout="")
        snap = engine.run_audit(FIXTURE_DIR, "AuditFixture")
        assert snap.decls == []
        assert any("nothing audited" in f for f in snap.failures)

    def test_missing_target_folder(self, fake_lake):
        fake_lake()
        snap = engine.run_audit(FIXTURE_DIR, "NoSuchFolder")
        assert snap.decls == []
        assert any("not found" in f for f in snap.failures)
        assert any("no auditable" in f for f in snap.failures)


# ---------------------------------------------------------------------------
# Workspace derivation
# ---------------------------------------------------------------------------

class TestDeriveModules:
    def test_maps_lean_files_to_modules(self, tmp_path):
        (tmp_path / "Foo" / "Baz").mkdir(parents=True)
        (tmp_path / "Foo" / "Bar.lean").write_text("")
        (tmp_path / "Foo" / "Baz" / "Qux.lean").write_text("")
        mods, failures = engine.derive_modules(tmp_path, "Foo")
        assert mods == ["Foo.Bar", "Foo.Baz.Qux"]
        assert failures == []

    def test_unsafe_names_skipped_with_failure(self, tmp_path):
        (tmp_path / "Foo").mkdir()
        (tmp_path / "Foo" / "Good.lean").write_text("")
        (tmp_path / "Foo" / "bad-name.lean").write_text("")
        mods, failures = engine.derive_modules(tmp_path, "Foo")
        assert mods == ["Foo.Good"]
        assert len(failures) == 1 and "bad-name" in failures[0]

    def test_dot_dirs_skipped_silently(self, tmp_path):
        (tmp_path / "Foo" / ".lake").mkdir(parents=True)
        (tmp_path / "Foo" / "Good.lean").write_text("")
        (tmp_path / "Foo" / ".lake" / "Scratch.lean").write_text("")
        mods, failures = engine.derive_modules(tmp_path, "Foo")
        assert mods == ["Foo.Good"]
        assert failures == []

    def test_single_file_target(self, tmp_path):
        (tmp_path / "Foo").mkdir()
        (tmp_path / "Foo" / "Bar.lean").write_text("")
        mods, failures = engine.derive_modules(tmp_path, "Foo/Bar.lean")
        assert mods == ["Foo.Bar"]
        assert failures == []


class TestDeriveTrustedPrefixes:
    def test_packages_tree_and_requires(self, tmp_path):
        pkgs = tmp_path / ".lake" / "packages"
        # Root-module .lean file at the package top level.
        (pkgs / "mathlib").mkdir(parents=True)
        (pkgs / "mathlib" / "Mathlib.lean").write_text("")
        (pkgs / "mathlib" / "lakefile.lean").write_text("")  # noise
        # Camel-case root that title-casing a require name would miss.
        (pkgs / "proofwidgets").mkdir()
        (pkgs / "proofwidgets" / "ProofWidgets.lean").write_text("")
        # Module-root *directory* (no top-level root file).
        (pkgs / "widget" / "WidgetKit").mkdir(parents=True)
        (pkgs / "widget" / "WidgetKit" / "Basic.lean").write_text("")
        # Lowercase noise dir never becomes a prefix.
        (pkgs / "widget" / "scripts").mkdir()
        (pkgs / "widget" / "scripts" / "gen.lean").write_text("")
        (tmp_path / "lakefile.toml").write_text(
            'name = "demo"\n\n[[require]]\nname = "myDep"\n'
        )
        prefixes = engine.derive_trusted_prefixes(tmp_path)
        assert "Mathlib" in prefixes
        assert "ProofWidgets" in prefixes
        assert "WidgetKit" in prefixes
        assert "MyDep" in prefixes  # capitalized require-name variant
        assert "lakefile" not in prefixes
        assert "scripts" not in prefixes
        assert set(DEFAULT_TRUSTED_PREFIXES) <= set(prefixes)

    def test_lakefile_lean_requires(self, tmp_path):
        (tmp_path / "lakefile.lean").write_text(
            'import Lake\nopen Lake DSL\n\n'
            'require mathlib from git\n'
            '  "https://github.com/leanprover-community/mathlib4" @ "stable"\n'
            'require "leanprover-community" / "plausible"\n'
        )
        prefixes = engine.derive_trusted_prefixes(tmp_path)
        assert "Mathlib" in prefixes
        assert "Plausible" in prefixes

    def test_fallback_without_workspace(self, tmp_path):
        assert engine.derive_trusted_prefixes(tmp_path) == sorted(
            DEFAULT_TRUSTED_PREFIXES
        )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_save_rotates_and_self_gitignores(self, tmp_path):
        first = mk_snapshot([mk_decl(type_pp="A")], repo_dir=str(tmp_path))
        second = mk_snapshot([mk_decl(type_pp="B")], repo_dir=str(tmp_path))
        path = engine.save_snapshot(first)
        assert path == tmp_path / ".marathon" / "audit" / "latest.json"
        assert (path.parent / ".gitignore").read_text() == "*\n"
        engine.save_snapshot(second)
        latest = engine.load_snapshot(tmp_path)
        previous = engine.load_snapshot(tmp_path, engine.PREVIOUS_NAME)
        assert latest == second
        assert previous == first

    def test_load_absent_and_corrupt(self, tmp_path):
        assert engine.load_snapshot(tmp_path) is None
        d = engine.audit_state_dir(tmp_path)
        d.mkdir(parents=True)
        (d / engine.LATEST_NAME).write_text("{not json")
        assert engine.load_snapshot(tmp_path) is None
        (d / engine.LATEST_NAME).write_text('{"schema_version": 99}')
        assert engine.load_snapshot(tmp_path) is None


# ---------------------------------------------------------------------------
# CLI smoke (run_audit monkeypatched — fully offline)
# ---------------------------------------------------------------------------

def _cli(monkeypatch, *argv):
    from marathon.__main__ import main

    monkeypatch.setattr(sys, "argv", ["marathon", *argv])
    main()


class TestCLI:
    def test_run_prints_summary_and_saves(self, tmp_path, monkeypatch, capsys):
        snap = mk_snapshot(
            [
                mk_decl(name="Foo.ok", type_pp="A"),
                mk_decl(name="Foo.sorried", type_pp="B",
                        axioms=["sorryAx"], has_sorry=True),
                mk_decl(name="Foo.smuggled", type_pp="C",
                        axioms=["Foo.bad_axiom"]),
                mk_decl(name="Foo.vacuous", type_pp="D",
                        tags=["vacuous_body"]),
                mk_decl(name="Foo.dark", status="unknown", type_pp=None,
                        has_sorry=None, reason="pp failed"),
            ],
            repo_dir=str(tmp_path),
        )
        monkeypatch.setattr(
            "marathon.audit.engine.run_audit",
            lambda repo, target, timeout: snap,
        )
        _cli(monkeypatch, "audit", "run", "--repo-dir", str(tmp_path),
             "--target", "Foo")
        out = capsys.readouterr().out
        assert "audited 5 declaration(s)" in out
        assert "sorry'd (transitive sorryAx): 1" in out
        assert "unknown (no evidence): 1" in out
        assert "axioms beyond whitelist: 1 (Foo.bad_axiom)" in out
        assert "Foo.vacuous: vacuous_body" in out
        assert (tmp_path / ".marathon" / "audit" / "latest.json").is_file()

    def test_run_exits_nonzero_on_empty_failed_audit(
        self, tmp_path, monkeypatch, capsys
    ):
        snap = mk_snapshot(
            [], repo_dir=str(tmp_path), failures=["lake not found"]
        )
        monkeypatch.setattr(
            "marathon.audit.engine.run_audit",
            lambda repo, target, timeout: snap,
        )
        with pytest.raises(SystemExit) as exc:
            _cli(monkeypatch, "audit", "run", "--repo-dir", str(tmp_path),
                 "--target", "Foo")
        assert exc.value.code == 1
        assert "lake not found" in capsys.readouterr().out

    def test_diff_smoke(self, tmp_path, monkeypatch, capsys):
        old = mk_snapshot([mk_decl(name="Foo.t", type_pp="A")],
                          repo_dir=str(tmp_path))
        new = mk_snapshot([mk_decl(name="Foo.t", type_pp="B")],
                          repo_dir=str(tmp_path))
        engine.save_snapshot(old)
        engine.save_snapshot(new)
        _cli(monkeypatch, "audit", "diff", "--repo-dir", str(tmp_path))
        out = capsys.readouterr().out
        assert "type_changed: 1" in out
        assert "Foo.t" in out

    def test_diff_without_previous_exits(self, tmp_path, monkeypatch, capsys):
        engine.save_snapshot(mk_snapshot([mk_decl()], repo_dir=str(tmp_path)))
        with pytest.raises(SystemExit):
            _cli(monkeypatch, "audit", "diff", "--repo-dir", str(tmp_path))
        assert "no previous" in capsys.readouterr().out

    def test_show_exact_and_suffix(self, tmp_path, monkeypatch, capsys):
        engine.save_snapshot(mk_snapshot(
            [mk_decl(name="Foo.Bar.thm", type_pp="∀ x, x = x")],
            repo_dir=str(tmp_path),
        ))
        _cli(monkeypatch, "audit", "show", "Foo.Bar.thm",
             "--repo-dir", str(tmp_path))
        out = capsys.readouterr().out
        assert "type: ∀ x, x = x" in out
        assert "fingerprint_type:" in out
        _cli(monkeypatch, "audit", "show", "thm",
             "--repo-dir", str(tmp_path))
        assert "Foo.Bar.thm" in capsys.readouterr().out

    def test_show_missing_decl_exits(self, tmp_path, monkeypatch, capsys):
        engine.save_snapshot(mk_snapshot([mk_decl()], repo_dir=str(tmp_path)))
        with pytest.raises(SystemExit) as exc:
            _cli(monkeypatch, "audit", "show", "Nope.nothere",
                 "--repo-dir", str(tmp_path))
        assert exc.value.code == 1
