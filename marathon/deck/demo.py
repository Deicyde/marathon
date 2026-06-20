"""Self-contained DEMO data + handlers for the deck (``marathon deck --demo``).

Lets anyone experience the full subdeck flow — pick a ready issue, swipe each
declaration (verify / reject-with-note / defer), reach the confirm-and-ship
summary — on hand-authored synthetic cards, with NO consumer repo, NO audit
snapshot, NO GitHub, and a SIMULATED ship that fires nothing irreversible
(no ``cmd_verify`` / ``cmd_reject``, no Aristotle). The per-declaration
verdicts live in a process-local store that resets when the server restarts.

The functions here mirror the real ``cards`` / ``subdeck`` API contract
exactly, so the frontend is identical in demo and live modes.
"""

from __future__ import annotations

import threading
from typing import Optional

# Reserved store key for the subdeck's ONE informal-text approval verdict (the
# leading approval card). Mirrors the live contract: never a real decl name, so
# it stays out of the reviewable set / progress / summary / ship.
from marathon.deck.subdeck import INFORMAL_KEY, MISSING_KEY

# --- demo content -----------------------------------------------------------
# Each issue is a "subdeck" of declarations. Tiers/evidence are fixed (in live
# mode they are computed from the audit snapshot). One declaration carries a
# `sorry` and a T0 tier so the reject → FIX-list flow is demonstrable.


def _kernel(*members):
    return [
        {"name": n, "kind": k, "type_pp": t, "value_pp": v}
        for (n, k, t, v) in members
    ]


def _evidence(*, sorry=False, axioms=(), tags=()):
    return {
        "axioms_beyond_whitelist": list(axioms),
        "sorry": sorry,
        "deception_tags": list(tags),
    }


def _decl(
    name, kind, tier, source_block, *,
    statement_pp=None, kernel=None, evidence=None, informal=None,
    in_code=True, api=False, api_reason=None,
    surfaced=False, surfaced_reason=None,
):
    return {
        "name": name,
        "kind": kind,
        "tier": tier,
        "qualifiers": [],
        "source_block": source_block,
        "source_file": "Demo/Algebra.lean",
        "statement_pp": statement_pp,
        "kernel": kernel or [],
        "evidence": evidence or _evidence(),
        "informal_rendering": informal,
        "informal_is_llm": informal is not None,
        "semantic_delta": None,
        # request-1 / skip-API mirror fields (see the live cards.DeclView).
        "in_code": in_code,
        "api": api,
        "api_reason": api_reason,
        # reconciliation mirror fields (see the live cards.DeclView): a
        # ``surfaced`` decl is auto-added because it lives in a cited decl's
        # namespace but is NOT cited by the issue body.
        "surfaced": surfaced,
        "surfaced_reason": surfaced_reason,
    }


_DEMO_ISSUES = [
    {
        "id": 9001,
        "title": "Demo · Bundled monoid homomorphism (Lee-style)",
        "chapter": 0,
        # The issue's ONE top-level informal statement (the whole result) — what
        # the leading approval card shows (NOT the per-decl renderings).
        "informal_statement": "A monoid homomorphism $f : M \\to N$ is a function "
        "preserving the identity ($f(1) = 1$) and multiplication "
        "($f(a \\cdot b) = f(a) \\cdot f(b)$); it therefore preserves powers and "
        "composes with other homomorphisms.",
        "decls": [
            _decl(
                "Demo.MonoidHom", "structure", "T1",
                "/-- A **monoid homomorphism** bundles a map with proofs that\n"
                "it preserves the identity and multiplication. -/\n"
                "structure Demo.MonoidHom (M N : Type*) [Monoid M] [Monoid N] where\n"
                "  toFun     : M → N\n"
                "  map_one'  : toFun 1 = 1\n"
                "  map_mul'  : ∀ a b, toFun (a * b) = toFun a * toFun b",
                statement_pp="(M N : Type u_1) → [inst : Monoid M] → [inst : Monoid N] → Type u_1",
                kernel=_kernel(
                    ("Monoid", "class", "Type u → Type u", None),
                ),
                informal="A bundled monoid homomorphism between monoids $M$ and $N$: a function $M \\to N$ together with proofs that it sends $1$ to $1$ and $a \\cdot b$ to $f(a) \\cdot f(b)$.",
            ),
            _decl(
                "Demo.MonoidHom.map_pow", "theorem", "T1",
                "/-- A monoid homomorphism preserves powers. -/\n"
                "theorem Demo.MonoidHom.map_pow (f : Demo.MonoidHom M N) (a : M) :\n"
                "    ∀ n, f.toFun (a ^ n) = f.toFun a ^ n := by\n"
                "  intro n\n"
                "  induction n with\n"
                "  | zero => simpa using f.map_one'\n"
                "  | succ k ih => simp [pow_succ, f.map_mul', ih]",
                statement_pp="∀ {M N} [Monoid M] [Monoid N] (f : Demo.MonoidHom M N) (a : M) (n : ℕ), f.toFun (a ^ n) = f.toFun a ^ n",
                kernel=_kernel(
                    ("Demo.MonoidHom", "structure", "(M N : Type*) → …", None),
                ),
                informal="For a monoid homomorphism $f$, $f(a^n) = f(a)^n$ for every $n \\in \\mathbb{N}$.",
            ),
            _decl(
                "Demo.MonoidHom.comp", "def", "T0",
                "/-- The composition of two monoid homomorphisms.\n\n"
                "NOTE: the identity-preservation field is left as `sorry`. -/\n"
                "def Demo.MonoidHom.comp (g : Demo.MonoidHom N P) (f : Demo.MonoidHom M N) :\n"
                "    Demo.MonoidHom M P where\n"
                "  toFun     := g.toFun ∘ f.toFun\n"
                "  map_one'  := by sorry\n"
                "  map_mul'  := by intro a b; simp [f.map_mul', g.map_mul']",
                statement_pp="{M N P} → … → Demo.MonoidHom N P → Demo.MonoidHom M N → Demo.MonoidHom M P",
                kernel=_kernel(
                    ("Demo.MonoidHom", "structure", "(M N : Type*) → …", None),
                ),
                evidence=_evidence(sorry=True),
                informal="The composite $g \\circ f$ of monoid homomorphisms, again a monoid homomorphism. (The proof that it preserves $1$ is currently a `sorry`.)",
            ),
        ],
    },
    {
        "id": 9002,
        "title": "Demo · Even natural numbers",
        "chapter": 0,
        "informal_statement": "A natural number is even when it equals $2k$ for "
        "some $k$; the sum of two even numbers is even.",
        "decls": [
            _decl(
                "Demo.IsEven", "def", "T1",
                "/-- A natural number is **even** when it is twice some `k`. -/\n"
                "def Demo.IsEven (n : ℕ) : Prop := ∃ k, n = 2 * k",
                statement_pp="ℕ → Prop",
                informal="$n$ is even iff there exists $k$ with $n = 2k$.",
            ),
            _decl(
                "Demo.isEven_add", "theorem", "T1",
                "/-- The sum of two even numbers is even. -/\n"
                "theorem Demo.isEven_add {m n : ℕ} (hm : Demo.IsEven m) (hn : Demo.IsEven n) :\n"
                "    Demo.IsEven (m + n) := by\n"
                "  obtain ⟨a, rfl⟩ := hm\n"
                "  obtain ⟨b, rfl⟩ := hn\n"
                "  exact ⟨a + b, by ring⟩",
                statement_pp="∀ {m n : ℕ}, Demo.IsEven m → Demo.IsEven n → Demo.IsEven (m + n)",
                kernel=_kernel(("Demo.IsEven", "def", "ℕ → Prop", "fun n => ∃ k, n = 2 * k")),
                informal="If $m$ and $n$ are even, so is $m + n$.",
            ),
        ],
    },
    {
        # Demonstrates BOTH new features: a clearly-MAIN textbook result with a
        # mix of easy-API decls (an instance + an `_apply` projection lemma)
        # the skip-API toggle auto-approves, plus a STALE CITATION (a name the
        # issue cites that is absent from the code → in_code=false, no source).
        "id": 9003,
        "title": "Demo · Linear map API (skip-API + stale citation)",
        "chapter": 0,
        "decls": [
            _decl(
                "Demo.IsLinear", "def", "T1",
                "/-- A function between modules is **linear** when it preserves\n"
                "addition and scalar multiplication. -/\n"
                "def Demo.IsLinear (f : M → N) : Prop :=\n"
                "  (∀ a b, f (a + b) = f a + f b) ∧ (∀ (c : R) a, f (c • a) = c • f a)",
                statement_pp="(M → N) → Prop",
                informal="$f$ is linear iff it is additive and homogeneous.",
            ),
            # Easy-API instance — skip-API auto-approves it.
            _decl(
                "Demo.instFunLikeLM", "instance", "T1",
                "/-- A bundled linear map is `FunLike`. -/\n"
                "instance Demo.instFunLikeLM : FunLike (Demo.LM R M N) M N where\n"
                "  coe f := f.toFun\n"
                "  coe_injective' := by rintro ⟨⟩ ⟨⟩ rfl; rfl",
                statement_pp="{R M N} → … → FunLike (Demo.LM R M N) M N",
                informal="The `FunLike` instance for bundled linear maps.",
                api=True, api_reason="instance",
            ),
            # Easy-API projection (_apply) lemma — skip-API auto-approves it.
            _decl(
                "Demo.LM.coe_apply", "theorem", "T1",
                "/-- A bundled linear map applies its `toFun`. -/\n"
                "@[simp] theorem Demo.LM.coe_apply (f : Demo.LM R M N) (a : M) :\n"
                "    f a = f.toFun a := rfl",
                statement_pp="∀ {R M N} … (f : Demo.LM R M N) (a : M), f a = f.toFun a",
                informal="Applying a bundled linear map equals applying its underlying function.",
                api=True, api_reason="projection/helper lemma (_apply)",
            ),
            # A STALE CITATION — the issue names a decl that no longer exists in
            # the code (renamed/deleted): in_code=false, no source, tier '-'.
            # The reconciliation DROPS it to the footnote (never a swipe card).
            _decl(
                "Demo.IsLinear.smul_old", "theorem", "-", None,
                statement_pp=None, informal=None, in_code=False,
            ),
            # A SURFACED decl — present in the CODE under the cited
            # ``Demo.IsLinear`` namespace but NOT cited by the issue body. The
            # reconciliation auto-adds it as a normal reviewable card
            # (surfaced=true) so the deck matches the code, not the stale body.
            _decl(
                "Demo.IsLinear.comp", "theorem", "T1",
                "/-- The composition of two linear maps is linear. -/\n"
                "theorem Demo.IsLinear.comp {f g : M → N}\n"
                "    (hf : Demo.IsLinear f) (hg : Demo.IsLinear g) :\n"
                "    Demo.IsLinear (g ∘ f) := by\n"
                "  refine ⟨fun a b => ?_, fun c a => ?_⟩ <;>\n"
                "    simp [Function.comp, hf.1, hf.2, hg.1, hg.2]",
                statement_pp="∀ {M N} … {f g : M → N}, Demo.IsLinear f → "
                "Demo.IsLinear g → Demo.IsLinear (g ∘ f)",
                informal="The composite of two linear maps is linear.",
                surfaced=True,
                surfaced_reason="in Demo.IsLinear namespace, not cited by the issue",
            ),
        ],
    },
]

_BY_ID = {iss["id"]: iss for iss in _DEMO_ISSUES}

# Process-local per-(issue, decl) verdict store: {issue_id: {decl: {verdict, note}}}.
# Resets when the server restarts (demo state is intentionally ephemeral).
_LOCK = threading.Lock()
_VERDICTS: dict[int, dict[str, dict]] = {}

# verify/reject count as REVIEWED; defer does not (mirrors the live contract).
_REVIEWED = {"verify", "reject"}
_WIRE = {"verify": "verified", "reject": "rejected", "defer": "deferred"}


def _informal_slot(issue_id: int, decls) -> Optional[dict]:
    """The demo subdeck's informal-approval slot — mirrors
    :func:`marathon.deck.subdeck._informal_slot`. Carries the issue's ONE
    top-level informal statement (``informal_statement`` on the demo issue;
    else the headline decl's rendering) + the stored approval verdict (reserved
    :data:`INFORMAL_KEY`). ``None`` when there is no top-level informal text."""
    iss = _BY_ID.get(issue_id) or {}
    statement = iss.get("informal_statement")
    is_llm = bool(statement)
    if not statement:
        head = decls[0] if decls else None
        if head and head.get("informal_is_llm") and head.get("informal_rendering"):
            statement, is_llm = head["informal_rendering"], True
    if not statement:
        return None
    rec = _VERDICTS.get(issue_id, {}).get(INFORMAL_KEY) or {}
    stored = rec.get("verdict")
    return {
        "is_llm": is_llm,
        "verdict": _WIRE.get(stored, stored),
        "note": rec.get("note"),
        "statement": statement,
    }


def reset() -> None:
    with _LOCK:
        _VERDICTS.clear()


def _worst_tier(decls) -> str:
    order = ("UNKNOWN", "T0", "T1", "T2", "T3")
    seen = [d["tier"] for d in decls if d["tier"] in order]
    return min(seen, key=order.index) if seen else "-"


def _decl_card(issue_id: int, d: dict) -> dict:
    rec = _VERDICTS.get(issue_id, {}).get(d["name"])
    verdict = _WIRE.get(rec["verdict"]) if rec else None
    note = rec.get("note") if rec else None
    return {**d, "verdict": verdict, "note": note}


def _summary(issue_id: int, decls) -> dict:
    recs = _VERDICTS.get(issue_id, {})
    kept, rejected, deferred = [], [], []
    for d in decls:
        rec = recs.get(d["name"])
        if not rec:
            continue
        if rec["verdict"] == "verify":
            kept.append(d["name"])
        elif rec["verdict"] == "reject":
            rejected.append({"name": d["name"], "note": rec.get("note") or ""})
        elif rec["verdict"] == "defer":
            deferred.append(d["name"])
    return {"kept": kept, "rejected": rejected, "deferred": deferred}


def _progress(issue_id: int, decls) -> dict:
    recs = _VERDICTS.get(issue_id, {})
    reviewed = sum(
        1 for d in decls
        if (recs.get(d["name"]) or {}).get("verdict") in _REVIEWED
    )
    return {"reviewed": reviewed, "total": len(decls)}


def _ordered_cards(issue_id: int, decls) -> list:
    # Unreviewed first, then by name (deterministic), mirroring the live deck.
    cards = [_decl_card(issue_id, d) for d in decls]
    cards.sort(key=lambda c: (c["verdict"] in ("verified", "rejected"), c["name"]))
    return cards


def _next_unreviewed(issue_id: int, decls) -> Optional[dict]:
    for c in _ordered_cards(issue_id, decls):
        if c["verdict"] not in ("verified", "rejected"):
            return c
    return None


def _partition(issue_id: int, decls, skip_api: bool):
    """``(main, api)`` decl lists mirroring subdeck.build_subdeck: when
    skip_api, API decls WITHOUT an explicit human reject go to ``api`` (ship
    auto-approves them); everything else (incl. a human-rejected API decl) is
    MAIN. When not skip_api, every decl is MAIN and ``api`` is empty."""
    if not skip_api:
        return list(decls), []
    recs = _VERDICTS.get(issue_id, {})
    main, api = [], []
    for d in decls:
        rejected = (recs.get(d["name"]) or {}).get("verdict") == "reject"
        if d.get("api") and not rejected:
            api.append(d)
        else:
            main.append(d)
    return main, api


# --- handlers (mirror cards.build_queue / subdeck.* ) -----------------------


def demo_queue() -> dict:
    """The issue picker: every demo issue, all 'ready' (no dep gate in demo)."""
    cards = []
    for iss in _DEMO_ISSUES:
        # Progress/tier are over the REVIEWABLE set (stale citations are footnote
        # entries, never cards), so a stale-cited issue can still complete.
        reviewable, _stale, _rec = _reconcile(iss["decls"])
        prog = _progress(iss["id"], reviewable)
        done = prog["reviewed"] >= prog["total"] and prog["total"] > 0
        cards.append({
            "id": iss["id"],
            "decl": reviewable[0]["name"] if reviewable else "",
            "chapter": iss["chapter"],
            "tier": _worst_tier(reviewable),
            "tier_qualifiers": [],
            "ready": not done,  # a fully-reviewed issue drops out of the picker
            "blocked_reason": "shipped/reviewed (demo)" if done else None,
            "title": iss["title"],
        })
    return {"cards": cards, "building": 0, "landed_today": 0}


def _reconcile(decls):
    """Mirror subdeck.build_subdeck's reconciliation on the demo decls:
    ``(reviewable, stale_citations, reconcile)``. A stale citation
    (``in_code=false``) is DROPPED to the footnote (never a card); a
    ``surfaced`` decl is added to the reviewable set and listed in
    ``reconcile.surfaced``. PURE — reads the static decls, writes nothing."""
    reviewable = [d for d in decls if d.get("in_code", True)]
    stale = [d for d in decls if not d.get("in_code", True)]
    stale_citations = [{"name": d["name"]} for d in stale]
    reconcile = {
        "dropped": [d["name"] for d in stale],
        "surfaced": [d["name"] for d in reviewable if d.get("surfaced")],
    }
    return reviewable, stale_citations, reconcile


def demo_subdeck(issue_id: int, *, skip_api: bool = False) -> dict:
    iss = _BY_ID.get(issue_id)
    if iss is None:
        return {"error": f"no demo issue #{issue_id}"}
    with _LOCK:
        decls = iss["decls"]
        # RECONCILE first (pure): stale citations drop to the footnote, surfaced
        # decls become reviewable cards — exactly as subdeck.build_subdeck does.
        reviewable, stale_citations, reconcile = _reconcile(decls)
        # skip_api: cards = MAIN only; auto_approved = the API decls ship would
        # fold into KEEP. A PURE read — nothing is recorded, nothing approved.
        main, api = _partition(issue_id, reviewable, skip_api)
        prog = _progress(issue_id, main)
        return {
            "issue": issue_id,
            "title": iss["title"],
            "chapter": iss["chapter"],
            "card_tier": _worst_tier(reviewable),
            "cards": _ordered_cards(issue_id, main),
            "auto_approved": _ordered_cards(issue_id, api) if skip_api else [],
            "progress": prog,
            "complete": prog["reviewed"] >= prog["total"] and prog["total"] > 0,
            "summary": _summary(issue_id, reviewable),
            # The leading informal-approval slot (None when no decl is
            # LLM-rendered) — mirrors the live subdeck shape; OUTSIDE the cards.
            "informal": _informal_slot(issue_id, reviewable),
            # The issue-level "something is MISSING" note (or None).
            "missing": (_VERDICTS.get(issue_id, {}).get(MISSING_KEY) or {}).get("note"),
            # Reconciliation outputs (PURE — mirror the live subdeck shape).
            "stale_citations": stale_citations,
            "reconcile": reconcile,
        }


def demo_missing_suggestions(issue_id: int) -> list:
    """SIMULATED "this may already exist" candidates for the flag-missing modal
    (mirrors cards.missing_suggestions_for_issue's shape). Canned for the demo's
    tensor-product / function-multiplication issue; [] otherwise."""
    iss = _BY_ID.get(issue_id) or {}
    title = (iss.get("title") or "").lower()
    if "function multiplication" in title or "12.22" in title:
        return [{
            "decl": "MixedTensorField.IsSmooth.smul_of_contMDiff",
            "matched_key": "12.22",
            "cited_by": [],
            "in_code": True,
            "source_file": "GeometricAnalysis/LeeSM/Chapter12/TensorField.lean",
            "snippet": "Multiplication by a smooth function preserves smoothness "
                       "of mixed tensor fields.",
        }]
    return []


def demo_record_missing(issue_id: int, note=None) -> dict:
    """SIMULATED missing-note record/clear (mirrors subdeck.record_missing)."""
    with _LOCK:
        text = (note or "").strip()
        decls = _VERDICTS.setdefault(issue_id, {})
        if text:
            decls[MISSING_KEY] = {"verdict": "missing", "note": text}
        else:
            decls.pop(MISSING_KEY, None)
        return {"ok": True, "missing": (decls.get(MISSING_KEY) or {}).get("note")}


def demo_record_verdict(issue_id: int, decl: str, verdict: str, note=None) -> dict:
    if verdict not in ("verify", "reject", "defer"):
        raise ValueError(f"unknown verdict {verdict!r}")
    if verdict == "reject" and not (note and str(note).strip()):
        raise ValueError("a reject requires a non-empty note")
    iss = _BY_ID.get(issue_id)
    if iss is None:
        raise ValueError(f"no demo issue #{issue_id}")
    with _LOCK:
        _VERDICTS.setdefault(issue_id, {})[decl] = {
            "verdict": verdict,
            "note": str(note) if note is not None else None,
        }
        # Progress/summary are over the REVIEWABLE (reconciled) set — stale
        # citations are footnote entries, never reviewable cards.
        reviewable, _stale, _rec = _reconcile(iss["decls"])
        prog = _progress(issue_id, reviewable)
        return {
            "ok": True,
            "progress": prog,
            "complete": prog["reviewed"] >= prog["total"] and prog["total"] > 0,
            "summary": _summary(issue_id, reviewable),
            "informal": _informal_slot(issue_id, reviewable),
            "next_decl": _next_unreviewed(issue_id, reviewable),
        }


def _compose_ship_note(issue_id: int, summary: dict, missing=None) -> str:
    keep = summary["kept"]
    fix = summary["rejected"]
    lines = [f"Per-declaration review of issue #{issue_id}.", ""]
    if keep:
        lines.append(
            "KEEP these declarations exactly as written (human-verified — "
            "do NOT change their statements or signatures):"
        )
        lines += [f"- {n}" for n in keep]
        lines.append("")
    if fix:
        lines.append("FIX these declarations (rejected):")
        lines += [f"- {r['name']}: {r['note']}" for r in fix]
        lines.append("")
    if (missing or "").strip():
        lines.append("ADD the missing declaration(s):")
        lines += [
            (ln if ln.lstrip().startswith("-") else f"- {ln.rstrip()}")
            for ln in missing.strip().splitlines()
        ]
        lines.append("")
    lines.append("Leave all other code untouched.")
    return "\n".join(lines)


def demo_ship(issue_id: int, *, skip_api: bool = False) -> dict:
    """SIMULATED ship — composes the outcome but fires nothing irreversible.

    Mirrors subdeck.ship_issue: when ``skip_api``, the issue's easy-API decls
    WITHOUT an explicit human verdict are auto-approved (folded into KEEP), and
    completeness is over the MAIN decls only. Auto-approval is realized only
    here (the ship), never on a demo_subdeck read."""
    iss = _BY_ID.get(issue_id)
    if iss is None:
        return {"ok": False, "reason": f"no demo issue #{issue_id}"}
    with _LOCK:
        # Ship composes over the RECONCILED reviewable set (stale citations are
        # footnote-only and never block a ship); auto-approval is realized here.
        reviewable, _stale, _rec = _reconcile(iss["decls"])
        main, api = _partition(issue_id, reviewable, skip_api)
        prog = _progress(issue_id, main)
        if prog["reviewed"] < prog["total"] or prog["total"] == 0:
            return {
                "ok": False,
                "reason": "subdeck not complete — review every declaration first",
            }
        summary = _summary(issue_id, reviewable)
        # Auto-approved API decls (skip_api: an API decl with no stored verdict)
        # fold into KEEP — the realization, at the ship only.
        recs = _VERDICTS.get(issue_id, {})
        auto_names = [
            d["name"] for d in api if d["name"] not in recs
        ] if skip_api else []
        kept = list(summary["kept"]) + auto_names
        composed = {
            "kept": kept,
            "rejected": summary["rejected"],
            "deferred": summary["deferred"],
        }
        # Auto-add: surfaced decls the human KEPT are written into the issue body.
        surfaced_names = {d["name"] for d in reviewable if d.get("surfaced")}
        added_to_issue = sorted(n for n in kept if n in surfaced_names)
        auto_clause = ""
        if auto_names:
            auto_clause = (
                f"\n\nAuto-approved {len(auto_names)} API declaration(s): "
                + ", ".join(auto_names)
            )
        add_clause = ""
        if added_to_issue:
            add_clause = (
                f"\n\nAdded {len(added_to_issue)} surfaced declaration(s) to the "
                "issue body: " + ", ".join(added_to_issue)
            )
        # Informal-approval: if the human approved the LLM-rendered informal
        # text, ship would flip the issue body's marker to verified.
        informal_verified = (
            (_VERDICTS.get(issue_id, {}).get(INFORMAL_KEY) or {}).get("verdict")
            == "verify"
        )
        informal_clause = (
            "\n\nMarked the issue's informal statement verified (LLM-rendered "
            "marker flipped)." if informal_verified else ""
        )
        missing = (_VERDICTS.get(issue_id, {}).get(MISSING_KEY) or {}).get("note")
        if summary["rejected"] or missing:
            note = _compose_ship_note(issue_id, composed, missing)
            outcome = "shipped"
            message = (
                "DEMO — not actually sent. In live mode this composed "
                "instruction would go verbatim to Aristotle:\n\n"
                + note + auto_clause + add_clause + informal_clause
            )
        else:
            outcome = "verified"
            message = (
                "DEMO — issue would be marked VERIFIED (no Aristotle). "
                "All declarations approved." + auto_clause + add_clause
                + informal_clause
            )
        # Clear this issue's verdicts; pick the next not-yet-completed issue.
        _VERDICTS.pop(issue_id, None)
        nxt = None
        for other in _DEMO_ISSUES:
            if other["id"] == issue_id:
                continue
            other_reviewable, _s, _r = _reconcile(other["decls"])
            op = _progress(other["id"], other_reviewable)
            if op["reviewed"] < op["total"]:
                nxt = other["id"]
                break
        return {
            "ok": True,
            "outcome": outcome,
            "message": message,
            "auto_approved": auto_names,
            "added_to_issue": added_to_issue,
            "informal_verified": informal_verified,
            "advanced_to_issue": nxt,
        }


def demo_refresh(issue_id: int) -> dict:
    """SIMULATED push-refresh — mirrors :func:`marathon.deck.refresh.\
refresh_issue_signatures` shape but fires NOTHING (no ``gh issue edit``, no
    GitHub, no git). Reports the drift the reconciliation would push: the stale
    citations it would DROP and the surfaced decls it would ADD. The demo has no
    real repo/HEAD, so ``sha`` is a synthetic placeholder and ``updated`` is true
    iff there is any drift to push."""
    iss = _BY_ID.get(issue_id)
    if iss is None:
        return {
            "ok": False, "updated": False, "dropped": [], "added": [],
            "sha": None, "message": f"no demo issue #{issue_id}",
        }
    reviewable, _stale, reconcile = _reconcile(iss["decls"])
    dropped = reconcile["dropped"]
    added = reconcile["surfaced"]
    sha = "demo000"
    updated = bool(dropped or added)
    if updated:
        message = (
            f"DEMO — not actually pushed. In live mode the '### Lean "
            f"signatures' section of issue #{issue_id} would be re-quoted to "
            f"HEAD: dropping {len(dropped)} stale citation(s) "
            f"({', '.join(dropped) or '—'}) and adding {len(added)} surfaced "
            f"decl(s) ({', '.join(added) or '—'})."
        )
    else:
        message = (
            f"DEMO — issue #{issue_id} signatures already match the code; "
            "nothing to refresh."
        )
    return {
        "ok": True,
        "updated": updated,
        "dropped": dropped,
        "added": added,
        "sha": sha,
        "message": message,
    }
