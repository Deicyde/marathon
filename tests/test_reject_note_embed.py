"""Reject-note embedding in the GitHub comment — the producer half of the
GitHub-discovered conductor queue (host writes; collaborator's conductor reads)."""
from __future__ import annotations

from marathon.review.reject_note import (
    embed_reject_note,
    extract_reject_note,
)


def test_embed_extract_roundtrip():
    note = "FIX these declarations (rejected):\n- Foo.bar: do X"
    assert extract_reject_note(embed_reject_note(note)) == note


def test_embed_is_one_html_comment_so_it_renders_invisible():
    body = embed_reject_note("hello")
    assert body.startswith("<!--") and body.rstrip().endswith("-->")


def test_extract_none_for_plain_or_empty_comment():
    assert extract_reject_note("❌ REJECTED — see body for findings.") is None
    assert extract_reject_note("") is None
    assert extract_reject_note(None) is None


def test_extract_from_full_comment_with_visible_prefix():
    note = "KEEP these declarations exactly as written:\n- A\nFIX: B"
    comment = "❌ REJECTED — see body.\n\n" + embed_reject_note(note)
    out = extract_reject_note(comment)
    assert out == note and "marathon-reject-note" not in out


def test_cmd_reject_embeds_note_in_github_comment(monkeypatch):
    """cmd_reject mirrors the verbatim note into the GitHub reject comment
    (and still records it locally)."""
    from types import SimpleNamespace

    import marathon.review.review as review

    calls = []

    def fake_gh(*a, **k):
        calls.append(a)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    recorded = {}
    monkeypatch.setattr(review, "gh", fake_gh)
    monkeypatch.setattr(review, "record_rejection",
                        lambda cfg, n, notes: recorded.update(note=notes))
    monkeypatch.setattr(review, "_ensure_conductor", lambda cfg: None)
    cfg = SimpleNamespace(
        github_repo="o/r",
        labels=SimpleNamespace(rejected="review:rejected", verified="review:verified"),
        state_path="/tmp/state.json",
    )
    monkeypatch.setattr(review, "load_config", lambda: cfg)

    note = "FIX these declarations (rejected):\n- A: do X"
    review.cmd_reject(SimpleNamespace(
        issue_num=7, notes=note, comment=None, no_refine=True))

    bodies = [a[a.index("--body") + 1] for a in calls
              if "comment" in a and "--body" in a]
    assert bodies, "no issue comment posted"
    assert "marathon-reject-note" in bodies[0]          # embedded marker
    assert "- A: do X" in bodies[0]                      # verbatim note
    assert recorded["note"] == note                      # still recorded locally
