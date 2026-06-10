"""Regression tests for safe solution extraction (marathon.skeleton).

Historical failure mode: ``_extract_solution`` rmtree'd the destination
chapter folder *before* inspecting the result tar, so a malformed tar (or
one simply missing the expected output folder) destroyed the previous
attempt's output with nothing to replace it. The fix stages the chapter
in a temp dir next to the destination, validates it is non-empty, then
atomically swaps it into place — a bad tar must leave the prior contents
byte-for-byte intact.

``marathon.refine`` imports ``_extract_solution`` from ``marathon.skeleton``,
so these tests cover both call sites.
"""

import io
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from marathon.skeleton import _atomic_replace_dir, _extract_solution

EXPECTED = PurePosixPath("Output/Chapter1")


# --- Fixture helpers ----------------------------------------------------------


def _make_tar(tar_path: Path, files: dict[str, str], dirs: tuple[str, ...] = ()) -> Path:
    """Build a .tar.gz containing ``files`` (name -> text) and bare ``dirs``."""
    with tarfile.open(tar_path, "w:gz") as tf:
        for d in dirs:
            info = tarfile.TarInfo(name=d)
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tar_path


def _make_repo_with_existing_chapter(tmp_path: Path) -> tuple[Path, Path]:
    """Repo dir with a pre-existing chapter output we must not lose."""
    repo_dir = tmp_path / "repo"
    target = repo_dir / "Output" / "Chapter1"
    target.mkdir(parents=True)
    (target / "old.lean").write_text("-- previous attempt's content\n")
    (target / "sub").mkdir()
    (target / "sub" / "nested.lean").write_text("-- nested previous content\n")
    return repo_dir, target


def _no_staging_leftovers(repo_dir: Path) -> bool:
    """No ``.marathon-*`` staging/holding dirs left behind anywhere."""
    return not list(repo_dir.rglob(".marathon-*"))


# --- Valid tar: atomic swap-in ------------------------------------------------


def test_valid_tar_replaces_existing_chapter(tmp_path):
    repo_dir, target = _make_repo_with_existing_chapter(tmp_path)
    tar_path = _make_tar(
        tmp_path / "solution.tar.gz",
        {
            "Output/Chapter1/New.lean": "theorem fresh : True := trivial\n",
            "Output/Chapter1/deep/Deeper.lean": "-- nested new content\n",
        },
    )

    found, log_updated, unexpected, cross = _extract_solution(
        tar_path, EXPECTED, repo_dir, tmp_path / "marathon.md"
    )

    assert found is True
    assert (target / "New.lean").read_text().startswith("theorem fresh")
    assert (target / "deep" / "Deeper.lean").is_file()
    # Wipe-and-replace semantics: stale files from the previous attempt are
    # gone (Aristotle's deletes propagate for the primary chapter).
    assert not (target / "old.lean").exists()
    assert not (target / "sub").exists()
    assert cross == []
    assert _no_staging_leftovers(repo_dir)


def test_valid_tar_creates_chapter_when_absent(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    tar_path = _make_tar(
        tmp_path / "solution.tar.gz",
        {"Output/Chapter1/New.lean": "-- content\n"},
    )

    found, _, _, _ = _extract_solution(
        tar_path, EXPECTED, repo_dir, tmp_path / "marathon.md"
    )

    assert found is True
    assert (repo_dir / "Output" / "Chapter1" / "New.lean").is_file()
    assert _no_staging_leftovers(repo_dir)


def test_wrapper_directory_is_stripped(tmp_path):
    """Aristotle sometimes wraps everything in ``submission_aristotle/``."""
    repo_dir, target = _make_repo_with_existing_chapter(tmp_path)
    log_dest = tmp_path / "marathon.md"
    tar_path = _make_tar(
        tmp_path / "solution.tar.gz",
        {
            "submission_aristotle/Output/Chapter1/New.lean": "-- wrapped\n",
            "submission_aristotle/marathon.md": "# log\n",
        },
    )

    found, log_updated, _, _ = _extract_solution(tar_path, EXPECTED, repo_dir, log_dest)

    assert found is True
    assert log_updated is True
    assert (target / "New.lean").read_text() == "-- wrapped\n"
    assert not (target / "old.lean").exists()
    assert log_dest.read_text() == "# log\n"
    assert _no_staging_leftovers(repo_dir)


# --- Bad tars: previous output must survive untouched --------------------------


def test_missing_folder_leaves_existing_chapter_intact(tmp_path):
    """A tar without the expected folder must not destroy prior output.

    This is the core regression: the old implementation rmtree'd the
    destination before checking the tar, so this scenario lost the whole
    previous chapter.
    """
    repo_dir, target = _make_repo_with_existing_chapter(tmp_path)
    tar_path = _make_tar(
        tmp_path / "solution.tar.gz",
        {"garbage.txt": "not a chapter\n", "notes/aside.md": "irrelevant\n"},
    )

    found, log_updated, unexpected, cross = _extract_solution(
        tar_path, EXPECTED, repo_dir, tmp_path / "marathon.md"
    )

    # found=False is what makes the caller set OUTPUT_FOLDER_MISSING —
    # preserved behavior.
    assert found is False
    assert log_updated is False
    assert "garbage.txt" in unexpected
    # Previous contents are byte-for-byte intact.
    assert (target / "old.lean").read_text() == "-- previous attempt's content\n"
    assert (target / "sub" / "nested.lean").is_file()
    assert _no_staging_leftovers(repo_dir)


def test_folder_with_no_files_leaves_existing_chapter_intact(tmp_path):
    """A bare directory entry (zero files) fails non-empty validation."""
    repo_dir, target = _make_repo_with_existing_chapter(tmp_path)
    tar_path = _make_tar(
        tmp_path / "solution.tar.gz",
        files={},
        dirs=("Output/", "Output/Chapter1/"),
    )

    found, _, _, _ = _extract_solution(
        tar_path, EXPECTED, repo_dir, tmp_path / "marathon.md"
    )

    assert found is False
    assert (target / "old.lean").read_text() == "-- previous attempt's content\n"
    assert _no_staging_leftovers(repo_dir)


def test_corrupt_tar_leaves_existing_chapter_intact(tmp_path):
    """An unreadable tar raises, but prior output and cleanup still hold."""
    repo_dir, target = _make_repo_with_existing_chapter(tmp_path)
    bogus = tmp_path / "solution.tar.gz"
    bogus.write_bytes(b"this is not a tar archive")

    with pytest.raises(tarfile.ReadError):
        _extract_solution(bogus, EXPECTED, repo_dir, tmp_path / "marathon.md")

    assert (target / "old.lean").read_text() == "-- previous attempt's content\n"
    assert (target / "sub" / "nested.lean").is_file()
    assert _no_staging_leftovers(repo_dir)


# --- Overlay (additional writable paths) semantics unchanged -------------------


def test_overlay_extras_still_overlay_not_wipe(tmp_path):
    """Cross-chapter extras keep overlay semantics alongside the new swap."""
    repo_dir, target = _make_repo_with_existing_chapter(tmp_path)
    sibling = repo_dir / "Output" / "Chapter2"
    sibling.mkdir(parents=True)
    (sibling / "keep.lean").write_text("-- not echoed by Aristotle\n")
    tar_path = _make_tar(
        tmp_path / "solution.tar.gz",
        {
            "Output/Chapter1/New.lean": "-- primary\n",
            "Output/Chapter2/Moved.lean": "-- cross-chapter write\n",
        },
    )

    found, _, _, cross = _extract_solution(
        tar_path,
        EXPECTED,
        repo_dir,
        tmp_path / "marathon.md",
        additional_writable_paths=[PurePosixPath("Output/Chapter2")],
    )

    assert found is True
    assert cross == ["Output/Chapter2/Moved.lean"]
    # Overlay: the new file landed AND the un-echoed file survived.
    assert (sibling / "Moved.lean").is_file()
    assert (sibling / "keep.lean").is_file()
    # Primary still wipe-and-replace.
    assert not (target / "old.lean").exists()
    assert _no_staging_leftovers(repo_dir)


# --- _atomic_replace_dir unit coverage -----------------------------------------


def test_atomic_replace_dir_swaps_and_cleans_up(tmp_path):
    parent = tmp_path / "repo"
    parent.mkdir()
    old = parent / "Chapter"
    old.mkdir()
    (old / "stale.lean").write_text("old")
    new = parent / ".staged"
    new.mkdir()
    (new / "fresh.lean").write_text("new")

    _atomic_replace_dir(new, old)

    assert (old / "fresh.lean").read_text() == "new"
    assert not (old / "stale.lean").exists()
    assert not new.exists()
    assert _no_staging_leftovers(parent)


def test_atomic_replace_dir_no_preexisting_target(tmp_path):
    parent = tmp_path / "repo"
    parent.mkdir()
    new = parent / ".staged"
    new.mkdir()
    (new / "fresh.lean").write_text("new")
    target = parent / "Chapter"

    _atomic_replace_dir(new, target)

    assert (target / "fresh.lean").read_text() == "new"
    assert not new.exists()
