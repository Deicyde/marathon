"""Parser construction and command handlers for ``marathon audit``."""

import asyncio
from pathlib import Path

from marathon.audit.probes import ProbeKind
from marathon.probes_aristotle import DEFAULT_MAX_PROBES as _VAC_DEFAULT_MAX


def _add_audit_subparser(subparsers) -> None:
    """Adds `marathon audit run/diff/show/tiers/backfill` — the Phase-5
    elaborator-grade audit engine (see ``marathon.audit.engine``): runs
    the generated Lean audit script inside the target repo's own
    workspace via ``lake env lean``, records per-declaration evidence
    (elaborated-type/value fingerprints over project-local vocabulary,
    transitive axioms, sorry accounting, deception tags), and persists
    snapshots at ``<repo>/.marathon/audit/latest.json`` (self-gitignored
    derived cache; the prior run rotates to ``previous.json``). `tiers`
    and `backfill` are the Phase-5b trust layer (see
    ``marathon.audit.trust``): tiers computed on read from snapshot +
    ledger verdict events, never stored."""
    p_audit = subparsers.add_parser(
        "audit",
        help="Elaborator-grade declaration audit (.marathon/audit/latest.json).",
        description=(
            "Run, diff, or inspect declaration audits. `run` derives the "
            "target folder's modules, elaborates them with the repo's own "
            "pinned toolchain (`lake env lean`), and snapshots per-decl "
            "evidence: pinned-pp type/value fingerprints (project-local "
            "constants only — Mathlib/Std/... are trusted vocabulary), "
            "transitive axioms, sorry accounting, deception tags. "
            "Declarations that fail to elaborate are recorded with status "
            "`unknown` — absence of evidence is reported, never hidden. "
            "Snapshots are derived cache (recomputable, gitignored), "
            "never committed."
        ),
    )
    sub = p_audit.add_subparsers(dest="audit_command", required=True)

    p_run = sub.add_parser(
        "run",
        help="Audit a folder's declarations and save latest.json.",
    )
    p_run.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root (lake workspace). Default: current directory.",
    )
    p_run.add_argument(
        "--target", required=True, metavar="FOLDER",
        help=(
            "Folder of .lean files to audit, relative to --repo-dir "
            "(e.g. GeometricAnalysis/LeeSM). A single .lean file works too."
        ),
    )
    p_run.add_argument(
        "--timeout", type=int, default=900, metavar="SECONDS",
        help="Timeout for the `lake env lean` run. Default: 900.",
    )
    p_run.set_defaults(func=_run_audit_run)

    p_diff = sub.add_parser(
        "diff",
        help="Per-decl changes: latest.json vs previous.json.",
    )
    p_diff.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_diff.set_defaults(func=_run_audit_diff)

    p_show = sub.add_parser(
        "show",
        help="Print one declaration's audit record from latest.json.",
    )
    p_show.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix "
            "also works, e.g. `double_eq`)."
        ),
    )
    p_show.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_show.set_defaults(func=_run_audit_show)

    p_tiers = sub.add_parser(
        "tiers",
        help="Computed trust tier per declaration (snapshot + verdicts).",
        description=(
            "Compute the trust tier of every declaration in the latest "
            "audit snapshot. Tiers are COMPUTED on read from the snapshot "
            "plus the ledger's append-only decl-verdict events — never "
            "stored (plan §2 ruling 4). UNKNOWN = absent/failed to "
            "elaborate; T0 = elaborated; T1 = axiom-clean (sorryAx "
            "accounted), no deception tags; T2 = + human spec-verdict "
            "with matching type+cone pins; T3 = + line-review verdict. "
            "Stale or broken pins surface as qualifiers "
            "(stale-toolchain, fingerprint-changed, cone-changed:X)."
        ),
    )
    p_tiers.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_tiers.add_argument(
        "--target", default=None, metavar="FOLDER",
        help=(
            "Restrict the table to declarations from this folder's "
            "modules (same semantics as `audit run --target`). "
            "Default: every declaration in the snapshot."
        ),
    )
    p_tiers.set_defaults(func=_run_audit_tiers)

    p_backfill = sub.add_parser(
        "backfill",
        help="Pin existing VERIFIED review issues as T2 verdicts (one-time).",
        description=(
            "Map every VERIFIED review sub-issue's cited declarations "
            "onto the LATEST audit snapshot and append T2 verdict events "
            "pinned to CURRENT main (source='backfill'). The historical "
            "verdict-time SHAs include red builds and are unbuildable, "
            "so these pins assert 'current main matches what I verified "
            "back then' — which is why writing REQUIRES --attest (one "
            "human attestation pass). Without --attest this is a dry "
            "run: the full would-be-pinned table prints and nothing is "
            "written. Declarations missing from the snapshot are listed "
            "as skipped with a reason, never guessed."
        ),
    )
    p_backfill.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_backfill.add_argument(
        "--chapter", type=int, default=None, metavar="N",
        help="Backfill only this chapter's issues. Default: all chapters.",
    )
    p_backfill.add_argument(
        "--attest", action="store_true",
        help=(
            "Attest that current main matches what you verified, and "
            "write the T2 events. Default: dry run."
        ),
    )
    p_backfill.set_defaults(func=_run_audit_backfill)

    p_inval = sub.add_parser(
        "invalidations",
        help="Report (and optionally surface) T2/T3 verdicts the latest "
             "snapshot no longer covers.",
        description=(
            "Diff the LATEST snapshot against the PREVIOUS one and the "
            "ledger's live verdicts: which human T2/T3 verdicts no longer "
            "cover the current code? A verdict is INVALIDATED when the "
            "decl's own type fingerprint changed, when a pinned cone "
            "member changed meaning or vanished (the member is NAMED), or "
            "when the decl went absent/unknown. Cross-toolchain wholesale "
            "staleness is reported SEPARATELY (resolve with `audit repin`, "
            "never a per-decl alarm). Default is a DRY RUN that writes "
            "nothing. With --apply: ONE batched parent-issue body rewrite "
            "flips every affected emoji 🟡→🟠, plus one idempotent "
            "marker-comment per affected issue behind a circuit breaker "
            "(content-hash dedup + per-issue daily cap) — the write-storm "
            "ruling forbids one body rewrite or one tracker flip per decl."
        ),
    )
    p_inval.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_inval.add_argument(
        "--apply", action="store_true",
        help=(
            "Flip the affected tracker emojis (one batched rewrite) and "
            "post circuit-broken marker comments. Default: dry run "
            "(print the table, write nothing)."
        ),
    )
    p_inval.set_defaults(func=_run_audit_invalidations)

    p_repin = sub.add_parser(
        "repin",
        help="Bulk trust override: re-pin stale/changed verdicts to the "
             "CURRENT snapshot (requires --yes).",
        description=(
            "RE-PIN AMNESTY — the bulk trust override. For every decl "
            "carrying a live T2/T3 verdict whose pins no longer match the "
            "current snapshot (own type changed, cone changed, or stale "
            "across a toolchain bump), print the re-pin table (decl, old "
            "fingerprint prefix → new, change kind: type-text / cone / "
            "toolchain-only). With --yes, append a NEW source='repin' "
            "verdict event for each, pinned to the CURRENT snapshot "
            "(append-only — the old events are preserved, never mutated). "
            "\n\nThis asserts that YOU re-checked these declarations "
            "against their cards, OR that you accept the changes "
            "sight-unseen: re-pinning silences the invalidation by "
            "moving the human attestation onto current main. The "
            "trade-off is yours. Decls absent or unknown in the current "
            "snapshot are REFUSED (you cannot re-pin what does not "
            "elaborate). Without --yes this is a dry run."
        ),
    )
    p_repin.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root (lake workspace). Default: current directory.",
    )
    p_repin.add_argument(
        "--decl", action="append", default=None, metavar="NAME",
        help=(
            "Restrict the re-pin to these declarations (repeatable; a "
            "unique dotted suffix also resolves). Default: every decl "
            "with a stale/changed verdict."
        ),
    )
    p_repin.add_argument(
        "--yes", action="store_true",
        help=(
            "Write the source='repin' verdict events. This is the trust "
            "override — see the command description. Default: dry run."
        ),
    )
    p_repin.set_defaults(func=_run_audit_repin)

    p_kernel = sub.add_parser(
        "kernel",
        help="The trust kernel of a declaration: the minimized human-read "
             "surface (project-local defs in its statement cone).",
        description=(
            "Compute the TRUST KERNEL of DECL (plan §2 ruling 5, goal 2's "
            "mechanism): the transitive set of PROJECT-LOCAL definitions in "
            "DECL's elaborated-type statement cone — the exact set of "
            "definitions a human must read to trust the statement. "
            "Mathlib/core constants are trusted vocabulary and are NOT in "
            "the kernel; proof bodies are NEVER in the kernel. Prints the "
            "kernel members (dependencies-first), the size (decls + "
            "pinned-pp LOC), any project-local lemmas whose statements are "
            "referenced but not read, and any unresolved cone references "
            "(absent from the snapshot — reported, never hidden). Pure over "
            "the latest snapshot; no Lean, no network."
        ),
    )
    p_kernel.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix also "
            "works, e.g. `main_thm`)."
        ),
    )
    p_kernel.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_kernel.set_defaults(func=_run_audit_kernel)

    p_card = sub.add_parser(
        "card",
        help="The machine half of a declaration's spec card (markdown).",
        description=(
            "Render the MACHINE HALF of DECL's spec card (the "
            "diff-of-meaning unit, plan §2 ruling 5): the statement, the "
            "'Definitions you must read' = the trust kernel ONLY (never the "
            "whole file), and the evidence table (computed tier, axioms "
            "beyond whitelist, sorry status, deception tags). The Claude "
            "half — fresh informal rendering, kernel-shrink suggestions, "
            "advisory semantic-delta prose — is attached later by the "
            "spec-auditor and is absent here. Pure over the latest snapshot "
            "+ ledger; no Lean, no network."
        ),
    )
    p_card.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix also "
            "works)."
        ),
    )
    p_card.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_card.set_defaults(func=_run_audit_card)

    p_spec = sub.add_parser(
        "spec-audit",
        help="Generate + cache the spec-auditor's informal renderings "
             "(one firewalled Claude call per declaration).",
        description=(
            "Run the spec-auditor over declarations in the latest snapshot "
            "and cache the result in .marathon/audit/spec_audits.json. For "
            "each decl, Claude renders the plain-English statement FROM THE "
            "LEAN TYPE ALONE (never the source .tex — the firewall) plus an "
            "advisory semantic-delta sentence; the deck and `audit card` then "
            "show it, LLM-flagged and pinned to the type fingerprint (a stale "
            "rendering for a changed statement is dropped). Bills the Claude "
            "Max subscription, not Aristotle. Without --decl, audits every "
            "OK declaration in --target (use --limit to cap)."
        ),
    )
    p_spec.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_spec.add_argument(
        "--target", metavar="FOLDER", default=None,
        help=(
            "Restrict to declarations whose module is under this repo-"
            "relative folder (e.g. GeometricAnalysis/LeeSM/Chapter14). "
            "Default: every OK declaration in the snapshot."
        ),
    )
    p_spec.add_argument(
        "--decl", action="append", metavar="NAME", default=None,
        help="A specific declaration to (re)render; repeatable. Overrides "
             "--target/--limit when given.",
    )
    p_spec.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Cap the number of declarations rendered this run (each is a "
             "Claude call). Default: no cap.",
    )
    p_spec.set_defaults(func=_run_audit_spec_audit)

    p_vac = sub.add_parser(
        "vacuity-probe",
        help="OPT-IN, BUDGET-SPENDING: ask Aristotle to DISPROVE a "
             "theorem's hypotheses (success = broken/vacuous spec).",
        description=(
            "!! SPENDS REAL ARISTOTLE BUDGET !! A vacuity probe asks "
            "Aristotle to prove a target theorem's hypotheses entail "
            "`False`. A SUCCESS means the hypotheses are jointly "
            "unsatisfiable, so the theorem is VACUOUSLY TRUE — a "
            "broken/misformalized spec (the documented typo-exploit "
            "failure mode). This is the only probe that ACTIVELY HUNTS "
            "misformalization with a prover, and it is the expensive one, "
            "so it is OPT-IN ONLY (never wired into the auto pipeline) and "
            "governed: a HARD per-invocation cap (--max-probes, default "
            f"{_VAC_DEFAULT_MAX}), and a persisted per-goal DEDUP "
            "(content-hash under .marathon/audit/vacuity/) so the same "
            "vacuity goal is never resubmitted across runs.\n\n"
            "EVIDENCE IS ASYMMETRIC: a SUCCESSFUL disproof is HIGH-SIGNAL "
            "and writes a structured finding (it does NOT auto-file a "
            "rejection this phase — review and reject manually). A "
            "failure-to-disprove is WEAK negative evidence ('no vacuity "
            "found, inconclusive') and NEVER raises any tier or trust "
            "level — Aristotle gives up opaquely, so absence of a finding "
            "proves nothing.\n\n"
            "The probe goal is staged into a THROWAWAY COPY of your repo "
            "(filtered by .gitignore); your working tree is never touched "
            "and the probe .lean is never committed. Pure-Lean probes "
            "(unfolding/sanity) are free and ship first; reach for this "
            "only when you want a prover to hunt vacuous statements."
        ),
    )
    p_vac.add_argument(
        "decl", nargs="+", metavar="DECL",
        help=(
            "One or more target theorem names to probe (fully qualified; a "
            "unique dotted suffix also resolves). Only theorem-like decls "
            "are probeable; defs/structures are skipped."
        ),
    )
    p_vac.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root (lake workspace). Default: current directory.",
    )
    p_vac.add_argument(
        "--max-probes", type=int, default=_VAC_DEFAULT_MAX, metavar="N",
        help=(
            f"HARD cap on probes submitted this invocation (default "
            f"{_VAC_DEFAULT_MAX}). Each probe spends real Aristotle budget; "
            "eligible goals beyond the cap are skipped (reported)."
        ),
    )
    p_vac.add_argument(
        "--polling-interval", type=int, default=30, metavar="SECONDS",
        help="Seconds between task-status polls. Default: 30.",
    )
    p_vac.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Plan only: print which goals WOULD be submitted (under the cap, "
            "minus dedups) and exit WITHOUT spending any budget. Generates "
            "and prints the goal files but submits nothing."
        ),
    )
    p_vac.set_defaults(func=_run_audit_vacuity_probe)

    p_probe = sub.add_parser(
        "probe",
        help="Generate (and optionally build) pure-Lean probes for a decl "
             "(unfolding / sanity — FREE, no Aristotle).",
        description=(
            "Generate the PURE-LEAN probes for DECL (plan §2 ruling 5, "
            "Phase 6b cheap-first tier — NO Aristotle, no budget): an "
            "UNFOLDING probe (a def's wellformedness / total-ness, degrading "
            "honestly to a typecheck when no expected value is supplied — it "
            "does NOT emit rfl-trivial probes that prove nothing) and a "
            "SANITY probe (the PUnit-collapse catcher, for "
            "structures/classes — emitted as 'needs witness' when the "
            "snapshot can't supply a non-collapsing model, never a vacuous "
            "pass). Prints the generated probe source. With --run (and lake "
            "present) it BUILDS each probe OUTSIDE the repo tree (so probe "
            "files can never enter an Aristotle bundle) against the repo's "
            "built modules and reports pass/fail/error/needs_witness — a "
            "FAILING unfolding/sanity probe is a high-signal finding; a "
            "FAILING shrink certificate rejects the shrink. Pure over the "
            "latest snapshot; --run additionally needs the repo built. "
            "(For the BUDGET-SPENDING vacuity probe, see `audit "
            "vacuity-probe`.)"
        ),
    )
    p_probe.add_argument(
        "decl", metavar="DECL",
        help=(
            "Fully qualified declaration name (a unique dotted suffix also "
            "works)."
        ),
    )
    p_probe.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(),
        help="Consumer repo root. Default: current directory.",
    )
    p_probe.add_argument(
        "--kind", action="append", choices=[k.value for k in ProbeKind],
        metavar="KIND",
        help=(
            "Restrict to a probe kind (unfolding / sanity); repeatable. "
            "Default: the kinds that apply to the decl. shrink_certificate "
            "needs a spec-auditor obligation and is never auto-generated "
            "from a decl."
        ),
    )
    p_probe.add_argument(
        "--run", action="store_true",
        help=(
            "Build the generated probes via `lake env lean` (outside the "
            "repo tree) and report outcomes. Default: print source only."
        ),
    )
    p_probe.add_argument(
        "--timeout", type=int, default=600, metavar="SECONDS",
        help="Timeout for each probe's `lake env lean` build. Default: 600.",
    )
    p_probe.set_defaults(func=_run_audit_probe)


def _run_audit_run(args) -> None:
    from marathon.audit.engine import run_audit, save_snapshot
    from marathon.gate import AXIOM_WHITELIST, SORRY_AXIOM

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = run_audit(repo_dir, args.target, timeout=args.timeout)
    path = save_snapshot(snapshot, repo_dir)
    decls = snapshot.decls
    sorried = sum(1 for d in decls if d.has_sorry is True)
    unknown = sum(1 for d in decls if d.status == "unknown")
    beyond = sorted({
        ax for d in decls for ax in d.axioms
        if ax not in AXIOM_WHITELIST and ax != SORRY_AXIOM
    })
    tagged = [(d.name, ";".join(d.tags)) for d in decls if d.tags]
    print(
        f"audited {len(decls)} declaration(s) across "
        f"{len(snapshot.modules)} module(s)"
        + (f" [toolchain {snapshot.toolchain}]" if snapshot.toolchain else "")
    )
    print(f"  sorry'd (transitive sorryAx): {sorried}")
    print(f"  unknown (no evidence): {unknown}")
    print(
        f"  axioms beyond whitelist: {len(beyond)}"
        + (f" ({', '.join(beyond)})" if beyond else "")
    )
    print(f"  deception-tagged: {len(tagged)}")
    for name, tags in tagged:
        print(f"    {name}: {tags}")
    if snapshot.failures:
        print(f"  failures ({len(snapshot.failures)}):")
        for failure in snapshot.failures:
            print(f"    - {failure}")
    print(f"saved {path}")
    if not decls and snapshot.failures:
        # Nothing audited at all — honest absence, but a failing exit so
        # scripts don't mistake an empty snapshot for a clean one.
        raise SystemExit(1)


def _run_audit_diff(args) -> None:
    from marathon.audit.engine import (
        DIFF_KEYS, LATEST_NAME, PREVIOUS_NAME, diff_snapshots, load_snapshot,
    )

    repo_dir: Path = args.repo_dir.resolve()
    new = load_snapshot(repo_dir, LATEST_NAME)
    if new is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    old = load_snapshot(repo_dir, PREVIOUS_NAME)
    if old is None:
        print(
            "no previous audit snapshot to diff against "
            "(only one run recorded so far)"
        )
        raise SystemExit(1)
    diff = diff_snapshots(old, new)
    print(f"audit diff: {old.created_at} -> {new.created_at}")
    for warning in diff.get("warnings", []):
        print(f"  WARNING: {warning}")
    total = 0
    for key in DIFF_KEYS:
        names = diff.get(key, [])
        total += len(names)
        print(f"  {key}: {len(names)}")
        for name in names:
            print(f"    {name}")
    if total == 0:
        print("  no per-declaration changes")


def _run_audit_show(args) -> None:
    from marathon.audit.engine import load_snapshot

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    by_name = snapshot.by_name()
    decl = by_name.get(args.decl)
    if decl is None:
        # Convenience: a unique dotted-suffix match also resolves.
        matches = [
            d for name, d in by_name.items()
            if name.endswith("." + args.decl)
        ]
        if len(matches) == 1:
            decl = matches[0]
        elif matches:
            print(f"ambiguous suffix {args.decl!r}; candidates:")
            for d in matches:
                print(f"  {d.name}")
            raise SystemExit(1)
    if decl is None:
        print(f"declaration {args.decl!r} not in latest snapshot "
              f"({len(by_name)} decl(s) audited)")
        raise SystemExit(1)
    print(f"name: {decl.name}")
    print(f"kind: {decl.kind}")
    print(f"module: {decl.module}")
    print(f"status: {decl.status}")
    print(f"type: {decl.type_pp if decl.type_pp is not None else '-'}")
    print(f"value: {decl.value_pp if decl.value_pp is not None else '-'}")
    print(f"fingerprint_type: {decl.fingerprint_type or '-'}")
    print(f"fingerprint_value: {decl.fingerprint_value or '-'}")
    print(f"cone: {', '.join(decl.cone) or '-'}")
    print(f"axioms: {', '.join(decl.axioms) or '-'}")
    sorry = "-" if decl.has_sorry is None else str(decl.has_sorry).lower()
    print(f"has_sorry: {sorry}")
    print(f"tags: {';'.join(decl.tags) or '-'}")
    if decl.reason is not None:
        print(f"reason: {decl.reason}")


def _run_audit_tiers(args) -> None:
    from marathon.audit.engine import derive_modules, load_snapshot
    from marathon.audit.trust import TIER_ORDER, compute_tiers
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    ledger = Ledger.for_repo(repo_dir)
    results = compute_tiers(snapshot, ledger)
    if args.target is not None:
        modules, failures = derive_modules(repo_dir, args.target)
        for failure in failures:
            print(f"  warning: {failure}")
        if not modules:
            print(f"no auditable .lean modules under {args.target}")
            raise SystemExit(1)
        wanted = set(modules)
        by_name = snapshot.by_name()
        results = [
            r for r in results if by_name[r.decl_name].module in wanted
        ]
    if not results:
        print("no declarations to report")
        return
    print(
        f"trust tiers: {len(results)} declaration(s), computed on read "
        f"(snapshot {snapshot.created_at}"
        + (f", toolchain {snapshot.toolchain}" if snapshot.toolchain else "")
        + ")"
    )
    name_w = max(len(r.decl_name) for r in results)
    for r in sorted(results, key=lambda r: r.decl_name):
        line = f"  {r.decl_name:<{name_w}}  {r.tier:<7}"
        if r.qualifiers:
            line += "  " + ",".join(r.qualifiers)
        print(line)
    counts = {tier: 0 for tier in TIER_ORDER}
    for r in results:
        counts[r.tier] += 1
    print("summary: " + "  ".join(
        f"{tier}={counts[tier]}" for tier in TIER_ORDER
    ))


def _run_audit_backfill(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.trust import apply_backfill, plan_backfill
    from marathon.ledger import Ledger
    from marathon.review.config import load_config

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    cfg = load_config(repo_dir)
    ledger = Ledger.for_repo(repo_dir)
    chapters = [args.chapter] if args.chapter is not None else None
    plan = plan_backfill(cfg, snapshot, ledger, chapters=chapters)
    if plan.items:
        print(f"would pin {len(plan.items)} declaration(s) as T2 "
              f"(toolchain {snapshot.toolchain}):")
        name_w = max(len(i.decl_name) for i in plan.items)
        for item in plan.items:
            print(
                f"  {item.decl_name:<{name_w}}  #{item.issue_num}  "
                f"{item.fingerprint_type[:12]}…  cone={item.cone_size}"
            )
    else:
        print("nothing to pin")
    if plan.skipped:
        print(f"skipped {len(plan.skipped)} citation(s):")
        for skip in plan.skipped:
            print(f"  {skip.name} (#{skip.issue_num}): {skip.reason}")
    if not plan.items:
        return
    if not args.attest:
        print(
            "dry run — nothing written. These pins assert 'current main "
            "matches what I verified back then'; re-run with --attest to "
            "make that attestation and write the T2 events."
        )
        return
    written = apply_backfill(ledger, snapshot, plan)
    print(
        f"wrote {written} T2 verdict event(s) (source=backfill) to "
        f"{ledger.db_path}"
    )


def _run_audit_invalidations(args) -> None:
    from marathon.audit.engine import LATEST_NAME, PREVIOUS_NAME, load_snapshot
    from marathon.audit.invalidate import (
        compute_invalidations, notify_invalidations,
    )
    from marathon.ledger import Ledger
    from marathon.review.config import load_config

    repo_dir: Path = args.repo_dir.resolve()
    new = load_snapshot(repo_dir, LATEST_NAME)
    if new is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    old = load_snapshot(repo_dir, PREVIOUS_NAME)  # may be None (first run)
    ledger = Ledger.for_repo(repo_dir)
    report = compute_invalidations(old, new, ledger)
    if not args.apply:
        print(notify_invalidations(load_config(repo_dir), report, apply=False))
        return
    cfg = load_config(repo_dir)
    print(notify_invalidations(cfg, report, apply=True))


def _run_audit_repin(args) -> None:
    from marathon.audit.engine import LATEST_NAME, PREVIOUS_NAME, load_snapshot
    from marathon.audit.invalidate import compute_invalidations
    from marathon.audit.trust import TrustError, _cone_pins, record_spec_verdict
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir, LATEST_NAME)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    previous = load_snapshot(repo_dir, PREVIOUS_NAME)
    ledger = Ledger.for_repo(repo_dir)
    report = compute_invalidations(previous, snapshot, ledger)
    by_name = snapshot.by_name()
    all_events = ledger.all_decl_verdict_events()

    # Candidate (decl, change-kind, claimed-tier) re-pins. type-text /
    # cone come from per-decl invalidations; toolchain-only from the
    # separate stale list (a bump, not a meaning change). Decls absent/
    # unknown in the CURRENT snapshot are refused below, never re-pinned.
    candidates: list[tuple[str, str, str]] = []
    for inv in report.invalidations:
        kind = "cone" if inv.cause == "cone-changed" else (
            "absent" if inv.cause == "absent" else "type-text"
        )
        candidates.append((inv.decl_name, kind, inv.tier_claimed))
    for name in report.stale_toolchain:
        claimed = _claimed_tier(all_events.get(name, []))
        candidates.append((name, "toolchain-only", claimed))

    # --decl filter: exact, else unique dotted-suffix (same rule as
    # `audit show`). An unresolvable/ambiguous selector is a hard error
    # rather than a silent no-op.
    if args.decl is not None:
        wanted: set[str] = set()
        for raw in args.decl:
            resolved = _resolve_repin_target(raw, [c[0] for c in candidates])
            if resolved is None:
                print(
                    f"--decl {raw!r}: no stale/changed verdict matches "
                    "(nothing to re-pin for it)"
                )
                raise SystemExit(1)
            wanted.add(resolved)
        candidates = [c for c in candidates if c[0] in wanted]

    if not candidates:
        print("no stale or changed verdicts to re-pin")
        return

    refused: list[tuple[str, str]] = []
    pinnable: list[tuple[str, str, str]] = []
    for name, kind, claimed in candidates:
        decl = by_name.get(name)
        if kind == "absent" or decl is None or decl.is_unknown \
                or decl.fingerprint_type is None:
            refused.append((name, "absent or unknown in the current snapshot"))
            continue
        # A re-pin pins the decl's CURRENT cone, so a cone member that
        # vanished/never-elaborated makes the decl unpinnable too — the
        # 'refuse decls absent/unknown' ruling extends to an absent cone.
        # Pre-check here (mirroring record_spec_verdict's guard) so the
        # command refuses cleanly per-decl instead of crashing mid-batch
        # on TrustError after the table prints (which also left earlier
        # decls in the batch partially re-pinned). Naming the missing
        # member fixes the MINOR old-fp==new-fp 'cone' row: the operator
        # sees WHY it cannot be re-pinned, not a fake clean diff.
        _pins, unpinnable = _cone_pins(decl, by_name)
        if unpinnable:
            refused.append((
                name,
                "cone member(s) absent or unknown — cannot re-pin: "
                + ", ".join(unpinnable),
            ))
        else:
            pinnable.append((name, kind, claimed))

    print(f"re-pin table (snapshot {snapshot.created_at}, toolchain "
          f"{snapshot.toolchain}):")
    if pinnable:
        name_w = max(len(n) for n, _, _ in pinnable)
        for name, kind, claimed in pinnable:
            old_fp = _old_pinned_fingerprint(all_events.get(name, []))
            new_fp = by_name[name].fingerprint_type or ""
            print(
                f"  {name:<{name_w}}  {claimed}  "
                f"{(old_fp[:12] + '…') if old_fp else '-':<13} → "
                f"{(new_fp[:12] + '…') if new_fp else '-':<13}  {kind}"
            )
    else:
        print("  (nothing pinnable)")
    if refused:
        print(f"refused {len(refused)} decl(s):")
        for name, why in refused:
            print(f"  {name}: {why}")

    if not pinnable:
        return
    if not args.yes:
        print(
            "dry run — nothing written. `audit repin --yes` is the bulk "
            "trust override: it asserts you re-checked these decls (or "
            "accept the changes sight-unseen) and appends new "
            "source='repin' verdict events pinned to current main."
        )
        return
    written = 0
    late_refused: list[tuple[str, str]] = []
    for name, _kind, claimed in pinnable:
        # The cone pre-check above should already have refused any
        # unpinnable decl, so record_spec_verdict cannot raise here in
        # practice. Guard anyway: a TrustError mid-batch must refuse THIS
        # decl, not abort the loop and leave the already-written earlier
        # decls as a partially-applied amnesty (defense in depth for the
        # append-only / 'refuse, never crash' rulings).
        try:
            record_spec_verdict(
                ledger, name, snapshot,
                tier=claimed,
                issue_num=_pinned_issue(all_events.get(name, [])),
                source="repin",
                notes="re-pin amnesty: operator re-checked or accepts current "
                      "main sight-unseen",
            )
        except TrustError as e:
            late_refused.append((name, str(e)))
            continue
        written += 1
    print(
        f"wrote {written} source='repin' verdict event(s) to "
        f"{ledger.db_path} (old events preserved — append-only)"
    )
    if late_refused:
        print(f"refused {len(late_refused)} decl(s) at write time:")
        for name, why in late_refused:
            print(f"  {name}: {why}")


def _claimed_tier(events) -> str:
    """Highest live verdict tier across an event log (T3 before T2),
    defaulting to T2 — used to re-pin at the same rung the human
    attested."""
    from marathon.audit.invalidate import _live_verdict

    event = _live_verdict(events)
    return event.tier_claimed if event is not None else "T2"


def _old_pinned_fingerprint(events) -> str:
    from marathon.audit.invalidate import _live_verdict

    event = _live_verdict(events)
    return (event.fingerprint_type or "") if event is not None else ""


def _pinned_issue(events):
    from marathon.audit.invalidate import _live_verdict

    event = _live_verdict(events)
    return event.issue_num if event is not None else None


def _resolve_repin_target(raw: str, names: list[str]):
    """Resolve a ``--decl`` selector against the candidate decl names:
    exact match, else a UNIQUE dotted-suffix match. None when neither
    (caller errors out — a re-pin must never guess which decl)."""
    if raw in names:
        return raw
    suffix = [n for n in names if n.endswith("." + raw)]
    return suffix[0] if len(suffix) == 1 else None


def _resolve_snapshot_decl(decl: str, snapshot):
    """Resolve a CLI decl selector against a snapshot: exact match, else a
    UNIQUE dotted-suffix match (same convenience as `audit show`). Returns
    the resolved fully-qualified name, or None when ambiguous/absent — the
    caller prints the candidates and exits, never guessing."""
    by_name = snapshot.by_name()
    if decl in by_name:
        return decl
    matches = sorted(n for n in by_name if n.endswith("." + decl))
    if len(matches) == 1:
        return matches[0]
    if matches:
        print(f"ambiguous suffix {decl!r}; candidates:")
        for name in matches:
            print(f"  {name}")
        raise SystemExit(1)
    return None


def _run_audit_kernel(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.kernel import compute_kernel

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    resolved = _resolve_snapshot_decl(args.decl, snapshot)
    # An unresolved selector still produces an honest kernel (target
    # reported unresolved) — but if it isn't even a known suffix, say so.
    target = resolved if resolved is not None else args.decl
    kernel = compute_kernel(target, snapshot)
    print(f"trust kernel of {kernel.target}")
    print(
        f"  size: {kernel.size_decls} local def(s), "
        f"{kernel.size_loc} pinned-pp line(s)"
    )
    print("  definitions you must read:")
    if kernel.members:
        for member in kernel.members:
            print(f"    {member.name}  ({member.kind})")
    else:
        print("    none — phrased entirely in trusted vocabulary")
    if kernel.local_lemmas:
        print("  local lemmas referenced (statements not read):")
        for name in kernel.local_lemmas:
            print(f"    {name}")
    if kernel.unresolved:
        print("  unresolved cone references (no audit evidence):")
        for name in kernel.unresolved:
            print(f"    {name}")


def _run_audit_card(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.spec_card import SpecCard
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    resolved = _resolve_snapshot_decl(args.decl, snapshot)
    target = resolved if resolved is not None else args.decl
    ledger = Ledger.for_repo(repo_dir)
    card = SpecCard.from_snapshot(target, snapshot, ledger)
    print(card.render_markdown())


def _run_audit_spec_audit(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.spec_store import generate_spec_audits
    from marathon.formalization import module_from_file_path  # noqa: F401
    from marathon.ledger import Ledger

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)

    # Resolve the set of declarations to render.
    if args.decl:
        names = []
        for sel in args.decl:
            r = _resolve_snapshot_decl(sel, snapshot)
            names.append(r if r is not None else sel)
    else:
        ok = [d for d in snapshot.decls if not getattr(d, "is_unknown", False)]
        if args.target:
            # Module path under the target folder: dotted module ->
            # repo-relative slashes, prefix-matched on the target.
            tgt = str(args.target).strip("/")
            tgt_mod = tgt.replace("/", ".")
            ok = [d for d in ok if (d.module or "").startswith(tgt_mod)]
        names = [d.name for d in ok]
        if args.limit is not None:
            names = names[: args.limit]

    if not names:
        print("no declarations matched (check --target / --decl / the snapshot)")
        raise SystemExit(1)

    ledger = Ledger.for_repo(repo_dir)
    total = len(names)
    print(
        f"spec-audit: rendering {total} declaration(s) "
        f"(one firewalled Claude call each; bills Claude Max)…"
    )
    state = {"n": 0, "ok": 0}

    def progress(name: str, ok_flag: bool) -> None:
        state["n"] += 1
        if ok_flag:
            state["ok"] += 1
        mark = "✓" if ok_flag else "·"
        print(f"  [{state['n']}/{total}] {mark} {name}")

    generate_spec_audits(
        repo_dir, snapshot, ledger, names, progress=progress
    )
    print(
        f"spec-audit: cached {state['ok']}/{total} rendering(s) -> "
        f".marathon/audit/spec_audits.json"
    )


def _run_audit_probe(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.audit.probes import probes_for_decl, run_probes

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)
    resolved = _resolve_snapshot_decl(args.decl, snapshot)
    if resolved is None:
        print(
            f"declaration {args.decl!r} not in latest snapshot "
            f"({len(snapshot.by_name())} decl(s) audited)"
        )
        raise SystemExit(1)
    decl = snapshot.by_name()[resolved]
    kinds = (
        tuple(ProbeKind(k) for k in args.kind) if args.kind else None
    )
    probes = probes_for_decl(decl, kinds=kinds)
    if not probes:
        # The kinds that auto-generate (unfolding/sanity) don't apply to
        # this decl — say so honestly rather than print nothing.
        print(
            f"no auto-generatable probes for {resolved} (kind {decl.kind!r}): "
            "unfolding applies to def/abbrev/instance, sanity to "
            "structure/class. (shrink_certificate needs a spec-auditor "
            "obligation, not a decl.)"
        )
        return
    print(f"generated {len(probes)} probe(s) for {resolved} (kind {decl.kind}):")
    for probe in probes:
        marker = " [needs witness]" if probe.needs_witness else ""
        print(f"\n--- {probe.kind.value} probe{marker} ---")
        print(probe.source.rstrip())
    if not args.run:
        print(
            "\n(generated only — pass --run to build them with `lake env "
            "lean` and report outcomes.)"
        )
        return
    report = run_probes(repo_dir, probes, timeout=args.timeout)
    print()
    print(report.render())
    # A high-signal finding (a failing unfolding/sanity probe, or a rejected
    # shrink) is a nonzero exit so scripts notice — an `error` (toolchain
    # trouble) or `needs_witness` is honest absence of evidence, not a fail.
    if report.findings:
        raise SystemExit(1)


def _run_audit_vacuity_probe(args) -> None:
    from marathon.audit.engine import load_snapshot
    from marathon.probes_aristotle import run_probes, select_targets

    repo_dir: Path = args.repo_dir.resolve()
    snapshot = load_snapshot(repo_dir)
    if snapshot is None:
        print("no latest audit snapshot; run `marathon audit run` first")
        raise SystemExit(1)

    # Resolve every selector against the snapshot (exact / unique suffix);
    # an unresolved one is a hard error, never a silent no-op.
    resolved_names: list[str] = []
    for raw in args.decl:
        resolved = _resolve_snapshot_decl(raw, snapshot)
        if resolved is None:
            print(f"declaration {raw!r} not in latest snapshot")
            raise SystemExit(1)
        resolved_names.append(resolved)

    if args.dry_run:
        # Plan only — generate goals, report the governor's decision, spend
        # nothing. (Dedup index is still consulted so the dry run is honest
        # about what a real run would skip.)
        plan = select_targets(
            snapshot, resolved_names, repo_dir, max_probes=args.max_probes
        )
        print(
            f"DRY RUN — no Aristotle budget spent. cap={args.max_probes}; "
            f"would submit {len(plan.to_run)} probe(s):"
        )
        for goal in plan.to_run:
            print(f"  {goal.decl_name}  (goal-hash {goal.goal_hash[:8]})")
        if plan.skipped_dedup:
            print(
                "  skipped (already submitted): "
                + ", ".join(plan.skipped_dedup)
            )
        if plan.skipped_cap:
            print(f"  skipped (over cap): {', '.join(plan.skipped_cap)}")
        if plan.skipped_unprobeable:
            print(
                "  skipped (not a probeable theorem): "
                + ", ".join(plan.skipped_unprobeable)
            )
        # Print one generated goal so the operator can eyeball it.
        if plan.to_run:
            sample = plan.to_run[0]
            print(
                f"\n--- generated goal for {sample.decl_name} "
                f"({sample.relpath}) ---"
            )
            print(sample.lean_source)
        return

    print(
        f"!! vacuity probe — spending real Aristotle budget !! "
        f"cap={args.max_probes}"
    )
    plan, outcomes = asyncio.run(
        run_probes(
            repo_dir,
            snapshot,
            resolved_names,
            max_probes=args.max_probes,
            polling_interval=args.polling_interval,
        )
    )
    if plan.skipped_dedup:
        print(f"skipped (already submitted): {', '.join(plan.skipped_dedup)}")
    if plan.skipped_cap:
        print(f"skipped (over cap, not spent): {', '.join(plan.skipped_cap)}")
    if plan.skipped_unprobeable:
        print(
            "skipped (not a probeable theorem): "
            + ", ".join(plan.skipped_unprobeable)
        )
    broken = [o for o in outcomes if o.broken_spec]
    for outcome in outcomes:
        print(f"  {outcome.summary}")
    if broken:
        print(
            f"\n{len(broken)} BROKEN-SPEC finding(s) written to "
            ".marathon/audit/vacuity/findings/ — review and reject "
            "manually (no auto-rejection filed this phase)."
        )
        raise SystemExit(2)
    print(
        "no broken specs found (all probes inconclusive — WEAK negative "
        "evidence, no tier change)."
    )
