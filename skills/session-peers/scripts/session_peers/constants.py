"""Constants for the session-peers CLI."""

from __future__ import annotations
import collections
import re

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# D11: pins, not requirements. A newer install warns, never fails.
CLAUDE_CODE_TESTED = "2.1.263"
CODEX_TESTED = "0.153.4"

PEER_PROTOCOL = 1
PEER_FEATURES = ["notify_idle"]

# Codex MAX_USER_INPUT_TEXT_CHARS, and roughly Claude's inbound line cap.
MAX_TEXT_CHARS = 1048576

# D4: replies per (thread, Claude session) before the shim goes quiet.
REPLY_BUDGET = 3
# `budget allow` may raise one requester's cap to at most this many replies.
BUDGET_ALLOW_MAX = 20
# A buddy binding's reply total: granted by default when a Claude session binds a
# Codex buddy, and `buddy set --replies N` may raise it up to the maximum.
BUDDY_REPLIES_DEFAULT = 100
BUDDY_REPLIES_MAX = 500
# What a running shim's code supports, saved in its own state file (never the
# vendor-read registry record). A shim keeps the code it started with, so a
# command whose marker only newer shims consume checks this first.
SHIM_FEATURES = ["budget_allow", "binding_allowance", "binding_allowance_max500"]
REPLY_BUDGET_WINDOW_DEFAULT = 30 * 60.0
REQUEST_TIMEOUT_DEFAULT = 10 * 60.0
REQUEST_TIMEOUT_MAX = 60 * 60.0
# Below this a Claude peer mid tool-call can miss the request entirely.
REQUEST_TIMEOUT_ADVISED_MIN = 120.0
REQUEST_POLL_INTERVAL = 0.1
WAIT_POLL_INTERVAL_DEFAULT = 1.0
REQUEST_ORPHAN_TTL = 60.0
VERSION_WARNING_WINDOW = 24 * 60 * 60.0

# Columns the `threads` table must have for the schema to count as recognised.
THREADS_COLUMNS = frozenset({"id", "rollout_path", "cwd", "name", "updated_at"})

TAG_PREFIX = "[session-peers"
TAG_RE = re.compile(
    r"^\[session-peers from=@(?P<from>\S*) sid=(?P<sid>\S*)"
    r"(?: mid=(?P<mid>\S*))? reply=(?P<reply>.*)\]$"
)
WRAPPER_RE = re.compile(
    r"^\s*<cross-session-message\s+(?P<attrs>[^>]*)>\n?(?P<body>.*?)\n?</cross-session-message>\s*$",
    re.DOTALL,
)
ATTR_RE = re.compile(r'([A-Za-z][A-Za-z0-9-]*)="([^"]*)"')
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")
AT_NAME_RE = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9_.\-]*)")

# Sentinel for a tag field the sender could not fill in. Parses back to None.
TAG_ABSENT = "-"
MAX_TAG_FIELD_CHARS = 256

# Built rather than written literally so this file has no stray triple
# quotes; _scan_multiline compares against them.
TRIPLE_DQ = '"' * 3
TRIPLE_SQ = "'" * 3

# A TOML bare key needs no quoting in a table header.
BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# `[features] # flags` is a valid TOML header; `# [features]` is a comment.
TOML_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")

# A peer name reaches Claude inside a wrapper attribute and inside the tag line,
# so it is restricted at the door rather than escaped at every use (B1).
PEER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Substitute for the "<" of a wrapper tag appearing inside a body. Printable and
# visible in a transcript, unlike a zero-width character.
LT_SUBSTITUTE = "\u2039"
WRAPPER_MARKUP_RE = re.compile(r"<(/?)(cross-session-message)", re.IGNORECASE)
REQUEST_MARKUP_RE = re.compile(r"<(/?)(session-peers-request)", re.IGNORECASE)
# Every character str.splitlines() treats as a boundary, plus C0/C1 controls.
ORIGIN_BREAK_RE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Tunables, read at shim start. The tests turn them down so a fixture rollout is
# picked up in milliseconds rather than seconds.
POLL_INTERVAL_DEFAULT = 1.0
LIVENESS_INTERVAL_DEFAULT = 5.0
ALIAS_REFRESH_INTERVAL_DEFAULT = 30.0
CONN_TIMEOUT = 30.0
MAX_RECORD_REWRITES = 2
PROCESSED_TURN_HISTORY = 200
CONTACT_HISTORY = 200
MAX_CONCURRENT_CLIENTS = 8
MAX_FRAMES_PER_CONNECTION = 16
READ_CHUNK = 1024 * 1024
# A rollout line longer than this is not a Codex turn (its own text cap is 1
# MiB): skip it rather than buffer it, so one damaged file cannot exhaust RAM.
MAX_ROLLOUT_LINE = 8 * 1024 * 1024
# S7: a completion older than this at shim start is recorded, never posted.
RESTART_DELIVERY_WINDOW = 900.0
GC_DAYS_DEFAULT = 7.0
# Per-thread files in state_dir(), named <thread uuid><suffix>. GC owns them.
THREAD_ARTIFACT_SUFFIXES = (".json", ".log", ".pid", ".budget-reset", ".budget-allow")

# What a buddy is for. Advisory scope only: nothing grants a permission from it.
BUDDY_USES = ("review", "brainstorm", "second-opinion", "co-steer", "ping", "sanity-check")
BUDDY_KINDS = ("cc", "codex")


Turn = collections.namedtuple(
    "Turn", "turn_id user_text tag outcome last_agent_message completed_at",
    defaults=(None,),
)
Event = collections.namedtuple("Event", "kind turn_id turn")


BINDING_LOCK_TIMEOUT = 10.0


# --------------------------------------------------------------------------
# Topics: pull-only, append-only logs any peer can post to and read
# --------------------------------------------------------------------------

TOPIC_MAX_CHARS = 256
TOPIC_TTL_DAYS_DEFAULT = 7.0
TOPIC_MAX_ENTRIES_DEFAULT = 1000
TOPIC_MAX_BYTES_DEFAULT = 16 * 1024 * 1024
TOPIC_TAIL_DEFAULT = 20
TOPIC_TAIL_MAX = 1000
TOPIC_KIND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
# C0 and C1 controls: a topic is one printable line.
TOPIC_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


_NO_DATA = object()

# The SessionStart hook's detached attach loop: poll every FAST_STEP seconds for
# the first FAST_WINDOW seconds, then every SLOW_STEP, until WAIT seconds pass.
HOOK_ATTACH_WAIT = 60.0
HOOK_ATTACH_FAST_WINDOW = 10.0
HOOK_ATTACH_FAST_STEP = 0.25
HOOK_ATTACH_SLOW_STEP = 1.0
