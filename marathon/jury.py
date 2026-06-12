"""Advisory jury: Claude-scored ``proof_integrity`` + ``code_quality``.

WHY: the deterministic gate (``marathon.gate``) covers everything a
regex/`#print axioms` scan can see, but "the proof compiles" is not "the
proof is genuine" — autoform-bot's documented deception patterns (vacuous
bodies, class-as-axiom, placeholder instances over ``PUnit``, degenerate
hypotheses) all build green. The jury is the LLM half of phase-2's gate:
two thresholded rubrics (integrity ≥ 3, quality ≥ 3) borrowed from
autoform's jury vocabulary, minus its faithfulness rubric.

Binding design constraints (marathon-v2 plan §2 ruling 3):

* **No faithfulness judging.** The information firewall means Claude
  never sees the ``.tex``; grading LLM-rendered statements against LLM
  renderings is circular. Faithfulness stays human — the prompt says so
  explicitly, and this module never reads source texts.
* **Advisory.** ``run_jury`` returns ``None`` on *any* failure (no CLI,
  no prompt file, empty folder, subprocess error, unparseable output) and
  never raises out. Enforcement and overrides are wired by the caller.

Subprocess conventions (prompt via stdin against E2BIG,
``ANTHROPIC_API_KEY`` scrubbed so the CLI falls back to Max OAuth, the
cross-process slot limiter) are shared with the rater and
``claude_review.py`` via ``marathon.claude_proc.run_claude``; the
``MARATHON_CLAUDE_MODEL`` env override and the lenient JSON extraction
with a per-field regex fallback stay here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
# Kept although the claude call itself moved to marathon.claude_proc:
# existing tests patch ``jury.subprocess.run`` (a setattr on the stdlib
# module object, so the patch reaches claude_proc's ``subprocess.run``
# too) and need this name to exist.
import subprocess  # noqa: F401
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from marathon.claude_proc import run_claude

# Default model, matching the rater. Resolution order (computed per call,
# unlike claude_review's import-time read, so tests and long-lived
# processes see env changes): explicit ``model`` arg > MARATHON_CLAUDE_MODEL
# env var > this constant.
DEFAULT_MODEL = "claude-opus-4-7"

# Pass thresholds per the autoform jury vocabulary (proof_integrity
# pass ≥ 3, code_quality pass ≥ 3). These live here — not only in the
# prompt — so the verdict can be recomputed from the scores instead of
# trusting the model's self-reported "verdict" string.
INTEGRITY_THRESHOLD = 3
QUALITY_THRESHOLD = 3

# Context caps. The code cap keeps a pathological chapter (or a future
# multi-chapter target) from blowing the model's context; the diff cap
# matches the rater's 80k convention. Module-level so tests (and unusual
# deployments) can override without re-plumbing arguments.
MAX_CODE_CHARS = 160_000
MAX_DIFF_CHARS = 80_000

_VALID_VERDICTS = ("pass", "fail")


@dataclass
class JuryVerdict:
    """One jury call's outcome. ``verdict`` is recomputed from the scores
    + thresholds whenever both scores are present (the model's own
    verdict string is advisory input, not authority); ``parse_warning``
    carries lenient-parse / verdict-disagreement notes for the JSONL
    trail without failing the call."""

    proof_integrity: Optional[int] = None
    code_quality: Optional[int] = None
    verdict: Optional[str] = None  # "pass" | "fail"
    notes: Optional[str] = None
    model: Optional[str] = None
    parse_warning: Optional[str] = None

    @property
    def passed(self) -> Optional[bool]:
        if self.verdict is None:
            return None
        return self.verdict == "pass"

    def render_line(self) -> str:
        """One-line summary for console output / PR bodies, e.g.
        ``jury (advisory): integrity=4 quality=3 → PASS``."""
        pi = self.proof_integrity if self.proof_integrity is not None else "—"
        cq = self.code_quality if self.code_quality is not None else "—"
        verdict = (self.verdict or "?").upper()
        line = f"jury (advisory): integrity={pi} quality={cq} → {verdict}"
        if self.parse_warning:
            line += f"  [{self.parse_warning}]"
        return line


def _read_lean_files_capped(folder: Path, cap: int) -> str:
    """Concatenate every ``.lean`` file under ``folder`` (sorted, with
    ``=== FILE: rel ===`` headers, same format as the rater) and truncate
    at ``cap`` chars with an explicit marker so the model knows it is
    looking at a prefix.

    WHY the per-file guard: ``run_jury`` promises to never raise (the
    jury is advisory), and that guarantee must cover context assembly —
    an unreadable entry (permissions, a directory named ``*.lean``) is
    skipped with a printed note, and non-UTF-8 bytes are replaced rather
    than allowed to throw ``UnicodeDecodeError``."""
    parts: list[str] = []
    for lean_file in sorted(folder.rglob("*.lean")):
        rel = lean_file.relative_to(folder)
        try:
            text = lean_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"  jury: skipping unreadable {rel} — {e}")
            continue
        parts.append(f"=== FILE: {rel} ===\n{text}")
    code = "\n\n".join(parts)
    if len(code) > cap:
        code = code[:cap] + f"\n\n... (code truncated at {cap:,} chars)"
    return code


def _assemble_prompt(
    rubric: str,
    code: str,
    target_label: str,
    diff_text: Optional[str],
) -> str:
    """Rubric + optional diff + code, joined with ``---`` separators
    (same shape as the rater's prompt assembly)."""
    parts = [rubric]
    if diff_text:
        diff = diff_text
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + (
                f"\n\n... (diff truncated at {MAX_DIFF_CHARS:,} chars)"
            )
        parts.append(
            "## Diff under review (this iteration's changes)\n\n"
            "```diff\n" + diff + "\n```"
        )
    parts.append(
        f"## Code under review (target folder `{target_label}`)\n\n{code}"
    )
    return "\n\n---\n\n".join(parts)


_JURY_SCORE_FIELDS = ("proof_integrity", "code_quality")


def _extract_json_object(text: str) -> str:
    """Extract a JSON-object-shaped substring from a Claude response
    (bare object, fenced block, or embedded object — same ladder as the
    rater's extractor)."""
    text = text.strip()
    if text.startswith("{"):
        return text
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        return fence.group(1)
    bare = re.search(r"\{.*\}", text, re.DOTALL)
    if bare:
        return bare.group(0)
    raise ValueError("no JSON object found in response")


def _extract_jury_lenient(response: str) -> tuple[Optional[dict], Optional[str]]:
    """Tolerant extraction of the jury JSON. Strict ``json.loads`` first;
    on failure (typically an unescaped quote inside ``notes``), regex
    per-field recovery of the two scores + verdict + best-effort notes.

    Returns ``(data, partial_warning)``:
      - ``(dict, None)`` on clean strict parse;
      - ``(dict, "warning")`` on partial recovery;
      - ``(None, None)`` if nothing could be extracted.
    """
    try:
        return json.loads(_extract_json_object(response)), None
    except (json.JSONDecodeError, ValueError):
        pass

    data: dict = {}
    missing: list[str] = []
    for field_name in _JURY_SCORE_FIELDS:
        m = re.search(rf'"{field_name}"\s*:\s*(\d+|null)\b', response)
        if m:
            v = m.group(1)
            data[field_name] = None if v == "null" else int(v)
        else:
            missing.append(field_name)

    verdict_m = re.search(r'"verdict"\s*:\s*"(pass|fail)"', response)
    if verdict_m:
        data["verdict"] = verdict_m.group(1)

    notes_m = re.search(r'"notes"\s*:\s*"([\s\S]*?)"\s*[},]', response)
    if notes_m:
        data["notes"] = (
            notes_m.group(1)
            .replace(r"\n", "\n")
            .replace(r"\"", '"')
            .replace(r"\\", "\\")
        )
        notes_recovered = True
    else:
        notes_recovered = False

    if not data:
        return None, None

    parts = ["lenient-parse fallback used (strict json.loads failed)"]
    if missing:
        parts.append(f"missing score fields: {','.join(missing)}")
    if not notes_recovered:
        parts.append("notes string could not be recovered")
    return data, "; ".join(parts)


def _coerce_int(v) -> Optional[int]:
    if isinstance(v, bool):  # bool is an int subclass; a score it is not
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _resolve_model(model: Optional[str]) -> str:
    if model:
        return model
    return os.environ.get("MARATHON_CLAUDE_MODEL") or DEFAULT_MODEL


def run_jury(
    repo_dir: Path,
    target_folder: Path,
    *,
    diff_text: Optional[str] = None,
    model: Optional[str] = None,
) -> Optional[JuryVerdict]:
    """Score ``target_folder`` against the two jury rubrics via one
    ``claude -p`` call. Returns ``None`` on any failure — the jury is
    advisory, so a broken jury must never break the pipeline. Failures
    are printed as soft one-line notes (matching the post-pipeline's
    console style) so the operator can see *why* the jury was skipped.

    ``diff_text`` is an optional unified diff (this iteration's changes)
    assembled by the caller; the jury never runs git itself. ``model``
    overrides the ``MARATHON_CLAUDE_MODEL`` env var, which overrides
    ``DEFAULT_MODEL``.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        print("  jury: skipped — claude CLI not on PATH")
        return None

    prompt_path = Path(__file__).parent / "prompts" / "jury.md"
    if not prompt_path.is_file():
        print(f"  jury: skipped — jury.md missing at {prompt_path}")
        return None
    rubric = prompt_path.read_text()

    code = _read_lean_files_capped(target_folder, MAX_CODE_CHARS)
    if not code:
        print(f"  jury: skipped — no .lean files under {target_folder}")
        return None

    try:
        target_label = str(target_folder.relative_to(repo_dir))
    except ValueError:
        target_label = str(target_folder)
    prompt = _assemble_prompt(rubric, code, target_label, diff_text)

    resolved_model = _resolve_model(model)

    # Subprocess conventions (stdin prompt against E2BIG, the
    # ANTHROPIC_API_KEY scrub for Max OAuth, the cross-process slot
    # limiter) live in marathon.claude_proc.run_claude. The pre-resolved
    # model is passed explicitly so the verdict's ``model`` field and the
    # actual call can never disagree.
    try:
        proc = run_claude(prompt, model=resolved_model)
    except OSError as e:
        print(f"  jury: skipped — could not exec claude (errno {e.errno}: {e.strerror})")
        return None

    if proc.returncode != 0:
        err = ((proc.stderr or proc.stdout) or "").strip()[:300]
        print(f"  jury: skipped — claude exited {proc.returncode}: {err}")
        return None

    response = (proc.stdout or "").strip()
    if not response:
        print("  jury: skipped — claude returned empty stdout")
        return None

    data, partial_warning = _extract_jury_lenient(response)
    if data is None:
        print(f"  jury: skipped — could not extract verdict; raw: {response[:200]}")
        return None

    proof_integrity = _coerce_int(data.get("proof_integrity"))
    code_quality = _coerce_int(data.get("code_quality"))
    raw_verdict = data.get("verdict")
    model_verdict = raw_verdict if raw_verdict in _VALID_VERDICTS else None

    if proof_integrity is None and code_quality is None and model_verdict is None:
        print(f"  jury: skipped — no usable fields in response; raw: {response[:200]}")
        return None

    warnings = [partial_warning] if partial_warning else []

    # Recompute the verdict from the thresholds when both scores are
    # present — the thresholds are the contract; the model's "verdict"
    # string is just its own arithmetic, which we don't trust blindly.
    if proof_integrity is not None and code_quality is not None:
        computed = (
            "pass"
            if proof_integrity >= INTEGRITY_THRESHOLD
            and code_quality >= QUALITY_THRESHOLD
            else "fail"
        )
        if model_verdict is not None and model_verdict != computed:
            warnings.append(
                f"model verdict {model_verdict!r} disagreed with thresholds; "
                f"using computed {computed!r}"
            )
        verdict = computed
    else:
        verdict = model_verdict

    return JuryVerdict(
        proof_integrity=proof_integrity,
        code_quality=code_quality,
        verdict=verdict,
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
        model=resolved_model,
        parse_warning="; ".join(warnings) if warnings else None,
    )
