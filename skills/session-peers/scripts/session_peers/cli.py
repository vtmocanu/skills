"""Cli for the session-peers CLI."""

from __future__ import annotations
import argparse
import sys
from . import constants as sp_constants
from . import diagnostics as sp_diagnostics, lifecycle as sp_lifecycle
from . import shim as sp_shim
from . import buddy as sp_buddy, diagnostics as sp_diagnostics, hooks as sp_hooks, maintenance as sp_maintenance, messaging as sp_messaging, topics as sp_topics

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="peers.py",
        description="Cross-session messaging between Claude Code and Codex CLI.",
    )
    sub = p.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="live Claude sessions and Codex threads")
    p_list.add_argument("--json", action="store_true", help="machine-readable output")
    p_list.set_defaults(func=sp_diagnostics.cmd_list)

    def add_message_source(parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument("--message", help="the message body")
        group.add_argument(
            "--message-file", metavar="PATH", help="read the message body from PATH"
        )

    p_send = sub.add_parser("send", help="send one message in either direction")
    p_send.add_argument(
        "--to",
        required=True,
        metavar="codex:<name|uuid>|cc:<name|uuid>|buddy",
        help="the peer",
    )
    add_message_source(p_send)
    p_send.add_argument(
        "--from-thread", metavar="UUID", help="the Codex thread sending (cc: targets)"
    )
    p_send.add_argument("--from-name", metavar="NAME", help="sender name for the tag")
    p_send.add_argument("--from-sid", metavar="ID", help="sender session id for the tag")
    p_send.add_argument(
        "--from-socket", metavar="PATH", help="sender socket for the reply tag"
    )
    p_send.add_argument("--json", action="store_true", help="machine-readable result")
    p_send.set_defaults(func=sp_messaging.cmd_send)

    p_ask = sub.add_parser(
        "ask", help="send a correlated request to Claude and wait for its reply"
    )
    p_ask.add_argument(
        "--to", required=True, metavar="cc:<name|uuid>|buddy", help="the Claude peer"
    )
    add_message_source(p_ask)
    p_ask.add_argument(
        "--from-thread",
        metavar="UUID",
        help="the Codex thread asking (defaults to CODEX_THREAD_ID)",
    )
    p_ask.add_argument(
        "--timeout",
        type=float,
        help="seconds to wait (default: 600; maximum: 3600)",
    )
    p_ask.add_argument("--json", action="store_true", help="machine-readable result")
    p_ask.set_defaults(func=sp_messaging.cmd_ask)

    p_reply = sub.add_parser(
        "reply", help="reply exactly once to a pending correlated request"
    )
    p_reply.add_argument("--request", required=True, metavar="UUID")
    add_message_source(p_reply)
    p_reply.add_argument("--json", action="store_true", help="machine-readable result")
    p_reply.set_defaults(func=sp_messaging.cmd_reply)

    p_dispatch = sub.add_parser(
        "dispatch",
        help="send a correlated request to Claude and return immediately",
    )
    p_dispatch.add_argument(
        "--to", required=True, metavar="cc:<name|uuid>|buddy", help="the Claude peer"
    )
    add_message_source(p_dispatch)
    p_dispatch.add_argument(
        "--from-thread",
        metavar="UUID",
        help="the Codex thread dispatching (defaults to CODEX_THREAD_ID)",
    )
    p_dispatch.add_argument(
        "--timeout",
        type=float,
        help="request lifetime in seconds (default: 600; maximum: 3600)",
    )
    p_dispatch.add_argument(
        "--json", action="store_true", help="machine-readable result"
    )
    p_dispatch.set_defaults(func=sp_messaging.cmd_dispatch)

    p_await = sub.add_parser(
        "await", help="consume the reply to a dispatched request by its id"
    )
    p_await.add_argument("--request", required=True, metavar="UUID")
    p_await.add_argument(
        "--from-thread",
        metavar="UUID",
        help="the Codex thread that dispatched it (defaults to CODEX_THREAD_ID)",
    )
    p_await.add_argument(
        "--timeout",
        type=float,
        help="seconds this call waits, distinct from the request lifetime "
        "(default: 600; maximum: 3600)",
    )
    p_await.add_argument("--json", action="store_true", help="machine-readable result")
    p_await.set_defaults(func=sp_messaging.cmd_await)

    p_wait = sub.add_parser("wait", help="wait for a Claude peer registry state")
    p_wait.add_argument(
        "--for", dest="for_peer", required=True, metavar="cc:<name|uuid>|buddy"
    )
    p_wait.add_argument(
        "--state", choices=("idle", "busy"), default="idle", help="target state"
    )
    p_wait.add_argument(
        "--timeout",
        type=float,
        help="seconds to wait (default: 600; maximum: 3600)",
    )
    p_wait.add_argument("--json", action="store_true", help="machine-readable result")
    p_wait.set_defaults(func=sp_messaging.cmd_wait_peer)

    p_shim = sub.add_parser("shim", help="run as one Codex thread's peer (foreground)")
    p_shim.add_argument("--thread", required=True, metavar="UUID")
    p_shim.set_defaults(func=sp_shim.cmd_shim)

    p_up = sub.add_parser("up", help="register a thread and start its shim")
    p_up.add_argument("target", nargs="?", metavar="name|uuid")
    p_up.set_defaults(func=sp_lifecycle.cmd_up)

    p_down = sub.add_parser("down", help="stop a shim; with a target, unregister it")
    p_down.add_argument("target", nargs="?", metavar="name|uuid")
    p_down.set_defaults(func=sp_lifecycle.cmd_down)

    p_restart = sub.add_parser(
        "restart",
        help="restart a shim on the current code, keeping its reply budget and grants",
    )
    p_restart.add_argument("target", metavar="name|uuid")
    p_restart.set_defaults(func=sp_lifecycle.cmd_restart)

    p_budget = sub.add_parser("budget", help="reply budget maintenance")
    bsub = p_budget.add_subparsers(dest="budget_cmd")
    p_reset = bsub.add_parser("reset", help="clear a thread's reply budget")
    p_reset.add_argument("thread", metavar="name|uuid|buddy")
    p_reset.set_defaults(func=sp_buddy.cmd_budget)
    p_allow = bsub.add_parser(
        "allow", help="let one requester receive up to N consecutive replies"
    )
    p_allow.add_argument("thread", metavar="name|uuid|buddy")
    p_allow.add_argument(
        "--replies",
        type=int,
        required=True,
        metavar="N",
        help="total consecutive replies for this sequence (1..%d)" % sp_constants.BUDGET_ALLOW_MAX,
    )
    p_allow.add_argument(
        "--for-session",
        metavar="UUID",
        help="the requesting Claude session (default: the calling session)",
    )
    p_allow.add_argument(
        "--as", dest="as_identity", metavar="cc:<uuid>|codex:<uuid>",
        help="the calling session, when it cannot be detected",
    )
    p_allow.set_defaults(func=sp_buddy.cmd_budget)
    p_budget.set_defaults(func=sp_buddy.cmd_budget, budget_cmd=None, thread=None)

    p_buddy = sub.add_parser("buddy", help="bind, show, ping or clear this session's buddy")
    p_buddy.add_argument(
        "--as", dest="as_identity", metavar="cc:<uuid>|codex:<uuid>",
        help="the calling session, when it cannot be detected",
    )
    p_buddy.add_argument("--json", action="store_true", help="machine-readable output")
    buddy_sub = p_buddy.add_subparsers(dest="buddy_cmd")

    def buddy_action(name, help_text, **kwargs):
        parser = buddy_sub.add_parser(name, help=help_text, **kwargs)
        # SUPPRESS: an option given before the action must survive the subparser.
        parser.add_argument(
            "--as", dest="as_identity", default=argparse.SUPPRESS,
            metavar="cc:<uuid>|codex:<uuid>",
        )
        parser.add_argument(
            "--json", action="store_true", default=argparse.SUPPRESS,
            help="machine-readable output",
        )
        return parser

    buddy_action("show", "report the buddy's status without attaching")
    p_buddy_set = buddy_action(
        "set",
        "bind a buddy by name or UUID",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "A bare name or UUID that is both a Claude session and a Codex thread\n"
            "(an attached Codex thread's UUID also names its shim) is refused;\n"
            "prefix the kind. Name matching never matches the caller itself, and\n"
            "without a target the caller's own name is looked up:\n"
            "  peers.py buddy set codex:<uuid>\n"
            "  peers.py buddy set cc:<uuid>\n"
            "  peers.py buddy set codex:my-thread --uses review,brainstorm\n"
            "  peers.py buddy set"
        ),
    )
    p_buddy_set.add_argument(
        "target",
        nargs="?",
        metavar="[cc:|codex:|@]name|uuid",
        help="the peer to bind (default: the other peer sharing this session's name)",
    )
    p_buddy_set.add_argument(
        "--uses",
        metavar="a,b",
        help="advisory scope (default: all of %s)" % ", ".join(sp_constants.BUDDY_USES),
    )
    p_buddy_set.add_argument(
        "--replies",
        type=int,
        metavar="N",
        help="a finite TOTAL of replies (1..%d, default %d for a Claude "
        "session's Codex buddy) this buddy may send beyond the per-sequence "
        "cap, across sequences and shim restarts; revoked by `buddy clear` or "
        "binding another buddy" % (sp_constants.BUDDY_REPLIES_MAX, sp_constants.BUDDY_REPLIES_DEFAULT),
    )
    buddy_action("ping", "report status, attaching a Codex buddy's shim if needed")
    buddy_action("clear", "unbind the buddy")
    p_buddy.set_defaults(func=sp_buddy.cmd_buddy, buddy_cmd=None)

    p_hook = sub.add_parser("session-hook", help="Codex SessionStart entry point")
    p_hook.add_argument(
        "--auto-attach", action="store_true", help="attach the triggering session UUID"
    )
    p_hook.set_defaults(func=sp_hooks.cmd_session_hook)

    p_hook_reconcile = sub.add_parser("hook-reconcile", help=argparse.SUPPRESS)
    p_hook_reconcile.add_argument("--thread", metavar="UUID")
    p_hook_reconcile.set_defaults(func=sp_hooks.cmd_hook_reconcile)

    p_install = sub.add_parser("install-hook", help="add the SessionStart entry")
    p_install.add_argument(
        "--auto-attach",
        action="store_true",
        help="automatically expose each starting or resumed Codex thread",
    )
    p_install.set_defaults(func=sp_hooks.cmd_install_hook)

    p_topic = sub.add_parser("topic", help="pull-only topic logs any peer can post to")
    tsub = p_topic.add_subparsers(dest="topic_cmd")
    p_tpost = tsub.add_parser("post", help="append one entry to a topic")
    p_tpost.add_argument("topic", metavar="TOPIC")
    tsource = p_tpost.add_mutually_exclusive_group(required=True)
    tsource.add_argument("--message", help="the entry text")
    tsource.add_argument("--message-file", metavar="PATH", help="read the entry text from PATH")
    tsource.add_argument("--json-file", metavar="PATH", help="a JSON value stored as data")
    p_tpost.add_argument("--kind", metavar="KIND", help="an opaque label for readers")
    p_tpost.add_argument(
        "--as", dest="as_identity", metavar="cc:<uuid>|codex:<uuid>",
        help="the posting session, when it cannot be detected",
    )
    p_tpost.add_argument("--json", action="store_true", help="machine-readable result")
    p_tpost.set_defaults(func=sp_topics.cmd_topic)
    p_ttail = tsub.add_parser("tail", help="print entries after a cursor")
    p_ttail.add_argument("topic", metavar="TOPIC")
    p_ttail.add_argument(
        "--since", type=int, metavar="SEQ",
        help="print entries after SEQ (default: the last --limit entries)",
    )
    p_ttail.add_argument(
        "--limit", type=int, default=sp_constants.TOPIC_TAIL_DEFAULT, metavar="N",
        help="at most N entries (default %d, maximum %d)"
        % (sp_constants.TOPIC_TAIL_DEFAULT, sp_constants.TOPIC_TAIL_MAX),
    )
    p_ttail.add_argument("--json", action="store_true", help="machine-readable output")
    p_ttail.set_defaults(func=sp_topics.cmd_topic)
    p_tlist = tsub.add_parser("list", help="topics with their last seq and time")
    p_tlist.add_argument("--json", action="store_true", help="machine-readable output")
    p_tlist.set_defaults(func=sp_topics.cmd_topic)
    p_topic.set_defaults(func=sp_topics.cmd_topic, topic_cmd=None)

    p_gc = sub.add_parser("gc", help="prune inactive bridge metadata")
    p_gc.add_argument("--days", type=float, help="retention in days (default: 7)")
    p_gc.add_argument("--dry-run", action="store_true", help="print without deleting")
    p_gc.set_defaults(func=sp_maintenance.cmd_gc)

    p_doctor = sub.add_parser("doctor", help="check the bridge end to end")
    p_doctor.set_defaults(func=sp_diagnostics.cmd_doctor)

    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0
