"""Tests for the Lean audit-script template (phase 5 audit engine, Lean side).

Two layers:

* always-run — string-level checks on the rendered template plus contract
  checks on the committed golden file (so the output contract stays
  executable on machines without a Lean toolchain);
* toolchain-gated — build the fixture package, run the generated script via
  ``lake env lean``, and compare against the golden file.  Skipped unless
  ``lake``/``elan`` exist AND the fixture's pinned toolchain is already
  installed (probing ``elan toolchain list`` so the test never triggers a
  surprise toolchain download).

Golden regeneration::

    MARATHON_REGEN_GOLDEN=1 uv run pytest tests/test_audit_lean_script.py -q

Documented normalizations applied before golden comparison (see
``extract_audit_lines``): non-contract stdout lines are dropped, a leading
``file:line:col: info:`` diagnostic prefix is stripped, trailing whitespace
is stripped, and the comparison is order-insensitive (the script sorts by
declaration name, but hash-map iteration upstream is not contractual).
"""

from __future__ import annotations

import base64
import os
import pathlib
import shutil
import subprocess

import pytest

from marathon.audit.lean_template import (
    AUDIT_FIELD_COUNT,
    BEGIN_SENTINEL,
    DEFAULT_TRUSTED_PREFIXES,
    DONE_SENTINEL,
    KINDS,
    META_SENTINEL,
    SCHEMA_VERSION,
    SENTINEL,
    VALUE_KINDS,
    render_audit_script,
)

TESTS_DIR = pathlib.Path(__file__).resolve().parent
FIXTURE_DIR = TESTS_DIR / "fixtures" / "audit_fixture"
GOLDEN_PATH = TESTS_DIR / "golden" / "audit_fixture_output.txt"
FIXTURE_MODULES = ("AuditFixture.Basic", "AuditFixture.Deception")
PINNED_TOOLCHAIN = (FIXTURE_DIR / "lean-toolchain").read_text().strip()

_FIELD_NAMES = (
    "sentinel", "name", "kind", "module", "status", "type_b64", "value_b64",
    "cone", "axioms", "has_sorry", "tags", "reason_b64",
)


# ---------------------------------------------------------------------------
# Toolchain probing
# ---------------------------------------------------------------------------

def _elan_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    cand = pathlib.Path.home() / ".elan" / "bin" / name
    return str(cand) if cand.exists() else None


def _skip_reason() -> str:
    """Empty string when the gated tests can run; else the skip reason."""
    if _elan_tool("lake") is None:
        return "lake not installed (no PATH lake, no ~/.elan/bin/lake)"
    elan = _elan_tool("elan")
    if elan is None:
        return "elan not installed"
    try:
        proc = subprocess.run(
            [elan, "toolchain", "list"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        return f"elan toolchain list failed: {exc}"
    installed = {
        line.split()[0] for line in proc.stdout.splitlines() if line.strip()
    }
    if PINNED_TOOLCHAIN not in installed:
        return f"fixture toolchain {PINNED_TOOLCHAIN} not installed"
    return ""


_REASON = _skip_reason()
needs_lake = pytest.mark.skipif(bool(_REASON), reason=_REASON or "unreachable")


# ---------------------------------------------------------------------------
# Contract-line helpers
# ---------------------------------------------------------------------------

_PREFIXES = (SENTINEL + "|", META_SENTINEL + "|", DONE_SENTINEL + "|")


def extract_audit_lines(raw: str) -> list[str]:
    """Normalize raw ``lake env lean`` stdout to contract lines only."""
    out: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip()
        if line.startswith(_PREFIXES) or line == BEGIN_SENTINEL:
            out.append(line)
            continue
        # Some toolchains fold the first #eval print into an
        # `<file>:<line>:<col>: info: ` diagnostic prefix.
        idx = line.find(SENTINEL)
        if idx > 0 and "info:" in line[:idx]:
            out.append(line[idx:])
    return out


def parse_records(lines: list[str]) -> dict[str, dict]:
    """Parse AUDIT lines into per-declaration dicts, keyed by name."""
    recs: dict[str, dict] = {}
    for line in lines:
        if not line.startswith(SENTINEL + "|"):
            continue
        parts = line.split("|")
        assert len(parts) == AUDIT_FIELD_COUNT, f"bad field count: {line}"
        rec = dict(zip(_FIELD_NAMES, parts))
        rec["type_pp"] = (
            None if rec["type_b64"] == "-"
            else base64.b64decode(rec["type_b64"], validate=True).decode()
        )
        rec["value_pp"] = (
            None if rec["value_b64"] == "-"
            else base64.b64decode(rec["value_b64"], validate=True).decode()
        )
        rec["cone_list"] = [] if rec["cone"] == "-" else rec["cone"].split(",")
        rec["axiom_list"] = (
            [] if rec["axioms"] == "-" else rec["axioms"].split(",")
        )
        rec["tag_list"] = [] if rec["tags"] == "-" else rec["tags"].split(";")
        assert rec["name"] not in recs, f"duplicate decl: {rec['name']}"
        recs[rec["name"]] = rec
    return recs


def metas(lines: list[str]) -> list[tuple[str, str]]:
    out = []
    for line in lines:
        if line.startswith(META_SENTINEL + "|"):
            _, key, value = line.split("|", 2)
            out.append((key, value))
    return out


def assert_contract_shape(lines: list[str]) -> None:
    """Structural checks every audit emission must satisfy."""
    assert lines[0] == BEGIN_SENTINEL
    meta = dict(metas(lines))
    assert meta["schema"] == SCHEMA_VERSION
    assert "lean_version" in meta
    recs = parse_records(lines)
    assert recs, "no AUDIT lines"
    done = [l for l in lines if l.startswith(DONE_SENTINEL + "|")]
    assert len(done) == 1, "exactly one AUDIT_DONE trailer expected"
    assert int(done[0].split("|")[1]) == len(recs)
    assert lines[-1] == done[0]
    for rec in recs.values():
        assert rec["kind"] in KINDS, rec
        assert rec["status"] in ("ok", "unknown"), rec
        # unknown rows carry '-' placeholders for the evidence fields
        # (has_sorry/tags/cone/axioms) — see unknownLine in lean_template.
        if rec["status"] == "unknown":
            assert rec["has_sorry"] in ("true", "false", "-"), rec
        else:
            assert rec["has_sorry"] in ("true", "false"), rec
        if rec["status"] == "ok":
            assert rec["type_pp"], rec
            assert rec["reason_b64"] == "-", rec
            if rec["kind"] in VALUE_KINDS:
                assert rec["value_pp"], rec
            else:
                assert rec["value_pp"] is None, rec
        else:
            assert rec["reason_b64"] != "-", rec


# ---------------------------------------------------------------------------
# Always-run: template rendering
# ---------------------------------------------------------------------------

class TestRender:
    def test_imports_and_sentinels(self):
        src = render_audit_script(FIXTURE_MODULES)
        for mod in FIXTURE_MODULES:
            assert f"import {mod}\n" in src
        assert "import Lean" in src
        assert f'IO.println "{BEGIN_SENTINEL}"' in src
        assert f'"{META_SENTINEL}|schema|{SCHEMA_VERSION}"' in src
        assert DONE_SENTINEL in src
        assert "#eval!" in src
        # The audited module list is spliced as Name literals.
        for mod in FIXTURE_MODULES:
            assert f"`{mod}" in src

    def test_no_unsubstituted_placeholders(self):
        src = render_audit_script(FIXTURE_MODULES)
        assert "__AUDIT_" not in src

    def test_default_trusted_prefixes_spliced(self):
        src = render_audit_script(FIXTURE_MODULES)
        for prefix in DEFAULT_TRUSTED_PREFIXES:
            assert f"`{prefix}" in src
        assert "Mathlib" in src  # the load-bearing one

    def test_custom_trusted_prefixes(self):
        src = render_audit_script(FIXTURE_MODULES, trusted_prefixes=["Foo"])
        assert "[`Foo]" in src
        assert "`Mathlib" not in src
        empty = render_audit_script(FIXTURE_MODULES, trusted_prefixes=[])
        assert "([] : List Name)" in empty

    def test_rejects_unsafe_names(self):
        for bad in ("Evil; #eval IO.println 1", "«weird»", "", "1Bad",
                    "Dot..Dot", "sp ace", "pipe|name"):
            with pytest.raises(ValueError):
                render_audit_script([bad])
            with pytest.raises(ValueError):
                render_audit_script(FIXTURE_MODULES, trusted_prefixes=[bad])

    def test_rejects_empty_module_list(self):
        with pytest.raises(ValueError):
            render_audit_script([])


# ---------------------------------------------------------------------------
# Always-run: the committed golden file must satisfy the contract, so the
# contract stays checkable on machines without a toolchain.
# ---------------------------------------------------------------------------

class TestGoldenFileContract:
    def test_golden_satisfies_contract(self):
        lines = GOLDEN_PATH.read_text().splitlines()
        assert_contract_shape(lines)

    def test_golden_covers_fixture_behaviors(self):
        recs = parse_records(GOLDEN_PATH.read_text().splitlines())
        kinds = {r["kind"] for r in recs.values()}
        assert {"theorem", "def", "instance", "abbrev",
                "structure", "class", "axiom"} <= kinds
        tags = {t for r in recs.values() for t in r["tag_list"]}
        assert {"vacuous_body", "proof_by_exfalso", "ignores_params",
                "trivial_instance"} <= tags
        assert any(r["has_sorry"] == "true" for r in recs.values())
        assert any(n.startswith("_private.") for n in recs)


# ---------------------------------------------------------------------------
# Toolchain-gated: run the script for real
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def audit_lines() -> list[str]:
    env = os.environ.copy()
    env["PATH"] = (
        str(pathlib.Path.home() / ".elan" / "bin")
        + os.pathsep + env.get("PATH", "")
    )
    lake = _elan_tool("lake")
    build = subprocess.run(
        [lake, "build"], cwd=FIXTURE_DIR, env=env,
        capture_output=True, text=True, timeout=600,
    )
    assert build.returncode == 0, f"lake build failed:\n{build.stdout}\n{build.stderr}"
    # .lake/ is gitignored — a safe scratch spot inside the workspace.
    script = FIXTURE_DIR / ".lake" / "marathon_audit_script.lean"
    script.write_text(render_audit_script(FIXTURE_MODULES))
    run = subprocess.run(
        [lake, "env", "lean", str(script)], cwd=FIXTURE_DIR, env=env,
        capture_output=True, text=True, timeout=600,
    )
    assert run.returncode == 0, f"audit script failed:\n{run.stdout}\n{run.stderr}"
    lines = extract_audit_lines(run.stdout)
    if os.environ.get("MARATHON_REGEN_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text("\n".join(lines) + "\n")
    return lines


@needs_lake
class TestLiveRun:
    def test_matches_golden(self, audit_lines):
        golden = GOLDEN_PATH.read_text().splitlines()
        assert sorted(audit_lines) == sorted(golden), (
            "audit output drifted from golden; if intentional, regenerate "
            "with MARATHON_REGEN_GOLDEN=1"
        )

    def test_contract_shape(self, audit_lines):
        assert_contract_shape(audit_lines)

    def test_meta_lines(self, audit_lines):
        meta = metas(audit_lines)
        assert dict(meta)["schema"] == SCHEMA_VERSION
        # lean-toolchain pins leanprover/lean4:vX.Y.Z; the script reports the
        # version that actually elaborated — they must agree.
        assert dict(meta)["lean_version"] == PINNED_TOOLCHAIN.rsplit(":v", 1)[1]
        assert [v for k, v in meta if k == "module"] == list(FIXTURE_MODULES)

    def test_theorem_has_no_value_def_does(self, audit_lines):
        recs = parse_records(audit_lines)
        thm = recs["AuditFixture.double_eq"]
        assert thm["kind"] == "theorem"
        assert thm["value_pp"] is None  # proof irrelevance
        dfn = recs["AuditFixture.double"]
        assert dfn["kind"] == "def"
        assert dfn["value_pp"] is not None
        assert "n n" in dfn["value_pp"]  # fun n => HAdd.hAdd n n
        abbr = recs["AuditFixture.twice"]
        assert abbr["kind"] == "abbrev"
        assert abbr["value_pp"] is not None

    def test_sorry_flagged_and_distinguished(self, audit_lines):
        recs = parse_records(audit_lines)
        sorried = recs["AuditFixture.double_is_two_mul"]
        assert sorried["has_sorry"] == "true"
        assert "sorryAx" in sorried["axiom_list"]
        # Custom axiom is reported but NOT conflated with sorry.
        axuser = recs["AuditFixture.le_double"]
        assert axuser["axiom_list"] == ["AuditFixture.double_growth"]
        assert axuser["has_sorry"] == "false"
        assert recs["AuditFixture.double_growth"]["kind"] == "axiom"

    def test_deception_tags(self, audit_lines):
        recs = parse_records(audit_lines)
        assert recs["AuditFixture.vacuous_truth"]["tag_list"] == ["vacuous_body"]
        assert recs["AuditFixture.from_false"]["tag_list"] == ["proof_by_exfalso"]
        assert recs["AuditFixture.ignores_input"]["tag_list"] == ["ignores_params"]
        trivial = [r for r in recs.values()
                   if "trivial_instance" in r["tag_list"]]
        assert len(trivial) == 1
        assert trivial[0]["kind"] == "instance"
        assert "PUnit" in trivial[0]["type_pp"]
        # Honest instances stay untagged.
        assert recs["AuditFixture.instCollapsibleNat"]["tag_list"] == []
        assert recs["AuditFixture.instInhabitedPoint"]["tag_list"] == []

    def test_cone_is_project_local_only(self, audit_lines):
        recs = parse_records(audit_lines)
        # The dependent theorem's cone is exactly the local def...
        assert recs["AuditFixture.double_eq"]["cone_list"] == ["AuditFixture.double"]
        # ...and across every record, nothing from trusted packages leaks in.
        for rec in recs.values():
            for entry in rec["cone_list"]:
                assert entry.startswith(("AuditFixture.", "_private.AuditFixture.")), (
                    f"non-project-local cone entry {entry!r} in {rec['name']}"
                )
        # Instance-in-type embedding is captured (cross-module statement).
        cone = set(recs["AuditFixture.collapse_nat"]["cone_list"])
        assert cone == {
            "AuditFixture.Collapsible.collapse",
            "AuditFixture.double",
            "AuditFixture.instCollapsibleNat",
        }

    def test_private_decls_audited(self, audit_lines):
        recs = parse_records(audit_lines)
        private = [n for n in recs if n.startswith("_private.")]
        assert len(private) == 1
        assert recs[private[0]]["status"] == "ok"
        # A public statement mentioning a private def carries it in its cone.
        assert recs["AuditFixture.hiddenHelper_eq"]["cone_list"] == private

    def test_weird_decls_survive(self, audit_lines):
        recs = parse_records(audit_lines)
        for name in ("AuditFixture.polyId", "AuditFixture.isEvenF",
                     "AuditFixture.isOddF"):
            assert recs[name]["status"] == "ok"
            assert recs[name]["kind"] == "def"
            assert recs[name]["value_pp"] is not None
        # The whole fixture elaborates: no unknowns anywhere.
        assert all(r["status"] == "ok" for r in recs.values())
