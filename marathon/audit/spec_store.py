"""Persistence for the spec-auditor's Claude-rendered card halves.

The spec-auditor (:mod:`marathon.spec_auditor`) renders the informal,
plain-English statement of a declaration *from its Lean type alone* — never
the source text (the firewall). That rendering is a Claude call, so it is
generated on demand and cached here rather than recomputed on every card view.

Cache: ``<repo>/.marathon/audit/spec_audits.json`` — a derived, self-gitignored
sidecar (same convention as ``latest.json``), one entry per declaration::

    {"<decl name>": {
        "informal_rendering": "...",        # LLM-rendered from the Lean type
        "delta_prose": "..." | null,         # advisory semantic-delta sentence
        "fingerprint_type": "<sha>",         # the type fingerprint at gen time
        "generated_at": "<iso8601>"
    }}

The ``fingerprint_type`` pin is load-bearing: a cached rendering is shown only
while it still matches the current snapshot's type fingerprint. When the
statement changes, the stale rendering is dropped (the card falls back to "no
rendering") rather than describing the old statement — the same
fingerprint-invalidation discipline the trust tiers use.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from marathon.audit.engine import audit_state_dir

SPEC_AUDITS_NAME = "spec_audits.json"


def spec_audits_path(repo_dir: str | Path) -> Path:
    return audit_state_dir(repo_dir) / SPEC_AUDITS_NAME


def load_spec_audits(repo_dir: str | Path) -> dict[str, dict]:
    """Load the spec-audit cache; ``{}`` when absent or unparseable (a
    corrupt cache is absence of evidence, not an error)."""
    path = spec_audits_path(repo_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_spec_audits(repo_dir: str | Path, data: dict[str, dict]) -> None:
    """Atomically write the cache (temp + replace, beside the target)."""
    path = spec_audits_path(repo_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def lookup(
    repo_dir: str | Path, decl_name: str, current_fingerprint_type: Optional[str]
) -> Optional[dict]:
    """Return the cached entry for ``decl_name`` ONLY if its pinned type
    fingerprint still matches ``current_fingerprint_type`` (so a stale
    rendering for a changed statement is never surfaced). ``None`` otherwise.

    Pass the cache pre-loaded via :func:`load_spec_audits` for batch use; this
    convenience loads per call."""
    entry = load_spec_audits(repo_dir).get(decl_name)
    if not entry:
        return None
    if entry.get("fingerprint_type") != current_fingerprint_type:
        return None
    return entry


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_spec_audits(
    repo_dir: str | Path,
    snapshot: Any,
    ledger: Any,
    decl_names: Iterable[str],
    *,
    model: Optional[str] = None,
    progress: Optional[Any] = None,
) -> dict[str, dict]:
    """Run the spec-auditor for each named declaration, merge the results
    into the on-disk cache, and return the full cache.

    Each decl: build its :class:`~marathon.audit.spec_card.SpecCard` machine
    half from the snapshot, run the (firewalled) Claude render, and store the
    informal rendering + advisory delta pinned to the current type
    fingerprint. A decl absent/unknown in the snapshot, or whose Claude call
    fails, is skipped (the auditor is advisory — never fatal). ``progress`` is
    an optional ``callable(decl_name, ok: bool)`` for CLI feedback."""
    from marathon.audit.spec_card import SpecCard
    from marathon.spec_auditor import audit_spec_card

    by_name = snapshot.by_name()
    cache = load_spec_audits(repo_dir)
    for name in decl_names:
        decl = by_name.get(name)
        if decl is None or getattr(decl, "is_unknown", False):
            if progress:
                progress(name, False)
            continue
        try:
            card = SpecCard.from_snapshot(name, snapshot, ledger)
            audit = audit_spec_card(card, model=model)
        except Exception:  # noqa: BLE001 — advisory layer never fatal
            audit = None
        if audit is None or not audit.informal_rendering:
            if progress:
                progress(name, False)
            continue
        cache[name] = {
            "informal_rendering": audit.informal_rendering,
            "delta_prose": audit.delta_prose,
            "fingerprint_type": decl.fingerprint_type,
            "generated_at": _now(),
        }
        if progress:
            progress(name, True)
    save_spec_audits(repo_dir, cache)
    return cache
