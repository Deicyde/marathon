"""Tests for auto-un-verify of a verified review issue whose reviewable
content changes (marathon.review.reverify + the conductor pass).

Contract under test:

* ``verified_staleness`` diffs an issue's CURRENT carded/in-code reviewable
  set against the manifest record frozen at verify time, CONSERVATIVELY:
  detects a new card, a changed statement, and a removed/renamed decl; is
  not-stale when current == manifest, when manifest-for-issue is empty, and
  when the source can't be read (no false flag).
* ``unverify_for_rereview`` removes the verified label, deletes the
  state.json entry, and posts ONE comment (all via mocked gh).
* the conductor's ``_maybe_reopen_stale_verified`` re-opens a stale verified
  issue, skips a fresh verified one, and skips an excluded (running-job) one.

Every gh / GitHub boundary is monkeypatched: no test invokes gh.
"""

from __future__ import annotations

import marathon.conductor as conductor
import marathon.review.reverify as reverify
from marathon.review.approved_statements import ApprovedStatement, save_manifest
from marathon.review.config import ChapterRegistry, ReviewConfig, ReviewLabels
from marathon.review.state import IssueState, ReviewState, load_state, save_state


# --- fixtures ---------------------------------------------------------------


def make_cfg(tmp_path) -> ReviewConfig:
    repo = tmp_path / "repo"
    (repo / "Demo" / "Chapter14").mkdir(parents=True, exist_ok=True)
    return ReviewConfig(
        repo_dir=repo,
        config_path=repo / ".marathon/review/config.toml",
        github_repo="owner/Demo",
        parent_issue=1,
        target_path_template="Demo/Chapter{chapter}",
        tracker_section_pattern="### Chapter {chapter}:",
        labels=ReviewLabels(),
        chapters={14: ChapterRegistry(chapter=14, entries=[(22, "Lemma A")])},
    )


def write_source(cfg: ReviewConfig, *, body: str) -> None:
    (cfg.repo_dir / "Demo" / "Chapter14" / "X.lean").write_text(body)


def issue_body_for(decls: list[str], bodies: dict[str, str]) -> str:
    """An issue body whose ```lean block declares ``decls`` (signatures only)."""
    lean = "\n".join(bodies[d] for d in decls)
    return f"### Lean signatures\n\n```lean\n{lean}\n```\n"


def issue_body_with_name_bullet(
    lean_decls: list[str], bodies: dict[str, str], bullet_names: list[str]
) -> str:
    """A mixed issue body: a ```lean signature block for ``lean_decls`` PLUS a
    ``### Lean declarations`` section citing ``bullet_names`` by NAME only (the
    chapter-intake shape — backtick'd names, no ```lean block)."""
    parts: list[str] = []
    if lean_decls:
        lean = "\n".join(bodies[d] for d in lean_decls)
        parts.append(f"### Lean signatures\n\n```lean\n{lean}\n```")
    if bullet_names:
        bullets = "\n".join(
            f"- [X.lean:1-2](http://x) — `{n}`" for n in bullet_names
        )
        parts.append(f"### Lean declarations\n\n{bullets}")
    return "\n\n".join(parts) + "\n"


def freeze(cfg: ReviewConfig, issue_num: int, sigs: dict[str, str]) -> None:
    """Write the approved-statements manifest with ``sigs`` frozen for the
    issue (each value is an ALREADY-normalized signature)."""
    save_manifest(
        cfg.repo_dir,
        {
            name: ApprovedStatement(
                decl_name=name, signature=sig, issue=issue_num,
                chapter=14, verified_ts="2026-06-20T00:00:00-04:00",
            )
            for name, sig in sigs.items()
        },
    )


def seed_verified(cfg: ReviewConfig, issue_num: int) -> None:
    save_state(
        cfg,
        ReviewState(issues={
            issue_num: IssueState(
                status="verified", verdict_ts="2026-06-20T00:00:00-04:00",
            )
        }),
    )


def patch_body(monkeypatch, body):
    """Make reverify.verified_staleness see ``body`` as the issue body
    (a callable taking issue_num, or a constant string/None)."""
    def fake(issue_num, repo):
        return body(issue_num) if callable(body) else body
    monkeypatch.setattr("marathon.review.github.issue_body", fake)


# --- verified_staleness -----------------------------------------------------


def test_staleness_detects_new_card(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    foo = "theorem foo : True := by trivial"
    bar = "theorem bar : True := by trivial"
    write_source(cfg, body=f"{foo}\n{bar}\n")
    # Only `foo` was frozen; the body now ALSO cites `bar` → new card.
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(monkeypatch, issue_body_for(["foo", "bar"], {"foo": foo, "bar": bar}))

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["new card: bar"]


def test_staleness_name_bullet_in_source_is_stale_new_card(tmp_path, monkeypatch):
    """Positive control (FIX 1 must not over-suppress): a genuine IN-SOURCE
    decl carded via a `### Lean declarations` name bullet but absent from the
    manifest IS freezable (extract_signature finds it in source) → new card.
    Mirrors #156's `ManifoldOrientation.toPointwiseOrientation`."""
    cfg = make_cfg(tmp_path)
    foo = "theorem foo : True := by trivial"
    newdecl = "def ManifoldOrientation.toPointwiseOrientation : Nat := 0"
    # Both decls live in source; only `foo` was frozen. The body cites the new
    # decl by NAME ONLY in `### Lean declarations`, but it IS in source →
    # capture WOULD have frozen it → genuine new card.
    write_source(cfg, body=f"{foo}\n{newdecl}\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_with_name_bullet(
            ["foo"], {"foo": foo}, ["ManifoldOrientation.toPointwiseOrientation"]
        ),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["new card: ManifoldOrientation.toPointwiseOrientation"]


def test_staleness_shared_decl_frozen_by_other_issue_is_not_new_card(tmp_path, monkeypatch):
    """FIX (the #24 loop): a decl carded by this issue but already frozen by a
    DIFFERENT issue is shared, already-reviewed content (e.g. a base type
    `DifferentialForm` owned by the issue that introduced it, referenced by a
    downstream issue). The manifest is keyed by decl name with a single owner,
    so it is absent from THIS issue's frozen subset — but it is NOT a new card,
    and flagging it re-opened the downstream issue forever."""
    cfg = make_cfg(tmp_path)
    foo = "theorem foo : True := by trivial"
    shared = "def Base : Nat := 0"
    write_source(cfg, body=f"{foo}\n{shared}\n")
    # Issue 22 froze `foo`; issue 99 (another issue) already froze `Base`.
    save_manifest(cfg.repo_dir, {
        "foo": ApprovedStatement("foo", "theorem foo : True", 22, 14),
        "Base": ApprovedStatement("Base", "def Base : Nat", 99, 14),
    })
    # Issue 22's body now also cites the shared `Base` by name.
    patch_body(
        monkeypatch,
        issue_body_with_name_bullet(["foo"], {"foo": foo}, ["Base"]),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_cross_chapter_name_collision_is_still_new_card(tmp_path, monkeypatch):
    """Adversarial-review regression (Finding A): the shared-decl suppression is
    scoped to the SAME chapter. A genuinely-new, unreviewed decl in THIS issue's
    chapter whose bare name merely collides with an unrelated decl frozen in a
    DIFFERENT chapter must STILL be flagged a new card — a global `name in
    manifest` test would alias them and let the new statement slip in unreviewed."""
    cfg = make_cfg(tmp_path)  # issue 22 is chapter 14
    foo = "theorem foo : True := by trivial"
    collide = "def collide : Nat := 0"
    write_source(cfg, body=f"{foo}\n{collide}\n")  # `collide` is a real chapter-14 decl
    # 22 froze `foo` (ch14); a DIFFERENT decl named `collide` was frozen by issue
    # 88 in a DIFFERENT chapter (11). The bare names alias in the manifest.
    save_manifest(cfg.repo_dir, {
        "foo": ApprovedStatement("foo", "theorem foo : True", 22, 14),
        "collide": ApprovedStatement("collide", "def collide : String", 88, 11),
    })
    patch_body(
        monkeypatch,
        issue_body_with_name_bullet(["foo"], {"foo": foo}, ["collide"]),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["new card: collide"]  # different chapter → genuinely new


def test_staleness_name_only_bullet_not_freezable_is_not_stale(tmp_path, monkeypatch):
    """FIX 1: a decl cited by NAME ONLY in `### Lean declarations` — NOT in
    source and with NO ```lean block defining it — was never freezable, so
    capture never put it in the manifest. It must NOT trigger a spurious
    `new card`, else the issue can never stay verified."""
    cfg = make_cfg(tmp_path)
    foo = "theorem foo : True := by trivial"
    # `foo` is in source and was frozen; `Ghost.bar` is cited by name only in
    # the `### Lean declarations` section, is NOT in source, and has no ```lean
    # block → capture could never freeze it.
    write_source(cfg, body=f"{foo}\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_with_name_bullet(["foo"], {"foo": foo}, ["Ghost.bar"]),
    )

    # Sanity: the carded set DOES include the non-freezable name (asymmetry).
    from marathon.review.verified_decls import extract_declarations_from_issue_body
    carded = extract_declarations_from_issue_body(
        issue_body_with_name_bullet(["foo"], {"foo": foo}, ["Ghost.bar"])
    )
    assert "Ghost.bar" in carded

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_partial_source_read_failure_is_not_stale(tmp_path, monkeypatch):
    """FIX 2: a multi-file chapter where ONE .lean file is unreadable while a
    sibling reads fine. A frozen decl living in the unreadable file would look
    removed/changed though the source is fine → must NOT be stale."""
    cfg = make_cfg(tmp_path)
    chap = cfg.repo_dir / "Demo" / "Chapter14"
    # Two files: A.lean readable, B.lean holds `bar` but will fail to read.
    (chap / "A.lean").write_text("theorem foo : True := by trivial\n")
    b = chap / "B.lean"
    b.write_text("theorem bar : True := by trivial\n")
    # Freeze `bar` (lives in the file we will make unreadable).
    freeze(cfg, 22, {"bar": "theorem bar : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["bar"], {"bar": "theorem bar : True := by sorry"}),
    )

    real_read_text = type(b).read_text

    def flaky_read_text(self, *args, **kwargs):
        if self.name == "B.lean":
            raise OSError("simulated unreadable file")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr("pathlib.Path.read_text", flaky_read_text)

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_detects_changed_signature(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # Frozen as `: True`; source now states `: False` → changed statement.
    write_source(cfg, body="theorem foo : False := by sorry\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["foo"], {"foo": "theorem foo : False := by sorry"}),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["changed statement: foo"]


def test_staleness_dropped_but_present_decl_is_not_removed(tmp_path, monkeypatch):
    """A frozen decl the issue stopped carding, but which is STILL in source
    unchanged, is NOT "removed/renamed" — a dropped-but-present statement is not
    a reviewable-content change. Flagging it re-opened verified issues on every
    reconcile, and pairing that flag with capture's prune risked unfreezing a
    still-guarded signature. Only the genuinely-new `baz` re-opens here."""
    cfg = make_cfg(tmp_path)
    write_source(cfg, body="theorem foo : True := by trivial\n")  # foo still present
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["baz"], {"baz": "theorem baz : True := by trivial"}),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["new card: baz"]  # foo NOT flagged — still present, unchanged


def test_staleness_body_defined_decl_absent_from_source_is_not_stale(tmp_path, monkeypatch):
    """FIX (the #45 loop): a decl still carded via its ```lean block whose bare
    name is NOT in source — because the in-source decl is namespace-qualified,
    or the citation is signature-only — was frozen by capture FROM the body
    block. It must be compared against the body block, NOT flagged
    "removed from code", else the issue re-opens on every reconcile forever.
    Code-existence of a cited decl is the audit's concern, not this diff's."""
    cfg = make_cfg(tmp_path)
    # Source has the decl only under a namespace-qualified spelling; the bare
    # `foo` the card cites resolves nowhere in source.
    write_source(cfg, body="theorem Ns.foo : True := by sorry\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["foo"], {"foo": "theorem foo : True := by sorry"}),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_changed_statement_via_body_block(tmp_path, monkeypatch):
    """The changed-statement check still fires for a body-defined decl: the
    body block now states `: False` where the manifest froze `: True`."""
    cfg = make_cfg(tmp_path)
    write_source(cfg, body="theorem Ns.foo : True := by sorry\n")  # bare foo absent
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["foo"], {"foo": "theorem foo : False := by sorry"}),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["changed statement: foo"]


def test_staleness_detects_removed_name_only_gone_from_code(tmp_path, monkeypatch):
    """Genuine removal is still caught: a decl carded by NAME ONLY (no ```lean
    block) that is absent from a trustworthy source resolves nowhere → the
    reviewed signature is genuinely gone → removed/renamed."""
    cfg = make_cfg(tmp_path)
    write_source(cfg, body="theorem unrelated : True := by trivial\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_with_name_bullet([], {}, ["foo"]),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["removed/renamed: foo"]


def test_reverify_loop_closes_after_recapture_prunes(tmp_path, monkeypatch):
    """End-to-end loop closure (BUG #1: #23/#44/#95/#97). A dirty manifest — a
    stale bare `foo` lingering beside the still-cited `Ns.foo` — makes
    verified_staleness flag `removed/renamed: foo` on every reconcile tick,
    endlessly re-opening the verified issue. A single re-verify
    (capture_on_verify) PRUNES the uncited bare entry, after which staleness
    reports not-stale: the loop converges instead of flapping forever."""
    from marathon.review.approved_statements import capture_on_verify, load_manifest

    cfg = make_cfg(tmp_path)
    # Source defines only the namespace-qualified decl.
    write_source(cfg, body="theorem Ns.foo : True := by sorry\n")
    # Dirty manifest as an earlier verify left it: bare `foo` lingers uncited.
    freeze(cfg, 22, {"foo": "theorem foo : True", "Ns.foo": "theorem Ns.foo : True"})
    body = issue_body_for(["Ns.foo"], {"Ns.foo": "theorem Ns.foo : True := by sorry"})
    patch_body(monkeypatch, body)

    # Before re-verify: the stale bare `foo` re-opens the issue.
    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert stale
    assert reasons == ["removed/renamed: foo"]

    # A single re-verify prunes the uncited bare `foo`.
    capture_on_verify(cfg, 22)
    assert "foo" not in load_manifest(cfg.repo_dir)

    # After: not stale — the loop has converged.
    stale2, reasons2 = reverify.verified_staleness(cfg, 22)
    assert not stale2
    assert reasons2 == []


def test_staleness_not_stale_when_current_equals_manifest(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    foo = "theorem foo : True := by trivial"
    write_source(cfg, body=f"{foo}\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(monkeypatch, issue_body_for(["foo"], {"foo": foo}))

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_empty_manifest_for_issue_is_not_stale(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    write_source(cfg, body="theorem foo : True := by trivial\n")
    # Manifest exists but for a DIFFERENT issue → manifest-for-22 is empty.
    freeze(cfg, 99, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["foo"], {"foo": "theorem foo : True := by trivial"}),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_source_read_failure_is_not_stale(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    # Source is EMPTY (folder has no .lean / unreadable) but the decl is still
    # carded — must NOT false-flag as removed/changed.
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(
        monkeypatch,
        issue_body_for(["foo"], {"foo": "theorem foo : True := by sorry"}),
    )

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


def test_staleness_body_unfetchable_is_not_stale(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    write_source(cfg, body="theorem foo : True := by trivial\n")
    freeze(cfg, 22, {"foo": "theorem foo : True"})
    patch_body(monkeypatch, None)  # body fetch failed

    stale, reasons = reverify.verified_staleness(cfg, 22)
    assert not stale
    assert reasons == []


# --- unverify_for_rereview --------------------------------------------------


def test_unverify_removes_label_deletes_state_and_comments(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    seed_verified(cfg, 22)
    assert load_state(cfg).get(22) is not None

    calls: list[list[str]] = []

    def fake_gh(*args, **kwargs):
        calls.append(list(args))
        class CP:
            returncode = 0
            stdout = '{"labels": []}' if args[:2] == ("issue", "view") else ""
            stderr = ""
        return CP()

    monkeypatch.setattr("marathon.review.github.gh", fake_gh)

    ok = reverify.unverify_for_rereview(cfg, 22, ["new card: bar"])
    assert ok is True

    # state entry deleted (deck re-surfaces the issue).
    assert load_state(cfg).get(22) is None

    # exactly one label-edit (remove verified), one read-back, and one comment.
    edits = [c for c in calls if c[:1] == ["issue"] and "edit" in c]
    comments = [c for c in calls if c[:2] == ["issue", "comment"]]
    assert len(edits) == 1
    assert "--remove-label" in edits[0]
    assert cfg.labels.verified in edits[0]
    assert "--add-label" not in edits[0]
    assert len(comments) == 1
    # the comment summarizes the reason.
    body = comments[0][comments[0].index("--body") + 1]
    assert "new card(s): bar" in body
    assert "♻️" in body


def test_unverify_leaves_manifest_frozen(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    seed_verified(cfg, 22)
    freeze(cfg, 22, {"foo": "theorem foo : True"})

    monkeypatch.setattr(
        "marathon.review.github.gh",
        lambda *a, **k: type("CP", (), {
            "returncode": 0,
            "stdout": '{"labels": []}' if a[:2] == ("issue", "view") else "",
            "stderr": "",
        })(),
    )
    reverify.unverify_for_rereview(cfg, 22, ["changed statement: foo"])

    from marathon.review.approved_statements import load_manifest
    # The manifest is deliberately NOT touched — a later change still diffs.
    assert "foo" in load_manifest(cfg.repo_dir)


def test_unverify_preserves_local_state_when_label_removal_silently_fails(
    tmp_path, monkeypatch, capsys,
):
    cfg = make_cfg(tmp_path)
    seed_verified(cfg, 22)

    def fake_gh(*args, **kwargs):
        # edit returns success, but read-back still contains review:verified.
        out = (
            '{"labels": [{"name": "review:verified"}]}'
            if args[:2] == ("issue", "view") else ""
        )
        return type("CP", (), {"returncode": 0, "stdout": out, "stderr": ""})()

    monkeypatch.setattr("marathon.review.github.gh", fake_gh)

    assert reverify.unverify_for_rereview(cfg, 22, ["new card: bar"]) is False
    assert load_state(cfg).get(22) is not None  # durable retry anchor retained
    assert "keeping local verified state" in capsys.readouterr().out


# --- conductor pass ---------------------------------------------------------


def test_conductor_reopens_stale_skips_fresh_and_excluded(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)

    # Two verified issues: 22 is stale (new card), 23 is fresh, plus a third
    # stale one (24) that is EXCLUDED because a job is running on it.
    cfg.chapters[14] = ChapterRegistry(
        chapter=14, entries=[(22, "A"), (23, "B"), (24, "C")]
    )
    save_state(cfg, ReviewState(issues={
        22: IssueState(status="verified", verdict_ts="2026-06-20T00:00:00-04:00"),
        23: IssueState(status="verified", verdict_ts="2026-06-20T00:00:00-04:00"),
        24: IssueState(status="verified", verdict_ts="2026-06-20T00:00:00-04:00"),
    }))

    # 22 and 24 are stale; 23 is fresh.
    def fake_staleness(_cfg, num):
        if num in (22, 24):
            return (True, [f"new card: card{num}"])
        return (False, [])

    reopened: list[int] = []

    def fake_unverify(_cfg, num, reasons):
        reopened.append(num)
        return True

    monkeypatch.setattr(reverify, "verified_staleness", fake_staleness)
    monkeypatch.setattr(reverify, "unverify_for_rereview", fake_unverify)

    n = conductor._maybe_reopen_stale_verified(cfg, exclude_issues=frozenset({24}))

    assert n == 1
    assert reopened == [22]  # 23 fresh (skipped), 24 excluded (running job)


def test_conductor_pass_skips_non_verified(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    save_state(cfg, ReviewState(issues={
        22: IssueState(status="rejected", verdict_ts="2026-06-20T00:00:00-04:00",
                       notes="x"),
    }))

    def boom(*a, **k):
        raise AssertionError("staleness must not be consulted for non-verified")

    monkeypatch.setattr(reverify, "verified_staleness", boom)
    n = conductor._maybe_reopen_stale_verified(cfg, exclude_issues=frozenset())
    assert n == 0
