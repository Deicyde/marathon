"""Parser for the user-maintained ``order.txt`` file.

Each chapter entry is a non-indented header line of the form::

    <input-tex-file> -> <output-folder-name>

Optionally followed by indented continuation lines providing free-form
per-chapter instructions / targets. Common leading whitespace is stripped
from the continuation block. Blank lines and ``#`` comments are ignored
(except blank lines inside an instruction block, which are preserved).
"""

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-only; avoids an import cycle
    from marathon.ledger import Target


@dataclass(frozen=True)
class OrderEntry:
    input_file: str
    output_folder: str
    instructions: str = ""


def parse_order_file(path: Path) -> list[OrderEntry]:
    if not path.is_file():
        raise FileNotFoundError(f"order.txt not found at {path}")

    entries: list[OrderEntry] = []
    seen_inputs: set[str] = set()
    seen_outputs: set[str] = set()

    pending_input: str | None = None
    pending_output: str | None = None
    pending_lines: list[str] = []

    def finalize() -> None:
        nonlocal pending_input, pending_output
        if pending_input is None or pending_output is None:
            return
        instructions = textwrap.dedent("\n".join(pending_lines)).strip()
        entries.append(
            OrderEntry(
                input_file=pending_input,
                output_folder=pending_output,
                instructions=instructions,
            )
        )
        pending_input = None
        pending_output = None
        pending_lines.clear()

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        no_comment = raw.split("#", 1)[0]
        stripped = no_comment.strip()

        if not stripped:
            # Blank (or comment-only) line. Preserve as a paragraph break inside
            # an in-progress instruction block; ignore otherwise.
            if pending_input is not None and pending_lines:
                pending_lines.append("")
            continue

        is_indented = no_comment[0] in (" ", "\t")

        if not is_indented:
            # New header line — finalize previous entry first.
            finalize()

            if "->" not in stripped:
                raise ValueError(f"{path}:{lineno}: missing '->' separator: {raw!r}")
            left, right = stripped.split("->", 1)
            input_file = left.strip()
            output_folder = right.strip()

            if not input_file or not output_folder:
                raise ValueError(f"{path}:{lineno}: empty filename or folder name in {raw!r}")
            if input_file in seen_inputs:
                raise ValueError(f"{path}:{lineno}: duplicate input file {input_file!r}")
            if output_folder in seen_outputs:
                raise ValueError(f"{path}:{lineno}: duplicate output folder {output_folder!r}")

            seen_inputs.add(input_file)
            seen_outputs.add(output_folder)
            pending_input = input_file
            pending_output = output_folder
        else:
            if pending_input is None:
                raise ValueError(
                    f"{path}:{lineno}: indented continuation line before any chapter entry: {raw!r}"
                )
            pending_lines.append(no_comment.rstrip())

    finalize()
    return entries


def import_order_as_targets(order_path: Path) -> list["Target"]:
    """Phase-7 legacy importer: map an existing ``order.txt`` into coarse
    ledger targets, so order.txt-driven projects migrate into the targets
    ledger (plan §3 Phase 7: ``order.txt`` is *demoted* to a legacy
    importer, NOT deleted).

    This is a SEPARATE, additive entry point — :func:`parse_order_file`
    and every existing caller of it are UNCHANGED. One ``kind='statement'``
    target per chapter (the chapter is the coarsest unit ``order.txt``
    expresses; the per-statement sorry/axiom intake modes in
    ``marathon.plan`` produce the fine-grained targets). Mapping:

    * ``name`` = ``order:<output_folder>`` (unique per chapter, since the
      parser already rejects duplicate output folders);
    * ``kind`` = ``'statement'`` (a coarse book-chapter unit, not a single
      Lean decl);
    * ``source_ref`` = the chapter's input ``.tex`` filename (the human
      origin), so the firewall is respected — this records the *citation*,
      never the file's contents;
    * ``lean_file`` = the output folder (where the chapter's Lean lands);
    * ``notes`` = the chapter's free-form per-chapter instructions, so the
      operator-curated targets survive the migration;
    * ``gate_policy`` defaults to the Target default ('human') — coarse
      chapter targets are a human-review unit until the per-statement
      planner refines them.

    Edges are NOT derived here: ``order.txt``'s top-to-bottom order is a
    submission order, not a semantic dependency DAG (the plan keeps that
    distinction — real dep edges come from kernel cones, not file order).
    """
    from marathon.ledger import Target

    targets: list["Target"] = []
    for entry in parse_order_file(order_path):
        targets.append(
            Target(
                name=f"order:{entry.output_folder}",
                kind="statement",
                source_ref=entry.input_file,
                lean_file=entry.output_folder,
                lean_decl=None,
                notes=entry.instructions or None,
            )
        )
    return targets
