"""Unit tests for marathon.order (the order.txt parser)."""

from pathlib import Path

import pytest

from marathon.order import OrderEntry, parse_order_file


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "order.txt"
    p.write_text(text)
    return p


def test_basic_headers(tmp_path):
    entries = parse_order_file(
        _write(tmp_path, "chap01.tex -> Chapter01\nchap02.tex -> Chapter02\n")
    )
    assert entries == [
        OrderEntry("chap01.tex", "Chapter01", ""),
        OrderEntry("chap02.tex", "Chapter02", ""),
    ]


def test_whitespace_around_arrow_is_stripped(tmp_path):
    # A header line must NOT be indented (leading whitespace marks a
    # continuation line); whitespace *around the arrow* is what gets stripped.
    (entry,) = parse_order_file(_write(tmp_path, "chap01.tex   ->   Chapter01   \n"))
    assert entry.input_file == "chap01.tex"
    assert entry.output_folder == "Chapter01"


def test_blank_and_comment_lines_ignored(tmp_path):
    text = "# a comment\n\nchap01.tex -> Chapter01\n   # indented comment-only\n\nchap02.tex -> Chapter02\n"
    entries = parse_order_file(_write(tmp_path, text))
    assert [e.output_folder for e in entries] == ["Chapter01", "Chapter02"]
    assert all(e.instructions == "" for e in entries)


def test_trailing_comment_on_header_stripped(tmp_path):
    (entry,) = parse_order_file(_write(tmp_path, "chap01.tex -> Chapter01  # do this one\n"))
    assert entry == OrderEntry("chap01.tex", "Chapter01", "")


def test_per_chapter_instructions_dedented(tmp_path):
    text = (
        "chap03.tex -> Chapter03\n"
        "    - Prove Theorem 3.5.\n"
        "    - Skip the examples in 3.7.\n"
    )
    (entry,) = parse_order_file(_write(tmp_path, text))
    assert entry.instructions == "- Prove Theorem 3.5.\n- Skip the examples in 3.7."


def test_blank_line_inside_instruction_block_is_paragraph_break(tmp_path):
    text = "chap04.tex -> Chapter04\n    Paragraph one.\n\n    Paragraph two.\n"
    (entry,) = parse_order_file(_write(tmp_path, text))
    assert entry.instructions == "Paragraph one.\n\nParagraph two."


def test_chapter_without_continuation_has_empty_instructions(tmp_path):
    text = "chap01.tex -> Chapter01\nchap02.tex -> Chapter02\n    Just this lemma.\n"
    a, b = parse_order_file(_write(tmp_path, text))
    assert a.instructions == ""
    assert b.instructions == "Just this lemma."


def test_missing_arrow_raises(tmp_path):
    with pytest.raises(ValueError, match="missing '->' separator"):
        parse_order_file(_write(tmp_path, "chap01.tex Chapter01\n"))


def test_empty_filename_or_folder_raises(tmp_path):
    with pytest.raises(ValueError, match="empty filename or folder"):
        parse_order_file(_write(tmp_path, "chap01.tex ->\n"))


def test_duplicate_input_raises(tmp_path):
    text = "chap01.tex -> Chapter01\nchap01.tex -> Chapter02\n"
    with pytest.raises(ValueError, match="duplicate input file"):
        parse_order_file(_write(tmp_path, text))


def test_duplicate_output_raises(tmp_path):
    text = "chap01.tex -> Chapter01\nchap02.tex -> Chapter01\n"
    with pytest.raises(ValueError, match="duplicate output folder"):
        parse_order_file(_write(tmp_path, text))


def test_indented_line_before_any_header_raises(tmp_path):
    with pytest.raises(ValueError, match="continuation line before any chapter entry"):
        parse_order_file(_write(tmp_path, "    orphan instruction\n"))


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_order_file(tmp_path / "does-not-exist.txt")


def test_empty_file_yields_no_entries(tmp_path):
    assert parse_order_file(_write(tmp_path, "\n  \n# only comments\n")) == []


def test_order_entry_is_frozen():
    entry = OrderEntry("a.tex", "A")
    with pytest.raises(Exception):
        entry.input_file = "b.tex"  # type: ignore[misc]
