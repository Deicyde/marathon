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
