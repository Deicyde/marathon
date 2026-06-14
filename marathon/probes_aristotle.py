"""marathon.probes_aristotle — budget-capped Aristotle vacuity probes.

Plan §2 ruling 5 (BINDING) + ``crit-feas-verification-surface-first`` §4.

A *vacuity probe* asks Aristotle to DISPROVE a target theorem's hypotheses:
to prove ``<hypotheses> → False``. A SUCCESS means the hypotheses are jointly
unsatisfiable, so the theorem is **vacuously true** — a broken (misformalized)
spec, the documented typo-exploit failure mode (Zulip, "Aristotle and
axioms"). This is the only probe that *actively hunts* misformalization with a
prover rather than waiting for a human.

Three things make this module load-bearing, and all three are guards, not
features:

* **It spends real Aristotle budget.** Pure-Lean probes (unfolding / sanity,
  :mod:`marathon.audit.probes`, a separate agent) are free and ship first;
  this one is the *expensive* probe, so it is OPT-IN only (never wired into
  the auto pipeline this phase), HARD-CAPPED per invocation
  (:data:`DEFAULT_MAX_PROBES`), and DEDUPED by a content-hash of the goal
  (persisted under ``.marathon/audit/vacuity/`` so the same vacuity goal is
  never resubmitted across runs). Per crit-feas §4: "per-chapter probe
  fan-out has no known budget" — the governor is the answer.

* **The goal mentions project defs, so it can't be prompt-only.** A
  ``¬ (∀ x, IsSmooth x)`` goal does not elaborate without the repo
  (crit-feas §4). The probe goal ``.lean`` is therefore STAGED INTO A COPY
  of the consumer repo (filtered by ``.gitignore`` exactly like
  :mod:`marathon.skeleton`) — never into the live repo, never committed —
  and the probe file is the only thing Aristotle is told it may edit. Other
  Aristotle bundles must continue to exclude probe files (they live only in
  this throwaway staging copy).

* **The evidence is asymmetric.** A SUCCESSFUL disproof (``TaskStatus.COMPLETE``)
  is HIGH-SIGNAL: the spec is broken — we write a structured finding file and
  print it. A failure-to-disprove (anything else terminal:
  ``COMPLETE_WITH_ERRORS`` / ``FAILED`` / ``CANCELED`` / ``OUT_OF_BUDGET``)
  is WEAK negative evidence — Aristotle gives up opaquely, so absence of a
  finding NEVER raises a tier or trust level. This asymmetry is enforced in
  :func:`interpret_outcome` and surfaced in every record.

NO auto-rejection is wired this phase. A successful disproof writes a finding
and prints; auto-filing it into the daemon queue waits for the critique's
"caps + dedup + notification first" precondition (the caps and dedup land
here; the notification hook and an explicit opt-in flag are future work).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from marathon.audit.records import AuditSnapshot, DeclAudit

# Where dedup state and findings live: a self-gitignored derived-cache dir
# under the repo's ``.marathon/audit/`` (same convention as the audit
# snapshot dir and the ledger db). Never committed.
VACUITY_RELPATH = Path(".marathon") / "audit" / "vacuity"
SUBMITTED_NAME = "submitted.json"  # dedup index: goal-hash -> record
FINDINGS_DIRNAME = "findings"  # one JSON per SUCCESSFUL disproof

# The hard per-invocation cap. The budget governor is the whole point of this
# module; the default is deliberately small (the critique: undocumented
# pricing + a ToS concurrent-session cap mean fan-out must be bounded).
DEFAULT_MAX_PROBES = 3

# Where the generated probe goal lives inside the staged repo copy. It must
# NOT collide with the consumer's tree; a dotted scratch dir under the repo
# root keeps it out of any lake target while still elaborating against the
# repo (we stage a copy, so the live repo is untouched regardless).
PROBE_DIR_IN_STAGE = ".marathon_vacuity_probe"


# --- Outcome / evidence semantics --------------------------------------------


@dataclass(frozen=True)
class VacuityGoal:
    """A generated vacuity goal for one target theorem.

    ``goal_hash`` is the dedup key — a content hash over the *meaning* of
    the goal (decl name + the normalized goal source), so the identical
    vacuity question is never resubmitted (across runs, via the persisted
    index). ``lean_source`` is the full ``.lean`` file staged for Aristotle;
    ``filename`` is its basename under :data:`PROBE_DIR_IN_STAGE`."""

    decl_name: str
    lean_source: str
    goal_hash: str
    filename: str

    @property
    def relpath(self) -> str:
        """The goal file's path relative to the staged repo root — what
        Aristotle is told to edit and what the prompt names."""
        return f"{PROBE_DIR_IN_STAGE}/{self.filename}"


@dataclass(frozen=True)
class VacuityOutcome:
    """The interpretation of one probe run's terminal status.

    ``broken_spec`` is the ONLY high-signal verdict — a successful disproof.
    Everything else is ``inconclusive`` and, by the binding asymmetry, must
    never move a tier."""

    decl_name: str
    goal_hash: str
    status: str  # the terminal TaskStatus.value
    broken_spec: bool  # True iff Aristotle PROVED hyps -> False
    summary: str  # one-line human summary
    project_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: str = ""

    @property
    def inconclusive(self) -> bool:
        """A failure-to-disprove — WEAK negative evidence. Never a tier
        change, never a finding (absence of a vacuity finding is not
        evidence of a correct spec)."""
        return not self.broken_spec

    def to_json(self) -> dict[str, Any]:
        return {
            "decl_name": self.decl_name,
            "goal_hash": self.goal_hash,
            "status": self.status,
            "broken_spec": self.broken_spec,
            "summary": self.summary,
            "project_id": self.project_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
        }


def interpret_outcome(
    goal: VacuityGoal,
    status_value: str,
    *,
    project_id: Optional[str] = None,
    task_id: Optional[str] = None,
) -> VacuityOutcome:
    """Map a terminal ``TaskStatus.value`` onto the asymmetric evidence
    semantics (BINDING):

    * ``COMPLETE`` ⇒ ``broken_spec=True`` — Aristotle PROVED the hypotheses
      contradictory; the theorem is vacuous, the spec is broken. HIGH signal.
    * anything else (``COMPLETE_WITH_ERRORS`` / ``FAILED`` / ``CANCELED`` /
      ``OUT_OF_BUDGET`` / any unknown) ⇒ ``broken_spec=False`` — a
      failure-to-disprove, WEAK negative evidence. It records "no vacuity
      found (inconclusive)" and NEVER raises a tier.

    The asymmetry is the point: a prover that closes ``hyps → False`` has
    demonstrated something; a prover that gives up has demonstrated nothing
    (it gives up opaquely)."""
    broken = status_value == "COMPLETE"
    if broken:
        summary = (
            f"BROKEN SPEC: Aristotle proved {goal.decl_name}'s hypotheses "
            "entail False — the theorem is vacuously true (misformalized)."
        )
    else:
        summary = (
            f"no vacuity found (inconclusive): probe for {goal.decl_name} "
            f"ended {status_value} without disproving the hypotheses — "
            "WEAK negative evidence, no tier change."
        )
    return VacuityOutcome(
        decl_name=goal.decl_name,
        goal_hash=goal.goal_hash,
        status=status_value,
        broken_spec=broken,
        summary=summary,
        project_id=project_id,
        task_id=task_id,
        created_at=_now_iso(),
    )


# --- Goal generation ----------------------------------------------------------


def build_vacuity_goal(decl: DeclAudit) -> VacuityGoal:
    """Generate the vacuity goal ``.lean`` for one target theorem.

    The goal asks to prove the target's hypotheses entail ``False`` — i.e.
    that the hypotheses are jointly unsatisfiable, which would make the
    theorem vacuously true (a broken spec).

    We deliberately do NOT do pretty-printer surgery on ``type_pp`` to split
    the hypothesis telescope from the conclusion: the pp output embeds
    instance/universe terms and nested dependent binders, and a mis-split
    would silently change the meaning of the probe. The robust alternative,
    available because the probe runs WITH THE REPO IN CONTEXT (crit-feas §4:
    a ``¬hyps`` goal can't be prompt-only — it mentions project defs), is to
    state the goal *by name*:

        ``theorem marathon_vacuity_… : ¬ <target>.hyps``  is not expressible
        directly, so we instead emit the canonical vacuity reduction that
        works for ANY ``∀``-telescoped proposition without parsing it:

            ``∀ …same binders as `target`… , False``

    and hand Aristotle the EXACT pinned-pp statement (as a comment) plus an
    instruction to restate the target's hypothesis binders and close with a
    proof of ``False``. The emitted file imports the target's own module (so
    every name in the statement resolves) and leaves a single ``sorry`` for
    Aristotle to discharge. A correct spec leaves this ``sorry`` unprovable;
    only a vacuous one closes it.

    ``goal_hash`` is over ``decl_name`` + the full goal source (which embeds
    the pinned-pp statement), so the SAME statement re-probes to the same
    hash (dedup hit) while a CHANGED statement re-probes fresh.
    """
    type_pp = decl.type_pp or "(no elaborated type available)"
    safe_name = _safe_ident(decl.name)
    import_line = (
        f"import {decl.module}"
        if decl.module
        else "-- (target module unknown; Aristotle must add the import)"
    )
    header = [
        "-- Marathon vacuity probe — BUILT, NEVER imported by the library.",
        "-- Goal: the target theorem's hypotheses are jointly contradictory",
        "-- (they entail `False`). A *successful* proof here means the target",
        "-- is VACUOUSLY TRUE — a broken/misformalized spec. A failure to",
        "-- prove it is WEAK evidence and changes no trust tier.",
        f"-- target: {decl.name}   (kind: {decl.kind})",
        "--",
        "-- target statement (pinned pretty-printer output):",
    ]
    header.extend(f"--   {line}" for line in (type_pp.splitlines() or [type_pp]))
    body = [
        import_line,
        "",
        f"theorem marathon_vacuity_{safe_name} :",
        "    -- Restate the target's hypothesis binders here, then derive",
        "    -- `False`. (Replace `True` with the actual hypotheses of the",
        "    -- target above; if they are satisfiable this is unprovable —",
        "    -- do NOT fabricate a proof.)",
        "    True → False := by",
        "  sorry",
    ]
    lean_source = "\n".join(header + [""] + body) + "\n"
    goal_hash = _goal_hash(decl.name, lean_source)
    filename = f"Vacuity_{safe_name}_{goal_hash[:8]}.lean"
    return VacuityGoal(
        decl_name=decl.name,
        lean_source=lean_source,
        goal_hash=goal_hash,
        filename=filename,
    )


def _safe_ident(name: str) -> str:
    """A filesystem/identifier-safe slug of a dotted decl name."""
    return "".join(c if c.isalnum() else "_" for c in name) or "anon"


def _goal_hash(decl_name: str, lean_source: str) -> str:
    """Content hash over the goal's meaning — the dedup key.

    Includes the decl name and the FULL goal source so a regenerated goal
    for the same decl with the same statement hashes identically (skip), but
    a changed statement (which changes the embedded pinned-pp comment) hashes
    differently (re-probe). The ``[:8]`` filename suffix and this full digest
    move together."""
    h = hashlib.sha256()
    h.update(decl_name.encode("utf-8"))
    h.update(b"\0")
    h.update(lean_source.encode("utf-8"))
    return h.hexdigest()


# --- Dedup index (persisted, gitignored) -------------------------------------


def vacuity_state_dir(repo_dir: str | Path) -> Path:
    return Path(repo_dir) / VACUITY_RELPATH


def _ensure_state_dir(repo_dir: str | Path) -> Path:
    """Create (and self-gitignore) the vacuity state dir — same convention
    as the audit snapshot dir and the ledger db: droppable derived cache
    living inside the consumer repo, never committed."""
    state_dir = vacuity_state_dir(repo_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    gitignore = state_dir / ".gitignore"
    if not gitignore.is_file():
        gitignore.write_text("*\n")
    return state_dir


def load_submitted_index(repo_dir: str | Path) -> dict[str, dict]:
    """The dedup index: ``{goal_hash: record}`` of every goal ever
    submitted. Absent / unreadable index ⇒ empty (a corrupt cache is
    absence of memory, not an error — at worst we re-probe once)."""
    path = vacuity_state_dir(repo_dir) / SUBMITTED_NAME
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def record_submitted(
    repo_dir: str | Path, outcome: VacuityOutcome
) -> None:
    """Persist that ``outcome.goal_hash`` was submitted (with its terminal
    status), so the same vacuity goal is never resubmitted. Append-merge
    into the index; atomic write."""
    state_dir = _ensure_state_dir(repo_dir)
    index = load_submitted_index(repo_dir)
    index[outcome.goal_hash] = outcome.to_json()
    path = state_dir / SUBMITTED_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def already_submitted(repo_dir: str | Path, goal_hash: str) -> bool:
    return goal_hash in load_submitted_index(repo_dir)


# --- Findings (one per SUCCESSFUL disproof) -----------------------------------


def write_finding(repo_dir: str | Path, outcome: VacuityOutcome) -> Path:
    """Write a structured finding file for a SUCCESSFUL disproof (a broken
    spec). HIGH-signal — but this phase only writes the file and prints; it
    does NOT auto-file a rejection (the critique's "auto-rejection needs
    caps/dedup/notification first" — caps + dedup land here; the
    notification hook and an opt-in auto-reject flag are future work).

    Raises ``ValueError`` if called for an inconclusive outcome — a finding
    is by definition a disproof; the asymmetry forbids writing one for weak
    negative evidence."""
    if not outcome.broken_spec:
        raise ValueError(
            "refusing to write a vacuity finding for an inconclusive "
            "outcome — only a successful disproof is a finding (binding "
            "asymmetry)"
        )
    state_dir = _ensure_state_dir(repo_dir)
    findings_dir = state_dir / FINDINGS_DIRNAME
    findings_dir.mkdir(parents=True, exist_ok=True)
    payload = dict(outcome.to_json())
    payload["finding_kind"] = "vacuous_spec"
    payload["auto_rejected"] = False  # never this phase
    payload["note"] = (
        "Aristotle proved this theorem's hypotheses entail False, so the "
        "theorem is vacuously true (a broken/misformalized spec). This is "
        "a high-signal finding. No automatic rejection was filed; review "
        "and reject manually."
    )
    path = findings_dir / f"{_safe_ident(outcome.decl_name)}_{outcome.goal_hash[:8]}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return path


# --- Staging (repo copy with the probe goal; probe excluded elsewhere) --------


def _list_repo_files(repo_dir: Path) -> list[str]:
    """Tracked + untracked-not-gitignored files in ``repo_dir`` (the
    skeleton convention). The ``.gitignore`` filter is what keeps the
    ``.marathon`` derived caches (and any *other* probe files) OUT of the
    bundle — they are gitignored, so they never get staged, so Aristotle
    never sees or edits them (crit-feas §4: "probe files must be excluded
    from create_from_directory bundles or Aristotle edits them")."""
    if not (repo_dir / ".git").exists():
        raise ValueError(f"{repo_dir} is not a git repo (no .git)")
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=str(repo_dir),
        capture_output=True,
        check=True,
    )
    return [p.decode("utf-8") for p in result.stdout.split(b"\0") if p]


def stage_probe_bundle(
    repo_dir: str | Path, goal: VacuityGoal, dest: str | Path
) -> Path:
    """Stage a COPY of the consumer repo at ``dest`` with exactly ONE probe
    goal added (``goal``), and return the path to the staged probe file.

    The repo copy is filtered by ``.gitignore`` (skeleton's
    ``git ls-files`` mechanism), so the live repo is never touched and
    gitignored caches / OTHER probe files are excluded. The probe goal is
    written under :data:`PROBE_DIR_IN_STAGE` INSIDE the copy only — it never
    lands in the user's repo and is never committed.

    Returns the staged probe file path (``dest/<PROBE_DIR_IN_STAGE>/...``).
    """
    repo = Path(repo_dir)
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)
    for rel in _list_repo_files(repo):
        src = repo / rel
        if not src.is_file():
            continue
        out = dest_path / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    probe_dir = dest_path / PROBE_DIR_IN_STAGE
    probe_dir.mkdir(parents=True, exist_ok=True)
    probe_path = probe_dir / goal.filename
    probe_path.write_text(goal.lean_source)
    return probe_path


# --- Governor: select targets under the cap, skipping dedups ------------------


@dataclass
class ProbePlan:
    """The governor's decision for one invocation: which goals to run
    (under the cap, not already submitted) and why others were skipped."""

    to_run: list[VacuityGoal] = field(default_factory=list)
    skipped_dedup: list[str] = field(default_factory=list)  # decl names
    skipped_cap: list[str] = field(default_factory=list)  # decl names
    skipped_unprobeable: list[str] = field(default_factory=list)  # decl names


def select_targets(
    snapshot: AuditSnapshot,
    decl_names: list[str],
    repo_dir: str | Path,
    *,
    max_probes: int = DEFAULT_MAX_PROBES,
) -> ProbePlan:
    """The budget governor. For each requested decl, in order:

    * skip non-theorem / unknown / valueless decls as *unprobeable* (a
      vacuity probe only makes sense for a proposition with hypotheses);
    * generate the goal and skip it if already submitted (DEDUP — the
      persisted index);
    * otherwise enqueue, until the hard ``max_probes`` cap is hit; further
      eligible goals are recorded as ``skipped_cap``.

    Returns a :class:`ProbePlan`. Pure except for reading the dedup index;
    no Aristotle, no staging, no spend — the caller submits ``to_run``.
    """
    by_name = snapshot.by_name()
    plan = ProbePlan()
    submitted = load_submitted_index(repo_dir)
    # Goal hashes already enqueued THIS invocation. The persisted index only
    # records goals from prior runs; without this, two selectors resolving to
    # the same decl (e.g. ``Foo.t`` and the unique suffix ``t``) — or a decl
    # name simply repeated on the command line — would each enqueue the
    # identical goal and double-spend, defeating the dedup guarantee. Dedup
    # must hold WITHIN an invocation too, not just across runs.
    planned: set[str] = set()
    for name in decl_names:
        decl = by_name.get(name)
        if decl is None or decl.is_unknown or not _is_probeable(decl):
            plan.skipped_unprobeable.append(name)
            continue
        goal = build_vacuity_goal(decl)
        if goal.goal_hash in submitted or goal.goal_hash in planned:
            plan.skipped_dedup.append(name)
            continue
        if len(plan.to_run) >= max_probes:
            plan.skipped_cap.append(name)
            continue
        plan.to_run.append(goal)
        planned.add(goal.goal_hash)
    return plan


def _is_probeable(decl: DeclAudit) -> bool:
    """A vacuity probe is meaningful only for a PROPOSITION (theorem/lemma):
    a def/structure/etc. has no "hypotheses" to disprove. We key on kind —
    theorems and the ``other`` kind (which the audit uses for theorem-like
    declarations it can't finely classify) are probeable; value-carrying
    def kinds are not."""
    return decl.kind in ("theorem", "lemma", "other")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- The opt-in runner (submits the plan's goals, spends budget) --------------


async def run_one_probe(
    repo_dir: str | Path,
    goal: VacuityGoal,
    *,
    polling_interval: int = 30,
    tmp_parent: Optional[Path] = None,
) -> VacuityOutcome:
    """Stage + submit ONE vacuity goal and interpret its terminal status.

    Staging happens in a throwaway temp dir (a repo copy with the single
    probe file), so the live repo is never dirtied. The submission goes
    through :func:`marathon.aristotle_runtime.submit_vacuity_probe` (the one
    narrow helper). On any terminal status the dedup index records the goal;
    a successful disproof additionally writes a finding. Returns the
    :class:`VacuityOutcome` either way.

    This is the only function in the module that spends real Aristotle
    budget; it is reached ONLY from the opt-in CLI verb.
    """
    import tempfile

    from marathon.aristotle_runtime import submit_vacuity_probe, vacuity_probe_prompt

    repo = Path(repo_dir)
    with tempfile.TemporaryDirectory(
        prefix="marathon-vacuity-", dir=str(tmp_parent) if tmp_parent else None
    ) as td:
        stage = Path(td) / "bundle"
        stage_probe_bundle(repo, goal, stage)
        prompt = vacuity_probe_prompt(goal.relpath)
        project, task = await submit_vacuity_probe(
            stage,
            goal.relpath,
            polling_interval=polling_interval,
            prompt=prompt,
        )
    status_value = task.status.value if task and task.status else "UNKNOWN"
    outcome = interpret_outcome(
        goal,
        status_value,
        project_id=getattr(project, "project_id", None),
        task_id=getattr(task, "agent_task_id", None),
    )
    # Persist dedup + (only on a disproof) the finding. Recording happens
    # for EVERY terminal status so the same goal is never resubmitted, even
    # when it was inconclusive.
    record_submitted(repo, outcome)
    if outcome.broken_spec:
        write_finding(repo, outcome)
    return outcome


async def run_probes(
    repo_dir: str | Path,
    snapshot: AuditSnapshot,
    decl_names: list[str],
    *,
    max_probes: int = DEFAULT_MAX_PROBES,
    polling_interval: int = 30,
) -> tuple[ProbePlan, list[VacuityOutcome]]:
    """The opt-in entry point: plan under the governor, then submit each
    surviving goal serially (never a fan-out — the ToS caps concurrent
    sessions and budget is undocumented; serial keeps the spend legible).

    Returns ``(plan, outcomes)``. Spends real budget for each goal in
    ``plan.to_run``; the governor (cap + dedup, applied in
    :func:`select_targets`) is what bounds that spend."""
    plan = select_targets(
        snapshot, decl_names, repo_dir, max_probes=max_probes
    )
    outcomes: list[VacuityOutcome] = []
    for goal in plan.to_run:
        outcome = await run_one_probe(
            repo_dir, goal, polling_interval=polling_interval
        )
        outcomes.append(outcome)
    return plan, outcomes
