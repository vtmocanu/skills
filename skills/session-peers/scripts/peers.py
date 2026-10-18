#!/usr/bin/env python3
"""Cross-session messaging between Claude Code sessions, Codex CLI threads and
OpenCode V2 sessions.

One stable launcher, one CLI, with bundled runtime modules. See PRD #44 and ``../references/spike-checklist.md`` for the
measurements every mechanism here relies on; the decision letters (D1..D11) in
the comments point at that PRD's decision log.

Subcommands::

    peers.py list [--json]
    peers.py send --to codex:<name|uuid>|cc:<name|uuid>|oc:<title|ses_id>|buddy
                  (--message <text>|--message-file <path>) [--json]
                  [--from-thread <uuid>] [--from-name N] [--from-sid S]
                  [--from-socket P]
    peers.py ask --to cc:<name|uuid>|oc:<title|ses_id>|buddy (--message <text>|--message-file <path>)
                 [--from-thread <uuid>] [--timeout <seconds>] [--json]
    peers.py dispatch --to cc:<name|uuid>|oc:<title|ses_id>|buddy
                      (--message <text>|--message-file <path>)
                      [--from-thread <uuid>] [--timeout <seconds>] [--json]
    peers.py await --request <uuid> [--from-thread <uuid>]
                   [--timeout <seconds>] [--json]
    peers.py reply --request <uuid> (--message <text>|--message-file <path>)
                   [--json]
    peers.py wait --for cc:<name|uuid>|oc:<title|ses_id>|buddy [--state idle|busy] [--timeout <seconds>]
                  [--json]
    peers.py watch start --task LABEL --timeout SECONDS [--wake] -- POLLER ARGV...
    peers.py watch list [--json]
    peers.py watch handled UUID --next ACTION_OR_BLOCKER
    peers.py shim --thread <uuid>
    peers.py up [<name|uuid>]
    peers.py down [<name|uuid>]
    peers.py restart <name|uuid>
    peers.py budget reset <name|uuid|buddy>
    peers.py budget allow <name|uuid|buddy> --replies N [--for-session <uuid>]
                          [--as cc:<uuid>|codex:<uuid>|oc:<ses_id>]
    peers.py buddy [show|ping|clear] [--as cc:<uuid>|codex:<uuid>|oc:<ses_id>] [--json]
    peers.py buddy set [cc:|codex:|oc:|@]<name|uuid> [--uses a,b] [--replies N]
                       [--as cc:<uuid>|codex:<uuid>|oc:<ses_id>] [--json]
    peers.py session-hook
    peers.py install-hook [--auto-attach]
    peers.py topic post <topic> (--message <text>|--message-file <path>|
                       --json-file <path>) [--kind KIND] [--as cc:<uuid>|codex:<uuid>|oc:<ses_id>]
                       [--json]
    peers.py topic tail <topic> [--since SEQ] [--limit N] [--json]
    peers.py topic list [--json]
    peers.py gc [--days 7] [--dry-run]
    peers.py doctor

Python 3.9+, stdlib only (D3).  macOS and Linux only.
"""

import os
import sys

_HERE = os.path.dirname(os.path.realpath(__file__))
if sys.path[:1] != [_HERE]:
    sys.path.insert(0, _HERE)

from session_peers.cli import main

if __name__ == "__main__":
    sys.exit(main())
