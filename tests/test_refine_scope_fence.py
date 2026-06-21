"""Focused-rejection scope fence.

A single-issue rejection refine may only write its TARGET chapter — the
cross-chapter / vendor whitelist is dropped, so Aristotle cannot sprawl across
chapters (the #114 failure mode: a Chapter-12-only ask that returned edits to 34
files across six chapters + vendored Mathlib). General iterations keep the full
whitelist.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path, PurePosixPath

from marathon import refine
from marathon.skeleton import _extract_solution

EXPECTED = PurePosixPath("Output/Chapter1")


def _make_tar(tar_path: Path, files: dict[str, str]) -> Path:
    with tarfile.open(tar_path, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tar_path


def _repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    (repo_dir / "Output" / "Chapter1").mkdir(parents=True)
    (repo_dir / "Output" / "Chapter2").mkdir(parents=True)
    (repo_dir / "Output" / "Chapter2" / "keep.lean").write_text("-- pre-existing Ch2\n")
    return repo_dir


# --- the helper that decides the writable scope -------------------------------


def test_writable_paths_fenced_for_focused_rejection(monkeypatch, tmp_path):
    sentinel = [PurePosixPath("Output/Chapter2"), PurePosixPath("Mathlib_4_30")]
    monkeypatch.setattr(refine, "_load_additional_writable_paths",
                        lambda repo, exp: sentinel)
    ep = PurePosixPath("Output/Chapter1")
    # focused rejection → fenced to the target chapter only (empty whitelist)
    assert refine._writable_paths_for_attempt(tmp_path, ep, 96) == []
    # general / autonomous iteration → full cross-chapter / vendor whitelist
    assert refine._writable_paths_for_attempt(tmp_path, ep, None) == sentinel


# --- end-to-end: an empty whitelist actually discards a cross-chapter write ----


def test_whitelisted_cross_chapter_write_is_kept(tmp_path):
    repo_dir = _repo(tmp_path)
    tar = _make_tar(tmp_path / "s.tar.gz", {
        "Output/Chapter1/New.lean": "theorem t : True := trivial\n",
        "Output/Chapter2/Sprawl.lean": "-- cross-chapter edit\n",
    })
    found, _log, _unexpected, cross = _extract_solution(
        tar, EXPECTED, repo_dir, tmp_path / "m.md",
        additional_writable_paths=[PurePosixPath("Output/Chapter2")],
    )
    assert found is True
    assert (repo_dir / "Output/Chapter1/New.lean").is_file()
    assert (repo_dir / "Output/Chapter2/Sprawl.lean").is_file()        # whitelisted → kept
    assert "Output/Chapter2/Sprawl.lean" in cross


def test_fenced_cross_chapter_write_is_discarded(tmp_path):
    repo_dir = _repo(tmp_path)
    tar = _make_tar(tmp_path / "s.tar.gz", {
        "Output/Chapter1/New.lean": "theorem t : True := trivial\n",
        "Output/Chapter2/Sprawl.lean": "-- cross-chapter edit\n",
    })
    # Empty whitelist = exactly what _writable_paths_for_attempt returns for a
    # focused rejection. The target chapter lands; the cross-chapter edit drops.
    found, _log, _unexpected, cross = _extract_solution(
        tar, EXPECTED, repo_dir, tmp_path / "m.md",
        additional_writable_paths=[],
    )
    assert found is True
    assert (repo_dir / "Output/Chapter1/New.lean").is_file()
    assert not (repo_dir / "Output/Chapter2/Sprawl.lean").exists()     # FENCED → discarded
    assert cross == []
