"""marathon.audit — elaborator-grade audit engine (phase 5).

Three layers:

* :mod:`marathon.audit.lean_template` — the Lean-side audit script
  template and its machine-parseable output contract;
* :mod:`marathon.audit.records` — :class:`~marathon.audit.records.DeclAudit`
  / :class:`~marathon.audit.records.AuditSnapshot` evidence records with
  computed sha256 fingerprints (theorems by elaborated type; def-like
  kinds by type AND value);
* :mod:`marathon.audit.engine` — render/run/parse over ``lake env lean``
  inside the target repo's own workspace, snapshot persistence at
  ``<repo>/.marathon/audit/latest.json`` (self-gitignored derived cache),
  and the pure :func:`~marathon.audit.engine.diff_snapshots`
  invalidation feed.

Submodules are imported lazily by consumers (no eager re-exports): the
template module must stay importable without pulling the engine's
dependencies.
"""

__all__ = ["engine", "lean_template", "records"]
