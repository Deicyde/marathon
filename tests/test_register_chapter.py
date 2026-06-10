"""Tests for the machine-managed ``[[chapters]]`` registry writer and
its CLI verbs (``marathon review register-chapter`` / ``show-registry``).

History these tests guard against (see docs/marathon-v2-plan.md §1 and
docs/v2-analysis/report-review-subsystem.md §2): the bootstrap/audit
coreviewer briefings used to instruct the Claude agent to hand-edit
``.marathon/review/config.toml``'s ``[[chapters]]`` block — the "third
state surface", with documented drift from GitHub reality. Registration
is now a programmatic rewrite (``marathon.review.config``) that:

* preserves everything *before* the registry block byte-for-byte
  (comments, ``[labels]``, top-level keys),
* regenerates the registry block in one stable, commented format,
* refuses loudly rather than risk mangling anything it cannot fully
  parse back (malformed TOML, non-chapters keys after the block).

No subprocesses, no network: the writer is pure file I/O on tmp_path,
and the CLI tests drive the real argparse tree + handlers with
``find_repo_dir`` resolving to tmp_path.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import marathon.review.cli as cli
import marathon.review.config as config_mod
from marathon.review.config import (
    RegistryEditError,
    load_config,
    parse_entry_arg,
    register_chapter,
    update_chapter_entries,
)


# --- helpers -------------------------------------------------------------------

BASE_CONFIG = """\
# GeometricAnalysis review config — hand comments must survive rewrites.
github_repo = "someone/SomeProject"
parent_issue = 1
referee_path = ".marathon/referee.md"  # inline comment, also load-bearing
target_path_template = "SomeProject/Chapter{chapter}"
tracker_section_pattern = "### Chapter {chapter}:"

[labels]
verified = "review:verified"
rejected = "review:rejected"
inflight = "review:in-flight-fix"
"""


def write_config(tmp_path: Path, text: str = BASE_CONFIG) -> Path:
    path = tmp_path / ".marathon" / "review" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


CH14 = [(14, "Lemma 14.7"), (15, "Proposition 14.8")]
CH15 = [(31, "Theorem 15.1: Stokes"), (32, "Corollary 15.2")]


# --- register into a config with no [[chapters]] yet ----------------------------


def test_register_into_empty_registry(tmp_path):
    write_config(tmp_path)

    block = register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)

    # The returned block is the actual on-disk registry section.
    assert "[[chapters]]" in block
    assert '[14, "Lemma 14.7"],' in block
    assert block in (tmp_path / ".marathon/review/config.toml").read_text()

    # And it round-trips through the real loader the review CLI uses.
    cfg = load_config(tmp_path)
    assert list(cfg.chapters) == [14]
    assert cfg.chapters[14].entries == CH14


def test_register_accepts_absolute_target_inside_repo(tmp_path):
    write_config(tmp_path)
    register_chapter(tmp_path, 14, tmp_path / "SomeProject" / "Chapter14", CH14)
    assert load_config(tmp_path).chapters[14].entries == CH14


# --- append a second chapter -----------------------------------------------------


def test_append_second_chapter_keeps_first(tmp_path):
    write_config(tmp_path)
    register_chapter(tmp_path, 15, "SomeProject/Chapter15", CH15)
    register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)

    cfg = load_config(tmp_path)
    assert cfg.chapters[14].entries == CH14
    assert cfg.chapters[15].entries == CH15

    # Stable format: chapters sorted numerically regardless of
    # registration order, and exactly one banner (no accumulation).
    text = (tmp_path / ".marathon/review/config.toml").read_text()
    assert text.index("chapter = 14") < text.index("chapter = 15")
    assert text.count("# --- chapter registries") == 1
    assert text.count("[[chapters]]") == 2


def test_register_existing_chapter_refuses_without_replace(tmp_path):
    write_config(tmp_path)
    register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)

    with pytest.raises(RegistryEditError, match="already registered"):
        register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)

    # --replace overwrites wholesale.
    register_chapter(
        tmp_path, 14, "SomeProject/Chapter14", [(99, "Lemma 14.9")], replace=True
    )
    assert load_config(tmp_path).chapters[14].entries == [(99, "Lemma 14.9")]


# --- update entries in place -----------------------------------------------------


def test_update_entries_in_place(tmp_path):
    write_config(tmp_path)
    register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)
    register_chapter(tmp_path, 15, "SomeProject/Chapter15", CH15)

    new_entries = [(14, "Lemma 14.7"), (16, "Exercise 14.20"), (15, "Proposition 14.8")]
    block = update_chapter_entries(tmp_path, 14, new_entries)

    assert '[16, "Exercise 14.20"],' in block
    cfg = load_config(tmp_path)
    assert cfg.chapters[14].entries == new_entries  # order preserved, not sorted
    assert cfg.chapters[15].entries == CH15  # sibling untouched


def test_update_unregistered_chapter_refuses(tmp_path):
    write_config(tmp_path)
    with pytest.raises(RegistryEditError, match="not registered"):
        update_chapter_entries(tmp_path, 14, CH14)


# --- preservation of unrelated config content -------------------------------------


def test_hand_edited_content_survives_repeated_rewrites(tmp_path):
    config_path = write_config(tmp_path)
    head_before = config_path.read_text()

    register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)
    update_chapter_entries(tmp_path, 14, [(14, "Lemma 14.7")])
    register_chapter(tmp_path, 15, "SomeProject/Chapter15", CH15)

    text = config_path.read_text()
    # Everything before the registry block is preserved byte-for-byte.
    assert text.startswith(head_before)
    # Comments (full-line and inline), [labels], and top-level keys intact.
    assert "# GeometricAnalysis review config" in text
    assert "# inline comment, also load-bearing" in text
    assert "[labels]" in text
    assert 'inflight = "review:in-flight-fix"' in text
    # Exactly one banner after three rewrites.
    assert text.count("# --- chapter registries") == 1

    # The loader still sees the hand-edited config unchanged.
    cfg = load_config(tmp_path)
    assert cfg.github_repo == "someone/SomeProject"
    assert cfg.labels.inflight == "review:in-flight-fix"


def test_substring_with_quotes_and_colons_round_trips(tmp_path):
    write_config(tmp_path)
    tricky = [(14, 'Theorem 14.9: the "unique" extension \\ case')]
    register_chapter(tmp_path, 14, "SomeProject/Chapter14", tricky)
    assert load_config(tmp_path).chapters[14].entries == tricky


def test_preexisting_handwritten_registry_is_absorbed(tmp_path):
    """Configs from before this writer existed have a hand-written
    [[chapters]] block with its own comments; the first programmatic
    write absorbs it into the stable format without losing entries."""
    write_config(
        tmp_path,
        BASE_CONFIG
        + "\n[[chapters]]\nchapter = 14\nentries = [\n"
        '  [14, "Lemma 14.7"],  # hand comment inside the block\n'
        '  [15, "Proposition 14.8"],\n]\n',
    )

    register_chapter(tmp_path, 15, "SomeProject/Chapter15", CH15)

    cfg = load_config(tmp_path)
    assert cfg.chapters[14].entries == CH14  # absorbed, not dropped
    assert cfg.chapters[15].entries == CH15
    text = (tmp_path / ".marathon/review/config.toml").read_text()
    assert text.count("# --- chapter registries") == 1


# --- refusals: never risk mangling the file ----------------------------------------


def test_malformed_toml_is_refused_and_file_untouched(tmp_path):
    broken = BASE_CONFIG + "\n[[chapters]\nchapter = oops\n"  # not TOML
    config_path = write_config(tmp_path, broken)

    with pytest.raises(RegistryEditError, match="does not parse as TOML"):
        register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)

    assert config_path.read_text() == broken  # byte-for-byte untouched


def test_non_chapters_keys_after_block_are_refused(tmp_path):
    """[labels] *after* [[chapters]] would be swallowed by the wholesale
    block rewrite — must refuse loudly, naming the stray keys."""
    text = (
        'github_repo = "someone/SomeProject"\n'
        "parent_issue = 1\n"
        'target_path_template = "SomeProject/Chapter{chapter}"\n'
        "\n[[chapters]]\nchapter = 14\nentries = [[14, \"Lemma 14.7\"]]\n"
        '\n[labels]\nverified = "x"\n'
    )
    config_path = write_config(tmp_path, text)

    with pytest.raises(RegistryEditError, match=r"labels.*after"):
        update_chapter_entries(tmp_path, 14, CH14)

    assert config_path.read_text() == text


def test_target_path_mismatch_is_refused(tmp_path):
    write_config(tmp_path)
    with pytest.raises(RegistryEditError, match="target_path_template"):
        register_chapter(tmp_path, 14, "SomeProject/Chapter15", CH14)
    # Nothing was registered.
    assert load_config(tmp_path).chapters == {}


def test_bad_entries_are_refused(tmp_path):
    write_config(tmp_path)
    with pytest.raises(RegistryEditError, match="empty entry list"):
        register_chapter(tmp_path, 14, "SomeProject/Chapter14", [])
    with pytest.raises(RegistryEditError, match="appears twice"):
        register_chapter(
            tmp_path, 14, "SomeProject/Chapter14",
            [(14, "Lemma 14.7"), (14, "Lemma 14.8")],
        )
    with pytest.raises(RegistryEditError, match="single line"):
        register_chapter(
            tmp_path, 14, "SomeProject/Chapter14", [(14, "Lemma\n14.7")]
        )


def test_missing_config_is_refused(tmp_path):
    with pytest.raises(RegistryEditError, match="not found"):
        register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)


# --- --entry parsing ----------------------------------------------------------------


def test_parse_entry_arg():
    assert parse_entry_arg("14:Lemma 14.7") == (14, "Lemma 14.7")
    # Split on the FIRST colon only — substrings may contain colons.
    assert parse_entry_arg("31:Theorem 15.1: Stokes") == (31, "Theorem 15.1: Stokes")
    # Whitespace around either part is shell noise, not content.
    assert parse_entry_arg(" 14 : Lemma 14.7 ") == (14, "Lemma 14.7")

    for bad in ("Lemma 14.7", "14:", ":Lemma", "fourteen:Lemma", ""):
        with pytest.raises(ValueError):
            parse_entry_arg(bad)


# --- CLI wiring ----------------------------------------------------------------------


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="marathon")
    sub = parser.add_subparsers(dest="command", required=True)
    cli.add_subparser(sub)
    return parser.parse_args(argv)


def test_cli_repeatable_entry_parsing():
    args = _parse([
        "review", "register-chapter",
        "--chapter", "14",
        "--target", "SomeProject/Chapter14",
        "--entry", "14:Lemma 14.7",
        "--entry", "31:Theorem 15.1: Stokes",
    ])
    assert args.chapter == 14
    assert args.target == "SomeProject/Chapter14"
    assert args.entry == ["14:Lemma 14.7", "31:Theorem 15.1: Stokes"]
    assert args.replace is False
    assert args.func is cli._cmd_register_chapter

    # --entry is required (registering an empty chapter is meaningless).
    with pytest.raises(SystemExit):
        _parse([
            "review", "register-chapter",
            "--chapter", "14", "--target", "SomeProject/Chapter14",
        ])


def test_cli_register_chapter_end_to_end(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    monkeypatch.setattr(config_mod, "find_repo_dir", lambda *a, **kw: tmp_path)

    args = _parse([
        "review", "register-chapter",
        "--chapter", "14",
        "--target", "SomeProject/Chapter14",
        "--entry", "14:Lemma 14.7",
        "--entry", "15:Proposition 14.8",
    ])
    cli.review_command(args)

    out = capsys.readouterr().out
    assert "registered chapter 14 (2 entries)" in out
    assert "[[chapters]]" in out  # resulting block is printed
    assert load_config(tmp_path).chapters[14].entries == CH14


def test_cli_register_chapter_bad_entry_exits(tmp_path, monkeypatch):
    write_config(tmp_path)
    monkeypatch.setattr(config_mod, "find_repo_dir", lambda *a, **kw: tmp_path)

    args = _parse([
        "review", "register-chapter",
        "--chapter", "14", "--target", "SomeProject/Chapter14",
        "--entry", "Lemma 14.7",  # no ISSUE: prefix
    ])
    with pytest.raises(SystemExit):
        cli.review_command(args)
    # Refusal happened before any write.
    assert load_config(tmp_path).chapters == {}


def test_cli_show_registry(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    monkeypatch.setattr(config_mod, "find_repo_dir", lambda *a, **kw: tmp_path)

    cli.review_command(_parse(["review", "show-registry"]))
    assert "no chapters registered" in capsys.readouterr().out

    register_chapter(tmp_path, 14, "SomeProject/Chapter14", CH14)
    cli.review_command(_parse(["review", "show-registry"]))
    out = capsys.readouterr().out
    # show-registry prints the identical stable block the writer wrote.
    assert out == (
        config_mod.render_chapters_block(load_config(tmp_path).chapters)
    )
