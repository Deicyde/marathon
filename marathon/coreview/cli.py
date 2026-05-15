"""Argparse wiring for ``marathon coreview <subcmd>``.

Exposes :func:`add_subparser` for ``marathon/__main__.py`` to call when
building the top-level argparser, and :func:`coreview_command` as the
top-level handler.
"""

from __future__ import annotations

import argparse

from marathon.coreview import review as r


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register the ``coreview`` subparser tree on ``subparsers``."""
    p_cv = subparsers.add_parser(
        "coreview",
        help=(
            "Per-declaration final review pass: walk through sub-issues, "
            "record VERIFIED / REJECTED verdicts, queue fixes for the "
            "auto-refine daemon. Reads <repo>/.marathon/coreview/config.toml "
            "for project-specific settings."
        ),
        description=(
            "Coreview is the human-in-the-loop mathematical-accuracy check "
            "before proofs land on a skeleton chapter. Each declaration has "
            "a sub-issue; this command lets you list/show/verify/reject them "
            "in textbook order, with rejections queued via referee.md."
        ),
    )
    cv_sub = p_cv.add_subparsers(dest="coreview_cmd", required=True)

    p_list = cv_sub.add_parser(
        "list",
        help="List sub-issues for the chapter in textbook order with statuses.",
    )
    p_list.add_argument("--chapter", type=int, required=True)
    p_list.set_defaults(func=r.cmd_list)

    p_next = cv_sub.add_parser(
        "next",
        help="Show the next unreviewed sub-issue (skipping verified/rejected).",
    )
    p_next.add_argument("--chapter", type=int, required=True)
    p_next.set_defaults(func=r.cmd_next)

    p_show = cv_sub.add_parser("show", help="Display a specific sub-issue body.")
    p_show.add_argument("issue_num", type=int)
    p_show.set_defaults(func=r.cmd_show)

    p_v = cv_sub.add_parser(
        "verify",
        help=(
            "Mark a sub-issue VERIFIED (statements accepted; default: keep "
            "OPEN, just flip 🟠 → 🟡)."
        ),
    )
    p_v.add_argument("issue_num", type=int)
    p_v.add_argument(
        "--close",
        action="store_true",
        help=(
            "Also close the issue (only when the declaration is FULLY "
            "implemented — no remaining sorrys)."
        ),
    )
    p_v.add_argument(
        "--comment",
        default=None,
        help="Override the short default verdict comment.",
    )
    p_v.set_defaults(func=r.cmd_verify)

    p_r = cv_sub.add_parser(
        "reject",
        help=(
            "Mark a sub-issue REJECTED (label + queue refinement bullet + "
            "auto-launch refine daemon)."
        ),
    )
    p_r.add_argument("issue_num", type=int)
    p_r.add_argument(
        "--notes",
        required=True,
        help=(
            "Rejection notes (file path or inline) — go into referee.md, "
            "NOT the comment."
        ),
    )
    p_r.add_argument("--comment", default=None, help="Override default verdict comment.")
    p_r.add_argument(
        "--no-refine",
        action="store_true",
        help="Don't auto-launch refine daemon (just queue via referee.md).",
    )
    p_r.set_defaults(func=r.cmd_reject)

    p_status = cv_sub.add_parser(
        "refine-status",
        help="Report whether a refine daemon is active for the chapter.",
    )
    p_status.add_argument("--chapter", type=int, required=True)
    p_status.set_defaults(func=r.cmd_refine_status)

    p_start = cv_sub.add_parser(
        "refine-start",
        help="Start the refine daemon explicitly (without rejecting anything).",
    )
    p_start.add_argument("--chapter", type=int, required=True)
    p_start.set_defaults(func=r.cmd_refine_start)

    p_stop = cv_sub.add_parser(
        "refine-stop",
        help=(
            "Stop the refine daemon for this chapter (SIGTERM); current "
            "marathon iteration finishes first."
        ),
    )
    p_stop.add_argument("--chapter", type=int, required=True)
    p_stop.set_defaults(func=r.cmd_refine_stop)

    # The daemon is normally launched indirectly via ``reject`` or
    # ``refine-start``, but is also exposed here as a top-level coreview
    # subcommand so the consumer-repo shim at
    # ``.marathon/coreview/refine_runner.py`` has a stable console entry
    # point. Same args as ``python -m marathon.coreview.daemon``.
    p_daemon = cv_sub.add_parser(
        "daemon",
        help=(
            "Run the auto-refine daemon directly (normally launched "
            "indirectly via `reject` or `refine-start`)."
        ),
    )
    p_daemon.add_argument("--chapter", type=int, required=True)
    p_daemon.add_argument(
        "--once",
        action="store_true",
        help="Process queue once then exit (legacy semantics).",
    )
    p_daemon.set_defaults(func=_run_daemon_subcommand)

    # --- subissues bulk operations -------------------------------------
    p_subs = cv_sub.add_parser(
        "subissues",
        help="Bulk-create or bulk-refresh sub-issue bodies from a drafts file.",
    )
    subs_sub = p_subs.add_subparsers(dest="subissues_cmd", required=True)

    p_sub_create = subs_sub.add_parser(
        "create",
        help=(
            "Bulk-create sub-issues from a drafts/<Chapter>.md file. One "
            "GitHub issue per draft section; attached as sub-issues of the "
            "parent."
        ),
    )
    p_sub_create.add_argument("drafts_file")
    p_sub_create.add_argument(
        "--skip",
        default="",
        help="Comma-separated entry numbers to skip (already created).",
    )
    p_sub_create.set_defaults(func=r.cmd_subissues_create)

    p_sub_refresh = subs_sub.add_parser(
        "refresh",
        help=(
            "Refresh existing sub-issue bodies from a drafts/<Chapter>.md "
            "file. Preserves comments, labels, sub-issue parent link."
        ),
    )
    p_sub_refresh.add_argument("drafts_file")
    p_sub_refresh.add_argument(
        "--only",
        default="",
        help=(
            "Comma-separated entry indices (1-based, in registry order) to "
            "refresh. Default: refresh every entry."
        ),
    )
    p_sub_refresh.set_defaults(func=r.cmd_subissues_refresh)


def _run_daemon_subcommand(args) -> None:
    """Dispatch ``marathon coreview daemon ...`` into the daemon module."""
    from marathon.coreview.daemon import run_daemon
    sys_exit = run_daemon(chapter=args.chapter, once=args.once)
    if sys_exit:
        import sys
        sys.exit(sys_exit)


def coreview_command(args) -> None:
    """Top-level handler dispatched from ``marathon coreview <subcmd>``."""
    args.func(args)
