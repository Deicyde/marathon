"""marathon.audit.records — typed audit evidence (phase 5 audit engine).

:class:`DeclAudit` mirrors one ``AUDIT|`` line of the Lean script contract
(:mod:`marathon.audit.lean_template`) with the base64 fields decoded, plus
two *computed* sha256 fingerprints:

* ``fingerprint_type`` — over the pinned-pp elaborated type (every kind).
* ``fingerprint_value`` — over the pinned-pp value, ONLY for the def-like
  kinds (:data:`~marathon.audit.lean_template.VALUE_KINDS`): a definition's
  meaning is its value, so type-only fingerprinting is unsound for defs;
  theorems carry no value fingerprint by proof irrelevance.

Per the plan's binding ruling ("trust is computed, never stored"),
fingerprints are derived fields: :meth:`DeclAudit.from_json` recomputes
them from the stored pp strings and ignores any persisted hash — a hash on
disk is a cache hint, never evidence.

:class:`AuditSnapshot` is one full audit run with enough out-of-band
context (``lean-toolchain`` content, ``.lake`` package revs) that
cross-version fingerprint comparisons can be *flagged* instead of trusted
(see :func:`marathon.audit.engine.diff_snapshots`).

Declarations with ``status == "unknown"`` (mid-decl elaboration failure,
or whole-module failure recorded by the engine) are preserved verbatim:
no pp strings, no fingerprints, ``has_sorry`` is ``None`` — absence of
evidence is reported, never punished or hidden.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from marathon.audit.lean_template import VALUE_KINDS

#: Version of the persisted snapshot JSON (``.marathon/audit/latest.json``).
SNAPSHOT_SCHEMA_VERSION = 1

#: The two declaration status values of the Lean-script contract.
STATUSES = ("ok", "unknown")


def fingerprint(text: str) -> str:
    """sha256 hex digest over the UTF-8 bytes of a pinned-pp string.

    The input is the *decoded* pretty-printer output exactly as the Lean
    script emitted it (the pp option set is pinned there); no further
    normalization happens here, so whitespace-identical reconstruction
    yields the identical digest."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeclAudit:
    """One declaration's audit evidence (one ``AUDIT|`` contract line).

    Field semantics follow the contract docstring in
    ``marathon/audit/lean_template.py``; list fields are kept exactly as
    emitted (the script sorts them)."""

    name: str
    kind: str
    module: str
    status: str  # "ok" | "unknown"
    type_pp: str | None  # decoded pinned-pp elaborated type; None iff unknown
    value_pp: str | None  # decoded pinned-pp value; def-like kinds only
    cone: list[str]  # project-local constants referenced by the TYPE
    axioms: list[str]  # transitive axioms (sorryAx included)
    has_sorry: bool | None  # None when status is "unknown" (field was "-")
    tags: list[str]  # deception tags
    reason: str | None  # decoded failure reason; None unless unknown
    # Computed, never taken from input (see module docstring).
    fingerprint_type: str | None = field(init=False, default=None)
    fingerprint_value: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        ft = fingerprint(self.type_pp) if self.type_pp is not None else None
        fv = (
            fingerprint(self.value_pp)
            if self.kind in VALUE_KINDS and self.value_pp is not None
            else None
        )
        object.__setattr__(self, "fingerprint_type", ft)
        object.__setattr__(self, "fingerprint_value", fv)

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "module": self.module,
            "status": self.status,
            "type_pp": self.type_pp,
            "value_pp": self.value_pp,
            "cone": list(self.cone),
            "axioms": list(self.axioms),
            "has_sorry": self.has_sorry,
            "tags": list(self.tags),
            "reason": self.reason,
            # Informational only — recomputed (never trusted) on load.
            "fingerprint_type": self.fingerprint_type,
            "fingerprint_value": self.fingerprint_value,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "DeclAudit":
        """Rebuild from :meth:`to_json` output.  Any persisted
        ``fingerprint_*`` keys are ignored; fingerprints are recomputed
        from the pp strings in ``__post_init__``."""
        return cls(
            name=d["name"],
            kind=d["kind"],
            module=d["module"],
            status=d["status"],
            type_pp=d.get("type_pp"),
            value_pp=d.get("value_pp"),
            cone=list(d.get("cone") or []),
            axioms=list(d.get("axioms") or []),
            has_sorry=d.get("has_sorry"),
            tags=list(d.get("tags") or []),
            reason=d.get("reason"),
        )


@dataclass
class AuditSnapshot:
    """One audit run: declarations + failures + cross-version context.

    ``failures`` is the run-level honesty channel: lake missing, nonzero
    exit, timeout, truncated output, malformed contract lines — anything
    that means evidence may be absent.  An empty ``decls`` list with a
    populated ``failures`` list is a valid (honest) snapshot."""

    repo_dir: str
    modules: list[str]  # fully qualified target modules, sorted
    toolchain: str | None  # lean-toolchain file content (stripped)
    lean_version: str | None  # in-band Lean.versionString from AUDIT_META
    package_revs: dict[str, str]  # lake-manifest package name -> rev
    trusted_prefixes: list[str]  # partition list used for this run
    created_at: str  # ISO-8601 UTC
    decls: list[DeclAudit]
    failures: list[str]
    schema_version: int = SNAPSHOT_SCHEMA_VERSION

    def by_name(self) -> dict[str, DeclAudit]:
        return {d.name: d for d in self.decls}

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "repo_dir": self.repo_dir,
            "modules": list(self.modules),
            "toolchain": self.toolchain,
            "lean_version": self.lean_version,
            "package_revs": dict(self.package_revs),
            "trusted_prefixes": list(self.trusted_prefixes),
            "created_at": self.created_at,
            "decls": [d.to_json() for d in self.decls],
            "failures": list(self.failures),
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "AuditSnapshot":
        version = d.get("schema_version")
        if version != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported audit snapshot schema {version!r} "
                f"(engine speaks {SNAPSHOT_SCHEMA_VERSION})"
            )
        return cls(
            repo_dir=d.get("repo_dir") or "",
            modules=list(d.get("modules") or []),
            toolchain=d.get("toolchain"),
            lean_version=d.get("lean_version"),
            package_revs=dict(d.get("package_revs") or {}),
            trusted_prefixes=list(d.get("trusted_prefixes") or []),
            created_at=d.get("created_at") or "",
            decls=[DeclAudit.from_json(x) for x in d.get("decls") or []],
            failures=list(d.get("failures") or []),
            schema_version=SNAPSHOT_SCHEMA_VERSION,
        )
