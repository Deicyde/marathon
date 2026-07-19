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

    comments = []

    def fake_post(issue, repo, body, **kwargs):
        comments.append((issue, repo, body, kwargs))

    recorded = {}
    monkeypatch.setattr(review, "post_issue_comment_rest", fake_post)
    monkeypatch.setattr(
        review, "converge_issue_labels_rest", lambda *a, **k: True
    )
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
        issue_num=7,
        notes=note,
        comment=None,
        no_refine=True,
        operation_id="saved-review-7",
    ))

    assert comments, "no issue comment posted"
    issue, repo, body, kwargs = comments[0]
    assert (issue, repo) == (7, "o/r")
    assert kwargs == {"operation_id": "saved-review-7"}
    assert "marathon-reject-note" in body                # embedded marker
    assert "- A: do X" in body                            # verbatim note
    assert recorded["note"] == note                      # still recorded locally


def test_cmd_reject_does_not_record_mixed_verified_rejected_labels(monkeypatch):
    from types import SimpleNamespace

    import pytest
    import marathon.review.review as review

    monkeypatch.setattr(review, "post_issue_comment_rest", lambda *a, **k: None)
    monkeypatch.setattr(
        review, "converge_issue_labels_rest", lambda *a, **k: False
    )
    monkeypatch.setattr(
        review,
        "record_rejection",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("local reject must wait for rejected-only labels")
        ),
    )
    monkeypatch.setattr(review, "_ensure_conductor", lambda cfg: None)
    monkeypatch.setattr(
        review,
        "load_config",
        lambda: SimpleNamespace(
            github_repo="o/r",
            labels=SimpleNamespace(
                rejected="review:rejected", verified="review:verified"
            ),
            state_path="/tmp/state.json",
        ),
    )

    with pytest.raises(RuntimeError, match="rejected-only"):
        review.cmd_reject(
            SimpleNamespace(
                issue_num=7,
                notes="fix it",
                comment=None,
                no_refine=False,
            )
        )


def test_cmd_reject_rest_comment_failure_is_concise_and_records_nothing(
    monkeypatch,
):
    """A failed GitHub projection keeps the saved deck review retryable.

    In particular, the very long mathematical note must not be copied into the
    command-shaped error and no issue-level rejection may be queued locally.
    """
    from types import SimpleNamespace

    import pytest
    import marathon.review.review as review

    note = "SENSITIVE_REJECT_NOTE " + ("canonical boundary model; " * 80)
    recorded = []
    converged = []

    def fail_comment(*_args, **_kwargs):
        raise RuntimeError(
            "GitHub REST comment for #171 could not be published: rate limited"
        )

    monkeypatch.setattr(review, "post_issue_comment_rest", fail_comment)
    monkeypatch.setattr(
        review,
        "converge_issue_labels_rest",
        lambda *a, **k: converged.append((a, k)) or True,
    )
    monkeypatch.setattr(
        review,
        "record_rejection",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    monkeypatch.setattr(
        review,
        "load_config",
        lambda: SimpleNamespace(
            github_repo="o/r",
            labels=SimpleNamespace(
                rejected="review:rejected", verified="review:verified"
            ),
            state_path="/tmp/state.json",
        ),
    )

    with pytest.raises(RuntimeError) as caught:
        review.cmd_reject(
            SimpleNamespace(
                issue_num=171,
                notes=note,
                comment=None,
                no_refine=False,
            )
        )

    message = str(caught.value)
    assert "GitHub projection failed for rejection #171" in message
    assert "saved declaration decisions remain safe to retry" in message
    assert "SENSITIVE_REJECT_NOTE" not in message
    assert len(message) < 300
    assert recorded == []
    assert converged == []  # never publish a rejected label without its note


def test_embed_extract_roundtrip_with_arrow_in_note():
    """A note containing '-->' must not break the HTML comment / extraction."""
    from marathon.review.reject_note import embed_reject_note, extract_reject_note
    note = "FIX A: rewrite f x --> g x; keep B"
    emb = embed_reject_note(note)
    assert emb.count("-->") == 1                       # only the close marker
    assert extract_reject_note(emb) == note            # byte-exact recovery
