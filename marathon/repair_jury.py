"""Repair faithfulness jury — did a compilation repair WEAKEN a statement?

A ``marathon repair`` makes a non-compiling module build again. ``[build:OK]``
proves it *compiles*, NEVER that it stayed *faithful* — a repair could "fix" the
build by dropping a hypothesis, weakening a conclusion, or making a definition
vacuous. This jury compares each changed declaration's statement BEFORE (the
broken version on the base branch) vs AFTER (the repaired version) and flags any
whose MEANING was weakened, so a compiling-but-weakened repair is blocked before
it can merge to ``main``.

Unlike :mod:`marathon.jury` (proof_integrity / code_quality, no faithfulness),
this is a narrow structural before/after diff judgment — "did the STATEMENT get
weaker?" — not open-ended faithfulness-to-source, so the information-firewall
concern is much smaller. Renamed-reference updates and type-mismatch fixes that
preserve meaning PASS; a dropped hypothesis / weakened conclusion / vacuous
restatement FAILS.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from marathon.claude_proc import run_claude

#: Cap on how much before/after source we feed the jury (chars), so a huge
#: refactor can't blow the prompt. Repairs are narrow by construction.
_SOURCE_CAP = 60_000


@dataclass
class RepairFaithfulnessVerdict:
    """Outcome of the repair faithfulness jury."""
    ran: bool = False
    passed: Optional[bool] = None          # None ⇒ jury didn't run / undetermined
    weakened: list[dict] = field(default_factory=list)  # [{decl, reason}]
    note: str = ""                         # human-readable status
    changed_files: list[str] = field(default_factory=list)

    @property
    def blocks(self) -> bool:
        """Block the merge ONLY on an affirmative weakening finding — an
        undetermined jury (no diff, claude failure) never blocks (honest
        degradation: build-OK + human review still guard it)."""
        return self.ran and self.passed is False

    def render_line(self) -> str:
        if not self.ran:
            return f"  repair-jury: skipped — {self.note}"
        if self.passed:
            return "  repair-jury: PASS — no statement weakened by the repair"
        names = ", ".join(w.get("decl", "?") for w in self.weakened) or "(unnamed)"
        return f"  repair-jury: FAIL — weakened statement(s): {names}"


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo_dir),
        capture_output=True, text=True, check=False,
    )


def _changed_lean_files(repo_dir: Path, base: str) -> list[str]:
    """Repo-relative ``.lean`` files that differ between ``origin/<base>`` and
    HEAD (the repair's net change)."""
    cp = _git(repo_dir, "diff", "--name-only", f"origin/{base}", "HEAD")
    if cp.returncode != 0:
        cp = _git(repo_dir, "diff", "--name-only", base, "HEAD")
    if cp.returncode != 0:
        return []
    return [f for f in cp.stdout.splitlines() if f.endswith(".lean")]


def _before(repo_dir: Path, base: str, rel: str) -> Optional[str]:
    cp = _git(repo_dir, "show", f"origin/{base}:{rel}")
    if cp.returncode != 0:
        cp = _git(repo_dir, "show", f"{base}:{rel}")
    return cp.stdout if cp.returncode == 0 else None


def _assemble_prompt(pairs: "list[tuple[str, str, str]]") -> str:
    """``pairs`` = ``[(rel, before, after), …]``."""
    blocks = []
    for rel, before, after in pairs:
        blocks.append(
            f"## `{rel}`\n\n### BEFORE (did not compile)\n```lean\n"
            f"{before[:_SOURCE_CAP]}\n```\n\n### AFTER (compiles — the repair)\n"
            f"```lean\n{after[:_SOURCE_CAP]}\n```"
        )
    body = "\n\n".join(blocks)
    return (
        "You are a faithfulness auditor for an automated Lean 4 REPAIR. A module "
        "did not compile (it referenced upstream declarations that were renamed "
        "or removed, or had type mismatches). An automated agent repaired it to "
        "compile. Your ONLY job: decide whether the repair changed the MEANING "
        "of any DECLARATION'S STATEMENT (its signature/type — for a `def` its "
        "type, for a `theorem`/`lemma` its proposition), beyond faithful "
        "reference-renames and type-mismatch fixes.\n\n"
        "FAIL a declaration whose statement was WEAKENED: a hypothesis dropped "
        "or strengthened, a conclusion weakened, an index/dimension quietly "
        "changed, a statement replaced with `True`/`trivial` or made vacuous, or "
        "a `def` made to return a trivial/placeholder object. PASS statements "
        "that are mathematically EQUIVALENT to before (only renamed references / "
        "adjusted to current signatures). Proof BODIES are irrelevant — a "
        "`sorry` body is fine; judge STATEMENTS only.\n\n"
        f"{body}\n\n"
        "Respond with ONLY a JSON object, nothing else:\n"
        '{"verdict": "pass" | "fail", "weakened": '
        '[{"decl": "<name>", "reason": "<how the statement got weaker>"}]}\n'
        '"verdict" is "fail" iff `weakened` is non-empty.'
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(response: str) -> "tuple[Optional[bool], list[dict], str]":
    m = _JSON_RE.search(response or "")
    if not m:
        return None, [], "jury response had no JSON object"
    try:
        data = json.loads(m.group(0))
    except ValueError as e:
        return None, [], f"jury JSON parse failed: {e}"
    weakened = data.get("weakened") or []
    if not isinstance(weakened, list):
        weakened = []
    weakened = [w for w in weakened if isinstance(w, dict)]
    verdict = str(data.get("verdict", "")).lower()
    if verdict == "fail" or weakened:
        return False, weakened, "weakening found"
    if verdict == "pass":
        return True, [], "no weakening"
    return None, [], f"unrecognized verdict {verdict!r}"


def run_repair_faithfulness_jury(
    repo_dir: Path,
    base: str,
    *,
    model: Optional[str] = None,
    timeout: Optional[float] = 600.0,
    _runner=run_claude,
) -> RepairFaithfulnessVerdict:
    """Compare every changed ``.lean`` file's declarations before/after the
    repair and flag weakened statements. Never raises — any failure yields an
    UNDETERMINED verdict (``ran`` may be True with ``passed=None``) that does
    NOT block (honest degradation: build-OK + human review still apply)."""
    repo_dir = Path(repo_dir)
    changed = _changed_lean_files(repo_dir, base)
    if not changed:
        return RepairFaithfulnessVerdict(
            ran=False, note="no .lean files changed vs base", changed_files=[])
    pairs: list[tuple[str, str, str]] = []
    for rel in changed:
        before = _before(repo_dir, base, rel)
        if before is None:
            # New file (no `before`) — a repair shouldn't add whole modules, but
            # if it did there is no prior statement to weaken; skip it.
            continue
        try:
            after = (repo_dir / rel).read_text()
        except OSError:
            continue
        pairs.append((rel, before, after))
    if not pairs:
        return RepairFaithfulnessVerdict(
            ran=False, note="no before/after pairs to compare",
            changed_files=changed)
    try:
        proc = _runner(_assemble_prompt(pairs), model=model, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — a jury failure must never crash the pipeline
        return RepairFaithfulnessVerdict(
            ran=True, passed=None, note=f"claude call failed: {e}",
            changed_files=changed)
    if getattr(proc, "returncode", 1) != 0:
        return RepairFaithfulnessVerdict(
            ran=True, passed=None,
            note=f"claude exited {getattr(proc, 'returncode', '?')}",
            changed_files=changed)
    passed, weakened, note = _parse(getattr(proc, "stdout", "") or "")
    return RepairFaithfulnessVerdict(
        ran=True, passed=passed, weakened=weakened, note=note,
        changed_files=changed)
