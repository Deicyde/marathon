"""Tests for the spec-auditor (marathon.spec_auditor).

The spec-auditor is the firewalled Claude role behind a spec card (phase-6a,
plan §2 ruling 5). It renders a target theorem's *trust kernel* into three
advisory things — an informal rendering, certifiable kernel-shrink
suggestions, and one delta sentence — and it is **advisory** (returns
``None`` on any failure, never raises) and **firewalled** (never shown the
source ``.tex``: it renders from the Lean alone; faithfulness is the human's
job).

Covered here, all with a monkeypatched subprocess (no real ``claude``):

* Prompt content (the firewall): the absolute-firewall sentence, the
  "never assert a shrink without a runnable certificate" iron rule, and the
  absence of any source/book/tex-comparison request.
* JSON parsing: strict happy path, fenced extraction, lenient string-field
  recovery, garbage → None.
* kernel_shrink discipline: a suggestion missing a runnable certificate is
  dropped (no certificate, no shrink), and ``certificate_obligations``
  extracts the surviving snippets for the probe phase.
* Context assembly: the target type and the kernel members' VALUES are
  embedded, but no ``.tex`` path / source text is ever assembled; the
  machine delta class is passed through; prompt travels via stdin not argv.
* None-on-failure: missing CLI, missing prompt file, exec error, nonzero
  exit, empty stdout, no-usable-fields — advisory means never raising out.
"""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from marathon import spec_auditor
from marathon.spec_auditor import (
    SpecAudit,
    KernelShrink,
    audit_spec_card,
    certificate_obligations,
)


# --- Fixture helpers ----------------------------------------------------------


@dataclass
class FakeMember:
    """A member-like object matching the documented thin interface
    (``.name`` / ``.type_pp`` / ``.value_pp``)."""

    name: str
    type_pp: Optional[str]
    value_pp: Optional[str] = None


@dataclass
class FakeCard:
    """A SpecCard-like object: ``.target`` / ``.kernel_members`` /
    ``.evidence`` (the interface spec_auditor codes against)."""

    target: FakeMember
    kernel_members: list
    evidence: Optional[str] = None


def _make_card(
    target_type: str = "∀ (x : MyProject.Widget), MyProject.IsSmooth x",
    member_value: str = "fun x => ContMDiff 𝓘(ℝ) 𝓘(ℝ) ⊤ x",
    evidence: Optional[str] = "axioms: [propext]; probes: 2 passed; tags: []",
) -> FakeCard:
    return FakeCard(
        target=FakeMember(
            name="MyProject.widget_smooth",
            type_pp=target_type,
            value_pp=None,  # theorem ⇒ no value
        ),
        kernel_members=[
            FakeMember(
                name="MyProject.IsSmooth",
                type_pp="MyProject.Widget → Prop",
                value_pp=member_value,
            )
        ],
        evidence=evidence,
    )


def _install_claude(
    monkeypatch,
    response: str = "",
    returncode: int = 0,
    raise_oserror: bool = False,
    calls: list | None = None,
):
    """Fake both ``shutil.which`` and ``subprocess.run`` inside
    marathon.spec_auditor. Records ``(cmd, kwargs)`` into ``calls`` when
    given (same idiom as test_jury)."""
    monkeypatch.setattr(
        spec_auditor.shutil, "which", lambda name: "/fake/bin/claude"
    )

    def fake_run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if raise_oserror:
            raise OSError(7, "Argument list too long")
        return SimpleNamespace(stdout=response, stderr="", returncode=returncode)

    monkeypatch.setattr(spec_auditor.subprocess, "run", fake_run)


STRICT_HAPPY = (
    '{"informal_rendering": "Every widget is smooth.", '
    '"kernel_shrink": [{"member": "MyProject.IsSmooth", '
    '"claim": "MyProject.IsSmooth is defeq to ContMDiff over the trivial model", '
    '"certificate": "example : MyProject.IsSmooth = '
    'ContMDiff 𝓘(ℝ) 𝓘(ℝ) ⊤ := rfl", "confidence": "medium"}], '
    '"delta_prose": "The statement is an equivalent refactor."}'
)


# --- Prompt content: the firewall + the certificate iron rule -------------------


def test_prompt_md_states_firewall_and_certificate_rule_and_no_source_compare():
    """The on-disk rubric must (a) declare the absolute firewall, (b) state
    the 'never assert a shrink without a runnable certificate' iron rule, and
    (c) never ask the model to consult/weigh the source book/tex."""
    text = (
        Path(spec_auditor.__file__).parent / "prompts" / "spec_audit.md"
    ).read_text()

    # (a) The firewall sentence: never shown the source, renders from Lean alone.
    assert "FIREWALL" in text
    assert "never" in text.lower() and "source" in text.lower()
    assert "the Lean ALONE" in text  # render from the Lean alone, not source

    # (b) The certificate iron rule, stated as an instruction.
    assert "never assert a shrink without a runnable certificate" in text.lower()

    # (c) Source/book/tex appear ONLY as firewall exclusions ("never shown
    #     the .tex"), NEVER as a comparison instruction. The renderer is told
    #     faithfulness-to-source is the HUMAN's job and out of scope, and is
    #     explicitly forbidden from comparing against any book/source.
    assert "out of scope" in text.lower()
    assert "HUMAN" in text
    lowered = text.lower()
    # Every mention of the source is a prohibition, not a request: the rubric
    # must contain the negative instruction and must NOT contain affirmative
    # "compare to / match the book"-style asks.
    assert "do not compare against any book" in lowered
    assert "must not ask for it" in lowered
    # The renderer is never DIRECTED to compare against the source. (The
    # rubric does tell the *human* to "check against the source themselves" —
    # that is the firewall, not a request to the model.)
    for affirmative in (
        "you must compare",
        "you should compare",
        "compare your rendering to the book",
        "the book says",
        "original statement (from book)",
    ):
        assert affirmative not in lowered, affirmative


def test_prompt_assembled_for_call_contains_firewall_and_no_tex(
    tmp_path, monkeypatch
):
    """The assembled prompt that actually reaches claude carries the rubric's
    firewall and embeds NO source text / .tex path."""
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)

    audit_spec_card(_make_card())

    prompt = calls[0][1]["input"]
    assert "FIREWALL" in prompt
    # The rubric NAMES ".tex" only to firewall it off ("never shown the
    # .tex"); what must never appear is an actual SOURCE-TEXT path or content
    # injected by the assembly. We assert the only ".tex" mentions are the
    # firewall prohibitions in the rubric, and that no source file path (a
    # concrete ``*.tex`` filename) was assembled into context.
    import re as _re

    tex_paths = _re.findall(r"\b[\w./-]+\.tex\b", prompt)
    assert tex_paths == [], tex_paths
    # Section headers come only from the (firewall-bearing) rubric and the
    # audit-evidence assembly — never a "source"/"book" content section.
    assert "## Source" not in prompt
    assert "## Book" not in prompt


# --- Context assembly: kernel values in, source text never ----------------------


def test_context_embeds_target_type_and_kernel_member_values(tmp_path, monkeypatch):
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)

    card = _make_card(
        target_type="THE_TARGET_TYPE_MARKER",
        member_value="THE_MEMBER_VALUE_MARKER",
        evidence="THE_EVIDENCE_MARKER",
    )
    audit_spec_card(card)

    prompt = calls[0][1]["input"]
    # Target type, the kernel member's name + value, and the evidence note are
    # all assembled into context.
    assert "THE_TARGET_TYPE_MARKER" in prompt
    assert "MyProject.IsSmooth" in prompt
    assert "THE_MEMBER_VALUE_MARKER" in prompt  # the kernel member's VALUE
    assert "THE_EVIDENCE_MARKER" in prompt
    # Prompt travels via stdin, never argv (E2BIG class).
    assert all("THE_TARGET_TYPE_MARKER" not in part for part in calls[0][0])


def test_machine_delta_class_passed_through_to_prompt(tmp_path, monkeypatch):
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)

    audit_spec_card(_make_card(), machine_delta="weakened")

    prompt = calls[0][1]["input"]
    assert "weakened" in prompt
    assert "do not override it" in prompt.lower()


def test_no_delta_section_without_machine_delta(tmp_path, monkeypatch):
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)

    audit_spec_card(_make_card())  # machine_delta defaults to None

    prompt = calls[0][1]["input"]
    # The assembled delta SECTION (distinct from the rubric's description of
    # the optional field) is absent when no machine delta is passed.
    assert "ground truth for your" not in prompt
    assert "classified this change as" not in prompt


def test_empty_kernel_is_described_not_omitted(tmp_path, monkeypatch):
    """A target whose cone has no project-local defs is fully Mathlib
    vocabulary; the prompt should say so rather than show a blank section."""
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)

    card = FakeCard(
        target=FakeMember(name="T", type_pp="∀ x : ℝ, x = x", value_pp=None),
        kernel_members=[],
    )
    audit_spec_card(card)

    prompt = calls[0][1]["input"]
    assert "no project-local" in prompt


def test_long_pp_is_truncated_with_marker(tmp_path, monkeypatch):
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)
    monkeypatch.setattr(spec_auditor, "MAX_PP_CHARS", 50)

    card = _make_card(target_type="X" * 500)
    audit_spec_card(card)

    prompt = calls[0][1]["input"]
    assert "... (truncated at 50 chars)" in prompt
    assert prompt.count("X") < 500


# --- JSON parsing: happy / fenced / lenient / garbage ---------------------------


def test_strict_json_happy_path(tmp_path, monkeypatch):
    _install_claude(monkeypatch, response=STRICT_HAPPY)

    a = audit_spec_card(_make_card())

    assert a is not None
    assert a.informal_rendering == "Every widget is smooth."
    assert a.delta_prose == "The statement is an equivalent refactor."
    assert len(a.kernel_shrink) == 1
    s = a.kernel_shrink[0]
    assert s.member == "MyProject.IsSmooth"
    assert s.certificate.startswith("example : MyProject.IsSmooth")
    assert s.confidence == "medium"
    assert a.parse_warning is None


def test_fenced_json_is_extracted(tmp_path, monkeypatch):
    _install_claude(
        monkeypatch,
        response="Here is the spec audit:\n```json\n" + STRICT_HAPPY + "\n```\n",
    )

    a = audit_spec_card(_make_card())
    assert a is not None
    assert a.informal_rendering == "Every widget is smooth."
    assert len(a.kernel_shrink) == 1


def test_lenient_recovery_drops_shrinks_but_keeps_strings(tmp_path, monkeypatch):
    """An unescaped quote inside ``informal_rendering`` breaks strict
    json.loads; the per-field regex recovers the string fields. kernel_shrink
    is a structured field we cannot trust under a malformed object, so it is
    dropped and the warning says so."""
    broken = (
        '{"informal_rendering": "The "smooth" widgets are dense.", '
        '"kernel_shrink": [], '
        '"delta_prose": "Strengthened."}'
    )
    _install_claude(monkeypatch, response=broken)

    a = audit_spec_card(_make_card())

    assert a is not None
    assert a.informal_rendering is not None
    assert "smooth" in a.informal_rendering
    assert a.delta_prose == "Strengthened."
    assert a.kernel_shrink == ()
    assert a.parse_warning is not None
    assert "lenient-parse fallback" in a.parse_warning


def test_garbage_response_returns_none(tmp_path, monkeypatch):
    _install_claude(monkeypatch, response="I cannot render this, sorry!")
    assert audit_spec_card(_make_card()) is None


def test_no_usable_fields_returns_none(tmp_path, monkeypatch):
    """A well-formed but empty object (no rendering, no shrink, no delta) is
    not worth a card."""
    _install_claude(
        monkeypatch,
        response='{"informal_rendering": "", "kernel_shrink": [], "delta_prose": ""}',
    )
    assert audit_spec_card(_make_card()) is None


# --- kernel_shrink discipline: no certificate ⇒ not a shrink --------------------


def test_shrink_without_certificate_is_dropped(tmp_path, monkeypatch):
    """The iron rule: a kernel-shrink claim with no runnable certificate just
    moves trust; it must be dropped, not surfaced as an obligation."""
    resp = (
        '{"informal_rendering": "Every widget is smooth.", '
        '"kernel_shrink": ['
        '{"member": "MyProject.IsSmooth", "claim": "trust me it is ContMDiff", '
        '"certificate": "", "confidence": "high"}, '
        '{"member": "MyProject.IsSmooth", '
        '"claim": "it is defeq to ContMDiff", '
        '"certificate": "example : MyProject.IsSmooth = ContMDiff 𝓘(ℝ) 𝓘(ℝ) ⊤ := rfl", '
        '"confidence": "low"}], '
        '"delta_prose": ""}'
    )
    _install_claude(monkeypatch, response=resp)

    a = audit_spec_card(_make_card())

    assert a is not None
    # Only the one with a real certificate survives.
    assert len(a.kernel_shrink) == 1
    assert a.kernel_shrink[0].certificate.startswith("example :")
    assert a.parse_warning is not None
    assert "without a runnable certificate" in a.parse_warning


def test_shrink_missing_member_is_dropped(tmp_path, monkeypatch):
    resp = (
        '{"informal_rendering": "X.", '
        '"kernel_shrink": ['
        '{"claim": "c", "certificate": "example : a = b := rfl"}], '
        '"delta_prose": ""}'
    )
    _install_claude(monkeypatch, response=resp)

    a = audit_spec_card(_make_card())
    assert a is not None
    assert a.kernel_shrink == ()


# --- certificate_obligations extraction -----------------------------------------


def test_certificate_obligations_extracts_snippets():
    audit = SpecAudit(
        informal_rendering="r",
        kernel_shrink=(
            KernelShrink(
                member="A",
                claim="a",
                certificate="example : A = M := rfl",
                confidence="high",
            ),
            KernelShrink(
                member="B",
                claim="b",
                certificate="example : B = N := by decide",
                confidence="low",
            ),
        ),
    )
    obligations = certificate_obligations(audit)
    assert obligations == [
        "example : A = M := rfl",
        "example : B = N := by decide",
    ]


def test_certificate_obligations_skips_blank_and_handles_empty():
    # Blank certificate (defensive — coercion normally drops these) is skipped.
    audit = SpecAudit(
        kernel_shrink=(
            KernelShrink(member="A", claim="a", certificate="   "),
            KernelShrink(member="B", claim="b", certificate="example : B = N := rfl"),
        ),
    )
    assert certificate_obligations(audit) == ["example : B = N := rfl"]
    assert certificate_obligations(SpecAudit()) == []


def test_certificate_obligations_round_trips_from_a_live_audit(tmp_path, monkeypatch):
    """End to end: a happy audit's surviving certificates are exactly what the
    probe phase will receive."""
    _install_claude(monkeypatch, response=STRICT_HAPPY)
    a = audit_spec_card(_make_card())
    assert a is not None
    assert certificate_obligations(a) == [
        "example : MyProject.IsSmooth = ContMDiff 𝓘(ℝ) 𝓘(ℝ) ⊤ := rfl"
    ]


# --- render helpers -------------------------------------------------------------


def test_render_rendering_carries_llm_caveat():
    a = SpecAudit(informal_rendering="Every widget is smooth.")
    out = a.render_rendering()
    assert spec_auditor.LLM_RENDER_CAVEAT in out
    assert "Every widget is smooth." in out


def test_render_line_summarizes_counts():
    a = SpecAudit(
        informal_rendering="r",
        kernel_shrink=(
            KernelShrink(member="A", claim="a", certificate="example : A = M := rfl"),
        ),
        delta_prose="d",
    )
    line = a.render_line()
    assert "rendering=yes" in line and "shrinks=1" in line and "delta=yes" in line


# --- Model resolution + env hygiene ---------------------------------------------


def test_model_resolution_arg_beats_env(tmp_path, monkeypatch):
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)
    monkeypatch.setenv("MARATHON_CLAUDE_MODEL", "env-model")

    a = audit_spec_card(_make_card(), model="arg-model")
    assert a is not None and a.model == "arg-model"
    cmd = calls[0][0]
    assert cmd[cmd.index("--model") + 1] == "arg-model"

    a2 = audit_spec_card(_make_card())
    assert a2 is not None and a2.model == "env-model"


def test_api_key_scrubbed_from_subprocess_env(tmp_path, monkeypatch):
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")

    audit_spec_card(_make_card())

    assert "ANTHROPIC_API_KEY" not in calls[0][1]["env"]


# --- None on any failure (advisory ⇒ never raises) ------------------------------


def test_none_when_claude_cli_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(spec_auditor.shutil, "which", lambda name: None)
    assert audit_spec_card(_make_card()) is None


def test_none_when_prompt_file_missing(tmp_path, monkeypatch):
    _install_claude(monkeypatch, response=STRICT_HAPPY)
    # Point the module at a directory with no spec_audit.md so the prompt-file
    # guard fires (without touching the real prompts/ dir).
    monkeypatch.setattr(
        spec_auditor.Path, "is_file", lambda self: False, raising=False
    )
    assert audit_spec_card(_make_card()) is None


def test_none_on_exec_oserror(tmp_path, monkeypatch):
    _install_claude(monkeypatch, raise_oserror=True)
    assert audit_spec_card(_make_card()) is None  # swallowed, not raised


def test_none_on_nonzero_exit(tmp_path, monkeypatch):
    _install_claude(monkeypatch, response="boom", returncode=1)
    assert audit_spec_card(_make_card()) is None


def test_none_on_empty_stdout(tmp_path, monkeypatch):
    _install_claude(monkeypatch, response="   \n")
    assert audit_spec_card(_make_card()) is None


def test_missing_attrs_on_member_do_not_raise(tmp_path, monkeypatch):
    """A member-like object missing the documented attrs degrades to
    placeholders rather than raising (advisory robustness)."""
    calls: list = []
    _install_claude(monkeypatch, response=STRICT_HAPPY, calls=calls)

    card = SimpleNamespace(
        target=SimpleNamespace(),  # no name/type_pp/value_pp
        kernel_members=[SimpleNamespace()],
        evidence=None,
    )
    a = audit_spec_card(card)
    assert a is not None  # no raise
    prompt = calls[0][1]["input"]
    assert "(unnamed)" in prompt or "(unknown)" in prompt
