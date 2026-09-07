# PRD #44: Cross-session messaging between Claude Code and Codex CLI

**Issue**: [#44](https://github.com/vtmocanu/skills/issues/44) | **Label**: PRD | **Priority**: Medium
**Area**: new skill `skills/session-peers/` (`SKILL.md`, `scripts/peers.py`, `scripts/test_peers.py`, `references/spike-checklist.md`), one `test.yml` step, README rows, CHANGELOG. macOS and Linux only (Windows uses named pipes, which the stdlib cannot speak).
**Status**: Implemented 2026-09-07, M0 to M6 done (M0 hook-timing item deferred); PR pending. Created 2026-09-07. Reviewed the same day by two Claude reviewers (27 findings) and by a Codex CLI session (13 findings in round 1, 7 in round 2), all folded in or answered below.

**Evidence basis**: every mechanism below was measured on this machine on
2026-09-07 with Claude Code 2.1.263 (`~/.local/share/claude/versions/2.1.263`)
and Codex CLI 0.153.4 (Homebrew cask). A fact that comes from reading a binary
or upstream source rather than a documented contract is marked
**(undocumented)**; those are the drift risks, see R1 and R2. Nothing here is
inferred from a blog post: the widely quoted `codex queue --session <name>` and
`codex --session-name` flags do not exist in 0.153.4, the real flags are
`--thread` and `--message`.

**Terms**: a *Claude session* is one running `claude` process with its inbox
socket; a *Codex thread* is one conversation held open by a `codex` process
(one TUI process can hold several, `/new` starts another in the same process);
a *peer* is either, as the other side lists it; a *shim* is the process that
stands in for one Codex thread inside Claude's peer fabric. Outcomes are
recorded inline under each milestone box (PRD #13 style) and in the Status line
at close; there is no separate progress section.

## Problem

Claude Code sessions on one machine can list and message each other: a session
has a name, `ListAgents` lists the peers, `SendMessage` delivers a message into
another session's conversation, and the user can type `@name` in the prompt
(documented at https://code.claude.com/docs/en/cross-session-messaging). A Codex
thread running next to them is invisible to that fabric and cannot message
back. Coordinating the two today is copy-paste between terminals.

Nothing existing closes the gap on the native primitives:

- `openai/codex-plugin-cc` (official) is one-way. Claude drives `codex
  app-server` threads it owns through a broker socket and never reaches an
  interactive `codex` TUI. Last commit 2026-07-08, 238 open PRs (oldest from
  launch day), 490 open issues.
- `sendbird/cc-plugin-codex` is the reverse, also one-way: Codex spawns fresh
  `claude -p` subprocesses, it never reaches a running Claude session.
- `Co-Messi/agent-peers-mcp`, `vbcherepanov/a2abridge`,
  `abhishekgahlot2/codex-claude-bridge`, `umum-ai/claude-codex-bridge` (created
  2026-09-05, no LICENSE) either poll an inbox on a timer or depend on Claude's
  Channels feature, which is a research preview that loads only allowlisted
  plugins unless `--dangerously-load-development-channels` is passed.

Both vendors shipped the primitives that make a direct bridge small:

| direction | primitive | verified |
|---|---|---|
| Claude to Codex | `codex queue --thread <uuid or exact name> --message "<text>"` (Codex 0.149.0+). In the default standalone layout (no shared daemon) it inserts into the queue store (`$CODEX_HOME/queue_1.sqlite`, `queued_items`) and the process holding the thread polls SQLite's `data_version` and starts the item as its next user turn, queuing if mid-turn; with a shared app-server daemon the same CLI goes through `thread/queue/add`. The turn runs under the thread's own approval mode, exactly like a typed prompt. | Live, no daemon present: message queued by UUID and by `/rename`d name, reply in 2 to 4 s. A thread that has had no turn yet is not found by name (`No active session found`) but is by UUID |
| Codex to Claude | One newline-delimited JSON line to the session's inbox socket (path read from its registry record): `{"type":"user","message":{"role":"user","content":"..."}}`. Documented as a supported injection point (the session logs a `socat` recipe at startup). | Live from a host shell: arrived instantly. From inside Codex's `exec` on macOS: **exit 1**, the measured default sandbox (`workspace-write`, `network_access=false`) denies the Unix-socket connect |

## Solution

One skill, `session-peers`, in this repo, installed by `npx skills` to both
agents (D9). It contains:

1. **`scripts/peers.py`** (Python 3 stdlib only, `#!/usr/bin/env python3`,
   0755): the whole bridge behind one CLI. `list`, `send` (either direction),
   `shim` (run as one Codex thread's peer, D2), `up` / `down`, `session-hook`
   (optional Codex `SessionStart` entry that runs `up`), `install-hook`,
   `doctor`. Roots come from `$CLAUDE_CONFIG_DIR` (default `~/.claude`) and
   `$CODEX_HOME` (default `~/.codex`, with `sqlite_home` /
   `CODEX_SQLITE_HOME` honoured for the databases); tests point everything at
   temp directories and never touch the real ones.
2. **Peer shims**: for every registered live Codex thread (D6), one small process
   that owns a Claude-style registry record
   `$CLAUDE_CONFIG_DIR/sessions/<shim-pid>.json` and listens on a socket in
   Claude's allowlisted socket directory. To every Claude session the thread is
   then a normal peer: it appears in `ListAgents` and the `@` typeahead under its
   Codex name, `SendMessage` delivers a frame to the shim, the shim tags it with
   the sender and queues it by UUID. The same shim tails the thread's rollout
   and delivers Codex's replies (D4): when a turn completes, its
   `task_complete.last_agent_message` goes to the Claude session that queued
   the turn, and to a session named on the first line with `@<name>`. The shim
   keeps a small persisted state file (cursor, pending turns, delivered turn
   ids, budgets) and exits when the process that held the thread no longer
   holds its rollout open.
3. **No mandatory Codex hook.** `up <name|uuid>` registers a thread once and
   starts its shim; a bare `up` reconciles every registered live thread from
   any shell; an optional `SessionStart` hook (installed by `install-hook`,
   trusted once in `/hooks`) runs that reconcile on every invocation so
   registered threads come back after a restart without a manual step. A newly
   `/rename`d thread still needs one `up <name>`. Nothing in the delivery path
   depends on a hook (D5).
4. **`SKILL.md`** with one body that branches by agent: what a Claude session
   does (`up`, then message `@<codex-name>` natively), what a Codex thread does
   (list Claude peers, address a reply with a first-line `@name`).

### Facts the implementation relies on (undocumented unless linked)

- **Registry**: `$CLAUDE_CONFIG_DIR/sessions/` (0700), one `<pid>.json` (0644)
  per live session, plus `<pid>.<sha256(socket path)>.key` (0600) holding
  `peerToken`. Live record shape: `{"pid","sessionId","cwd","startedAt",
  "procStart","version","peerProtocol":1,"peerFeatures":[...],
  "kind":"interactive","entrypoint":"cli","pidDomain":"darwin",
  "messagingSocketPath","name","nameSource","nameSince","status":"busy|idle",
  "updatedAt","statusUpdatedAt"}`. Records are parsed leniently; the live filter
  is pid alive, `pidDomain` matches, and `procStart` equals
  `ps -o lstart= -p <pid>` under `LC_ALL=C TZ=UTC`, trimmed. Stale records are
  deleted by whichever session notices. M0 confirms the directory follows
  `CLAUDE_CONFIG_DIR`.
- **Socket directory allowlist**: the 2.1.263 sender accepts a peer socket only
  under `/tmp/cc-socks` or `/tmp/cc-socks-<uid>` (also the `/private/tmp`
  spellings) on macOS, `/run/user/<uid>/cc-socks` on Linux, plus a Termux path.
  `$TMPDIR` is never consulted (here it is `/var/folders/.../T/` while every live
  record sits in `/tmp/cc-socks`). Senders read `messagingSocketPath` from the
  record, never compute it; the shim binds in the directory of a live record's
  `messagingSocketPath`, or the platform default when no record exists.
- **Sender-side checks** when Claude connects to a peer socket
  (https://code.claude.com/docs/en/errors): "reply target is a symlink",
  "connected endpoint is not the expected process", "connected endpoint is not
  owned by this user", "connected endpoint is a different process with the
  expected pid". So the process that `accept`s on the socket **must be the pid
  in the record**: one shim process per record, no forked handlers.
- **Frame** Claude itself sends: `{"msgV":1,"msg_id":"<uuid4>","type":"user",
  "message":{"role":"user","content":"<body>"},"priority":"next",
  "from":"uds:<sender socket path>"}`. The body is normally wrapped in
  `<cross-session-message from="uds:..." from-session="<uuid>" hop-chain="..."
  from-name="<name>" from-mode="bypass|prompting">BODY</cross-session-message>`
  with a fixed attribute order the parser validates by re-serialising. A bare
  unwrapped string is also accepted and arrives attributed as a peer with no
  name. The socket writes nothing back; delivery receipts arrive as a separate
  connection to the `from` address carrying
  `{"type":"control","action":"peer_message_status",...}`. Other control frames
  a listener sees: `rename`, `notify_when_idle`, `peer_idle_notice`. Measured
  `notify_when_idle` frame: `{"type":"control","action":"notify_when_idle",
  "from":"uds:<path>","from_mode":"prompting","msgV":1,"msg_id":"<uuid>"}`; the
  accepted answer: `{"type":"control","action":"peer_idle_notice",
  "orig_msg_id":<that msg_id>,"state":"idle","finished_at":<ms>,"detail":<str>}`.
- **Inbound gate**: `crossSessionInbound` unset means mode parity. A sender that
  asserts no permission class is delivered to a prompting-mode session and held
  for approval by a bypass-permissions session. Auth line optional on macOS and
  Linux. A connection with no complete line in 30 s is closed; a line over about
  a million characters drops the connection. The receiver rate-limits per
  sender, drops identical repeats, and queues at most 50 messages.
- **Codex names and threads**: `/rename <name>` in the TUI (or the app-server
  method `thread/name/set`) writes `threads.name` in the `threads` table of
  `$CODEX_HOME/state_*.sqlite` (numeric suffix = schema version; the reader
  picks the file whose schema it recognises, not the largest number) and an
  entry in `$CODEX_HOME/session_index.jsonl`. **Codex's title suggester also
  writes `name`** (measured: 11 of 93 threads have one, several of them
  generated, e.g. `Retrieve Codex login token`), and the suggester's schema
  allows one-word titles while the first prompt is persisted as a provisional
  name (`hello` appeared in the index before `Respond to greeting`), so
  **nothing in `name` distinguishes a user's `/rename` from a generated title**;
  opt-in is explicit registration (D6). A thread is held by a runtime when its
  `rollout_path` (`$CODEX_HOME/sessions/<y>/<m>/<d>/rollout-...jsonl`) is open
  in a `codex` process, checked with `lsof`; an open rollout proves a runtime
  holds it, not that a TUI is attached (a `codex exec` or daemon can hold one
  too). A `flock` probe on `$CODEX_HOME/thread-writer-locks/<uuid>.lock` was
  rejected: a probe acquires the lock for an instant and can make Codex's own
  acquire fail. Name resolution in `codex queue` cannot be relied on to reject
  duplicates (the 0.153.2 source picks a match), so the bridge resolves names
  itself, refuses duplicates, and always queues by UUID. Cap 100 items per
  thread (`MAX_QUEUE_ITEMS`), text cap 1 048 576 chars
  (`MAX_USER_INPUT_TEXT_CHARS`); only threads loaded in a live process drain; an
  interrupted turn pauses the queue. Source says a 10 s poll; measured under 1 s
  in three trials, so the latency bound holds only for an idle, unpaused thread.
- **Rollout events the shim reads** (measured on 0.153.4): `event_msg` of type
  `task_started` `{turn_id, started_at, ...}`; `turn_context` `{turn_id, cwd,
  model, approval_policy, sandbox_policy, ...}`; `response_item` `{role: user,
  content: [{type: input_text, text}]}` for the prompt (this item carries **no
  `turn_id`**; it sits between its `task_started` and `task_complete`);
  `response_item` `{role: assistant}` for messages; `event_msg`
  `task_complete` `{turn_id, last_agent_message, started_at, completed_at,
  duration_ms}` once per completed turn, or `turn_aborted` `{turn_id, reason,
  ...}` on an interrupt (2 seen today). `last_agent_message` may be null.
  Correlation is therefore by turn boundary, never by "last user item before
  EOF", and a turn ends on either event.
- **Codex hooks** (`features.hooks = true`, `$CODEX_HOME/hooks.json`;
  https://learn.chatgpt.com/docs/hooks): event-specific schemas (`SessionEnd`
  has no `model` or `permission_mode`; `Stop`'s `last_assistant_message` is
  nullable); `SessionStart` runs when the first turn starts, not at TUI launch
  (0.153.2 source, `core/src/session/turn.rs`), and fires again on `resume`,
  `clear` and `compact` (`source` field); `SessionEnd` is synchronous, 1 s
  default timeout, 3 s cap, and does not run on a crash; an active-turn Ctrl-C
  is `Interrupt`. The runner lets a detached child survive but waits for
  inherited stdout/stderr pipes, so a daemonised child MUST use
  `start_new_session=True`, redirect all three fds and close inherited ones.
  Trust state lives in `config.toml` `[hooks.state."<file>:<snake_case_event>:<i>:<j>"]`
  with `trusted_hash` (and `enabled`, which may be omitted); a new or edited
  entry is reviewed in the TUI (`/hooks`); `--dangerously-bypass-hook-trust` is
  per-invocation only. The user's `hooks.json` already carries third-party
  entries for most events, so the installer MUST append into existing arrays.
- **Where Codex finds the skill**: `$CODEX_HOME/skills`, the repo's
  `.agents/skills`, and the user-level `~/.agents/skills/<name>/` (the npx
  shared store, independent of `$CODEX_HOME`; the per-agent copies under
  `~/.claude/skills/` flip between symlink and copy on every `update`). The hook
  command is `python3 <realpath of peers.py> session-hook`, resolved at install
  time from the script's own location.

## Decision log

- **D1. Native primitives, not MCP, Channels, or app-server.** `codex queue` and
  the inbox socket are the only paths that reach an *already running
  interactive* session on both sides. Channels needs a launch flag and an
  allowlisted plugin during its preview; `codex mcp-server` and `codex
  app-server` own their own threads; `codex-plugin-cc`'s broker is the wrong
  layer for this and is unmaintained.
- **D2. One shim process per Codex thread, registering the shim's own pid,
  owning both directions.** The per-process record is forced by the sender-side
  endpoint check and the one-record-per-pid registry layout. The shim writes the
  record, unlinks a leftover socket, binds in the allowlisted directory, serves
  NDJSON in its own pid (a forked handler changes the peer pid), tails the
  thread's rollout for status and replies (D4), and exits when the process
  that held the thread no longer holds its rollout open, which it re-checks
  every few seconds with `lsof` against the recorded pid (a live daemon can
  unload one thread while staying alive, so pid existence alone would leave a
  stale peer accepting messages into an undrained queue; `SessionEnd` is
  best-effort and absent on a crash). If a Claude session's stale cleaner
  deletes its record, it rewrites at most twice and then stops, so a
  disagreement never becomes write/delete churn. `up` starts one shim per
  registered live thread (idempotent: it deduplicates shims, never
  reconciliation, so it is safe to run on every hook invocation), `down` stops
  them. Measured 2026-09-07: a record with `peerFeatures: []` makes Claude
  refuse `notify_when_idle` ("runs a version without idle notices"); with
  `["notify_idle"]` the subscription is accepted and the shim's
  `peer_idle_notice` rendered as a Claude idle notice. A shim killed with
  SIGTERM left its record and socket behind (Python's default handler skips
  `finally`), so the shim installs signal handlers. **State and restart**: each
  shim persists
  `$CODEX_HOME/session-peers/<uuid>.json` (rollout byte cursor, pending
  turn-to-sender map, delivered turn ids, budgets, contact list). On start it
  resumes from the cursor when the file exists and starts at EOF otherwise; it
  never transmits a completion that happened before its cursor, so a restart
  cannot resend history. A tagged turn that completed while no shim was running
  is delivered on restart only if it completed within the last 15 minutes;
  older ones are marked delivered without posting (measured in review: without
  the gate a stale answer was replayed). Delivery across a shim crash is
  therefore best-effort and bounded. Sleep is a non-issue (`procStart` is
  stable); reboot clears `/tmp` and the next `up` restarts the shims.
- **D3. Python 3 stdlib only.** `sqlite3` reads Codex's databases, `socket`
  speaks the Unix socket without a `nc`/`socat` dependency, `subprocess` runs
  `codex queue`, `ps` and `lsof`, `json` does the rest. Matches this repo's
  existing scripts (`scripts/validate_skills.py`,
  `agent-team/scripts/sync.py`, tested by `test.yml`) and the skill-maker rule
  "stdlib only where possible". The `hooks.json` merge therefore lives in
  `peers.py install-hook`, not in a shell script.
- **D4. Reply policy: auto reply-to-sender plus explicit first-line `@<name>`,
  delivered by the shim on `task_complete`.** The shim prefixes every queued
  message with one tag line
  `[session-peers from=@<claude-name> sid=<sessionId> reply=uds:<socket>]` and
  remembers, per turn it started, who asked. Tailing the rollout it pairs
  `task_started.turn_id` with the following `role: user` item and the matching
  `task_complete`; on completion it posts `last_agent_message` (tag stripped)
  to the recorded sender, after re-reading that sender's record, applying the
  liveness filter and checking `sessionId == sid` (pids are reused and Claude's
  own sender guards do not run here). Independently, when the first line of
  `last_agent_message` begins with `@<claude-name>`, it posts to that session by
  name (prior-contact rule, D8). Duplicates to one target collapse;
  Measured 2026-09-07: a typed turn and a queued turn, and two queued items
  back to back, each produced their own `task_started` / user item /
  `task_complete` with matching `turn_id`, the next turn starting 5 to 30 ms
  after the previous `task_complete`; a completed turn can carry a null
  `last_agent_message` even with an assistant item present. After a Ctrl-C the
  queue stays paused through a `codex resume` until the user types a new
  prompt, and a dead process leaves its items queued, so the shim reports
  "paused after interrupt" or "not live" to the sender through a
  `peer_message_status` control frame instead of pretending delivery.
  `task_complete` fires once per turn, so a `Stop` hook that continues the turn
  changes nothing here; a `turn_aborted` or a null `last_agent_message` ends
  the turn with no deliverable reply and clears the pending entry. A `Stop`
  hook was the first design and was dropped: it can fire more than once per
  turn, loses a revised answer when skipped, and `transcript_path` is nullable.
  **Reply budget** instead of a hop counter (Claude's native reply carries no
  bridge tag, so a counter restarts each round trip): per (thread, Claude
  session) the shim delivers at most 3 replies to bridge-originated turns in a
  row, **first-line `@name` included**, since the model can write `@name` on
  its own; the counter never resets by inference (an untagged user item can
  come from another `codex queue` caller or a hook continuation), only by the
  user: `peers.py budget reset <thread>` or a fresh `up <thread>`. Past the
  budget the shim drops the reply with a stderr line and the human sees it in
  the Codex TUI as usual.
- **D5. Codex to Claude never goes through sandboxed `exec`, and needs no
  hook.** Measured on macOS: the default sandbox blocks the socket connect
  (platform- and profile-dependent, so stated as a measurement, not a law). The
  shim is a host process, so delivery is unsandboxed by construction. `peers.py
  send --to cc:<name>` still exists for host shells; the SKILL.md tells Codex to
  use a first-line `@name` and let the shim deliver. `UserPromptSubmit` and
  `Stop` are not used.
- **D6. Names are Codex's own thread names; opt-in is explicit registration.**
  `/rename <name>` in the TUI is the source; the shim registers the name
  verbatim. Because Codex's title suggester also writes `name` (one-word titles
  allowed, and the first prompt is persisted as a provisional name), the bridge
  never infers consent from a name: a thread becomes a peer only after
  `peers.py up <name|uuid>`, which resolves the name once, refuses a name held
  by more than one live thread, and persists the UUID in
  `$CODEX_HOME/session-peers/registered.json`; `down <name|uuid>` unregisters.
  Registration survives restarts, so the reconcile in a bare `up` (from a shell
  or the optional hook) brings a registered thread back without a manual step,
  while a newly `/rename`d thread needs one `up <name>`. Whitespace-free names
  stay an addressing convention (Claude's typeahead needs quotes otherwise),
  not a consent signal. The model cannot rename a thread (no tool exposes
  `thread/name/set`), so a repo's `AGENTS.md` cannot opt a thread in. The
  SKILL.md tells users to `/rename` and to add `thread-title` to
  `[tui] status_line` so the name is visible.
- **D7. Frame content: the wrapper without a permission claim (measured).**
  M0 posted, from a shim socket as `from`, a wrapper carrying `from`,
  `from-session` and `from-name` but **no `from-mode`**, and a bare string
  prefixed `Message from Codex thread <name>:`. The wrapper rendered as a named
  peer preview (`› Message from @codex-uzi: … (ctrl+o to expand)`) and the model
  saw it attributed; the bare form rendered like a typed prompt with no name.
  The bridge sends the wrapper. Claude's own frames to the shim carried
  `from`, `from-name` and `from-mode` only, so `from-session` and `hop-chain`
  are optional on the receiving side. Never assert `from-mode`: Codex has no Claude permission class, and an
  unclassified sender is exactly what a bypass-mode session holds for approval.
  Either way `from` is the shim's socket, so Claude's reply reaches Codex.
- **D8. Security: same-uid filesystem model, narrowed where the bridge would
  widen it.** Modes as measured: socket 0600, record 0644, both directories
  0700. The shim accepts a frame only from a client whose peer uid is ours, caps
  the body at `MAX_USER_INPUT_TEXT_CHARS`, and applies D4's budget. A Codex
  thread can only be steered (by a repo's `AGENTS.md`, say) into a Claude session
  that already messaged it: explicit `@name` delivery is allowed only to a
  session in the shim's contact list, while reply-to-sender always works;
  `SESSION_PEERS_ALLOW_UNSOLICITED=1` lifts the rule for a user who wants it.
  The shim refuses a `reply=` path outside the allowlisted directories or
  through a symlink. A Claude-to-Codex message is a Codex user turn executed
  under Codex's own approval mode, so Claude gains nothing Codex's user did not
  already allow. Nothing new is exposed on the network.
- **D9. One skill, not two.** `claude` is a reserved substring in `name:` (so
  `claude-peers` cannot exist), and `npx skills update` reinstalls every changed
  skill to every detected agent regardless of `-a`, so agent-specific skills
  would land in both agents anyway. One `session-peers` folder, one script, one
  description that names both agents, a body that branches on which agent is
  reading. It sits outside `agent-kit`, so no `plugin.json` entry and
  `check_bundle_coverage.py` is unaffected; `validate_skills.py` already sweeps
  `skills/**`.
- **D10. No Claude-side hook.** Claude replies to Codex with its native
  `SendMessage` to the shim.
- **D11. Drift is detected, not prevented.** `peers.py` pins the Claude Code
  and Codex versions it was validated against and prints a one-line warning
  (never a failure) from `list`, `send`, `up` and `doctor` when the installed
  version is newer (records on this machine already span 2.1.247 to 2.1.263, so
  mid-run upgrades are normal). When the registry format changes,
  `send --to codex:<name>` keeps working through `codex queue` without the shim;
  only the `@`-typeahead and reply delivery degrade, and `doctor` says so. When
  the Codex side is what drifted (unknown `state_*` schema), name resolution
  and liveness are unavailable: `send --to codex:<uuid>` still queues, prints
  "liveness unverified", and a name target reports sending unavailable rather
  than guessing.

## Milestones

**M0. Spike the undocumented seams (throwaway code; findings written into D2, D4, D7 and `references/spike-checklist.md`).**
- [x] **2026-09-07 13:35** A hand-written shim writing the M2 record appeared in
  `ListAgents` as `codex-uzi [a6a2b4] · interactive · idle` 4 s after start;
  `SendMessage` passed the endpoint checks; the frame is quoted in the
  checklist. No `.key` file was needed. `CLAUDE_CONFIG_DIR` not provable from
  the binary; recorded as an assumption (checklist item 6).
- [x] **2026-09-07 13:42** Both forms delivered; wrapper (no `from-mode`)
  renders as a named peer, bare does not. D7 filled. A Claude reply to the
  wrapped message reached the shim socket (the SHIM-OK and IDLE-OK round trips).
- [x] **2026-09-07 14:01** `notify_when_idle` control frame received (shape in
  the facts); `peer_idle_notice` accepted and rendered as
  `[Cross-session idle notice] "codex-uzi" … is idle now — it finished a turn
  at 14:01. Its harness reports: «turn task_complete»`. Needs
  `peerFeatures: ["notify_idle"]` in the record.
- [x] **2026-09-07 14:02** Two items queued 0.1 s apart ran as two sequential
  turns (`task_started` 5.9 s after queueing, the second 30 ms after the first
  `task_complete`), each `task_complete` carrying its reply (`BB-A`, `BB-B`).
  Q3 resolved. Also measured: a queue paused by Ctrl-C stays paused across
  `codex resume` until a typed prompt; a dead process leaves items queued.
- [~] Hook timing on 0.153.4: **deferred**, the design no longer depends on it
  (hooks are optional, `up` is the bootstrap); left as checklist item 13 for the
  first user who installs the hook.
- [x] **2026-09-07** `references/spike-checklist.md` written with the measured
  values; spike shim lived in the session scratchpad only.
Success met: D2, D4 and D7 carry measured answers; the checklist exists; no
spike code is committed.

**M1. `peers.py` core: roots, discovery, rollout reader, one-shot send.**
- [x] Roots from `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `sqlite_home`; every read
  tolerates a missing file, DB or unknown schema (empty result plus a stderr
  warning, never a traceback).
- [x] `list --json`: Claude sessions with the exact liveness filter (pid alive,
  `pidDomain`, `procStart` via `ps -o lstart=` under `LC_ALL=C TZ=UTC`); Codex
  threads from the recognised `state_*.sqlite` schema plus
  `session_index.jsonl`, filtered to rollouts held open per `lsof`, with the
  D6 registration flag and the holding pid.
- [x] Rollout reader: incremental tail from a byte cursor that yields turn
  records `(turn_id, user_text, tag, outcome, last_agent_message)` from the
  boundary events (`task_started`, `task_complete`, `turn_aborted`), with and
  without a tag line, null completion text, across a file that grows while
  read.
- [x] `send --to codex:<name|uuid>`: bridge-side name resolution (refuses
  duplicates), liveness check first, tag line, `codex queue --thread <uuid>`;
  the D11 degraded mode when the schema is unknown.
- [x] `send --to cc:<name>`: registry lookup, optional auth line from the `.key`
  file, frame per D7, `from` set when a shim socket exists for the calling Codex
  thread, otherwise omitted; refuses a socket outside the allowlisted dirs.
- [x] D11 version pin and warning.
Success met 2026-09-07: `peers.py` (2935 lines) with `list --json`, the rollout reader and both `send` forms, 215 tests green in `test_peers.py` against fake `codex`, `ps`, `lsof` and temp roots (run locally, 74 s).

**M2. Shim, `up` / `down`.**
- [x] `shim --thread <uuid>`: D2 in full (record, socket, own-pid serving,
  foreign-uid refusal, rewrite-at-most-twice, persisted state, resume-from-
  cursor, exit when the rollout is no longer held) plus D4 in full (tag,
  pending-turn map, rollout tail, reply-to-sender with `sid` verification,
  first-line `@name` with the contact list, reply budget, abort and null-text
  handling), `notify_when_idle` answered with `peer_idle_notice` on
  `task_complete` or `turn_aborted`, unknown `control` actions ignored,
  `status` mirrored busy on `task_started` and idle on either end event.
- [x] `up [<name|uuid>]` / `down [<name|uuid>]` / `budget reset <thread>`:
  registration per D6, reconcile of registered live threads, idempotent (one
  shim per thread, reconciliation on every call), daemonising with
  `start_new_session=True` and redirected fds so it is safe from a hook.
Success met 2026-09-07: shim end-to-end tests on a temp socket and a growing fixture rollout (delivery once, no resend across restart, `notify_when_idle` answered, foreign uid refused, budget incl. `@name`, exit when the rollout is no longer held); live run in M6.

**M3. `install-hook`, `session-hook`, `doctor`.**
- [x] `session-hook`: reads the `SessionStart` payload, runs the bare `up`
  reconcile detached, prints `{}`; runs on every invocation (`startup`,
  `resume`, `clear`, `compact`) and relies on `up`'s one-shim-per-thread
  dedupe.
- [x] `install-hook`: backs up `$CODEX_HOME/hooks.json`, appends one
  `SessionStart` entry into the existing array (creating the file or the array
  if absent) keyed by the command string for idempotency, sets
  `features.hooks = true` in `config.toml` if unset, and prints the `/hooks`
  trust step. Optional: the skill works without it.
- [x] `doctor`: each bridge entry's presence and its `[hooks.state]` block
  (reported as "trust unknown, open /hooks" since Codex's hash algorithm is not
  documented), installed versions against the pins,
  the socket directory in use, live shims versus opted-in threads, and whether
  `codex` and `lsof` are on `PATH`.
Success met 2026-09-07: `session-hook`, `install-hook` (in-place `hooks =` rewrite with backup, unparseable file refused with backup) and `doctor` covered by `TestInstallHook*`, `TestFeaturesHooksKey` and doctor fixtures; live `doctor` output recorded in M6.

**M4. Tests in `test.yml`, no live agents needed.**
- [x] `scripts/test_peers.py` (unittest, stdlib): registry parsing and
  liveness with a fake `ps`; Codex discovery against an in-test `state`
  schema, a fixture `session_index.jsonl` and a fake `lsof`; D6 registration (name resolved
  once, duplicate refused, unregistered thread ignored even when named);
  rollout reader on a fixture that grows mid-test, with `turn_aborted` and null
  text; cursor persistence and no-replay on restart; tag build and parse round
  trip; target extraction, contact list, budget (including `@name`) and dedupe;
  the degraded mode on an unknown schema; frame construction (auth
  line present/absent, wrapped/bare per D7); socket directory allowlist with
  both dir shapes as fixtures; shim end to end on a temp socket in a thread
  (socket paths kept under the 104-byte macOS cap); `install-hook` merge, backup
  and idempotency; `doctor` on fixtures; version warning with injected version
  strings.
- [x] One `test.yml` step: `python3 skills/session-peers/scripts/test_peers.py`;
  `scripts/validate_skills.py` green.
Success met 2026-09-07: `test.yml` step added; 215 tests (51 added in the review round: wrapper injection, pid ownership via flock, socket-dir ownership, chunked rollout reads, connection caps, restart window, real `peer_uid`, timestamps); green locally on macOS, CI on `ubuntu-latest` pending the PR.

**M5. Documentation.**
- [x] `SKILL.md`: frontmatter per skill-maker (third person, single line, no
  unquoted colon-space, triggers such as "message the codex session", "tell
  codex", "list codex sessions", "reply to the claude session", "@codex"); body
  under 500 lines, branched by agent, with install (`npx skills add`, `up`,
  the optional `install-hook` and its `/hooks` trust step, `/rename`,
  `thread-title` in `[tui] status_line`, the optional Claude `SessionStart`
  snippet that runs `up`), the registration step after `/rename`, the reply
  policy and budget, and the limits (sizes, duplicate names, sandbox, macOS and
  Linux only, replies need a running shim, best-effort across a shim restart).
  `agnix --target claude-code skills/session-peers/SKILL.md` clean before
  commit (local step; agnix is not in CI).
- [x] README: one row in "All skills" and the count 19 to 20; CHANGELOG
  `Unreleased` under `### Added` as "`session-peers`: ...".
Success met 2026-09-07: `SKILL.md` (138 lines, agnix 0 errors 0 warnings, validate_skills 20/20), README row and count 20, CHANGELOG `Added` entry; M6's first round trip followed the SKILL.md steps (`/rename`, `up <name>`, `SendMessage`).

**M6. End-to-end validation on this machine, recorded inline here.**
- [x] **2026-09-07 11:34:46Z** `up codex-uzi` registered and started shim pid
  29539 in 0.4 s; it appeared in `ListAgents` as `codex-uzi [7b2bfa]`;
  `SendMessage` at ~11:35:00 started the Codex turn at 11:35:06, complete at
  11:35:10 (RT1). Whole round trip about 10 s, of which Codex's poll was ~6 s.
- [x] **11:35:30 to 11:35:35** RT2 and RT3 (sent back to back) each landed in
  the sender exactly once with no `@`; this machine has two third-party `Stop`
  hooks installed (Clawd on Desk, Orca), so re-fired stops did not duplicate.
- [x] **11:35:10** RT1's reply began with `@uzi-30`; the explicit-address leg
  and the reply-to-sender leg collapsed into one delivery (dedupe verified).
- [x] **11:35** `notify_when_idle` on RT1 produced one
  `[Cross-session idle notice] "codex-uzi" ... finished a turn at 14:35 ... «the
  Codex thread is idle»` in the sender.
- [x] Negatives, all 2026-09-07: `send --to codex:<uuid of an exited thread>`
  refused with `no active session ... its process has exited (the queue would
  sit undrained)`, queue untouched (11:36); RT4 dropped with `reply budget of 3
  spent for session uzi-30` (11:36:21) and RT5 delivered after `budget reset`
  (11:37:10); RT4's `@nobody-xx` first line dropped with `no live Claude session
  named 'nobody-xx'` (the unknown-name path; the prior-contact path is covered
  by unit tests, not exercised live to avoid messaging another real session);
  the named, live, unregistered thread `review` listed `registered: false` and
  never became a peer; Esc during `sleep 600` produced `turn_aborted` (11:47:23)
  and the shim logged `aborted; nothing to deliver`; a message into the paused
  thread was queued and logged `paused after an interrupt` (11:47:40), the
  `held` status frame was posted but **no notice rendered in the sender's
  terminal on 2.1.263** (recorded as unverified), and the item drained right
  after the user's next prompt (`ok` at 11:49:02, RT6 delivered 11:49:13);
  `down` then `up` restarted the shim (pid 44506) with no resend of RT1 to RT3.
Success met: every box carries timestamps and measured figures.

## Success criteria

1. A Claude session lists every live, registered (D6) Codex thread on the
   machine as a peer with no Claude-side configuration.
2. Claude to Codex delivery by `@name` completes in under 15 s worst case for an
   idle, unpaused thread (the 10 s queue poll plus the turn start), typically
   under 2 s; a busy or interrupted thread has no bound and `send` says so.
3. Codex to Claude delivery needs no sandbox change, no Codex hook and no
   per-message approval in Codex.
4. A turn that a Claude session queued is answered back to that session with
   no addressing by the Codex model, exactly once while the shim runs
   (best-effort across a shim restart).
5. All logic is covered by stdlib unit tests in `test.yml`; the only untested
   paths are the real `codex` binary and a real Claude session, both exercised
   in M6.
6. A newer Claude Code or Codex version produces a warning, not a broken
   `send`; the skill-only send path has no dependency on the registry format.

Criteria 2, 3 and 4 need a live agent and are measured in M6 only.

## Risks

- **R1. Claude's registry, socket allowlist and frame format are undocumented
  and versioned (`peerProtocol` 1, growing `peerFeatures`).** A release can
  change the record shape, add a mandatory field, or tighten the endpoint
  checks. Mitigation: D11's warning, `references/spike-checklist.md` re-run after
  an upgrade, and the skill-only send path that survives any registry change.
- **R2. Codex internals used by discovery and delivery (`state_*.sqlite`
  schema, `session_index.jsonl`, rollout event names and layout) are
  undocumented.** `codex queue` itself is a supported CLI and is the only thing
  `send --to codex:` needs; discovery degrades to "name your thread and pass
  it by UUID with liveness unverified" and reply delivery degrades to none,
  with `doctor` naming the cause.
  Every DB read is wrapped; an unknown schema yields an empty list plus a
  warning; an unrecognised rollout event is skipped.
- **R3. Sandbox and platform.** The measured macOS sandbox blocks sockets from
  `exec`; the shim avoids `exec` entirely. Linux socket dir is
  `/run/user/<uid>/cc-socks`; both shapes are fixtures in M4.
- **R4. Hook trust UX (optional path only).** A `SessionStart` entry is inert
  until trusted in `/hooks`; `up` by hand covers it and `doctor` reports an
  untrusted entry.
- **R5. Queue latency and lost wakes.** The queue reader polls every 10 s and a
  thread not loaded in any live process accumulates silently; `send` checks
  liveness first and says so instead of queueing into a dead thread; a thread
  with no turn yet is addressed by UUID.
- **R6. Loops and cost.** Each delivered message is a paid turn on the
  receiving side. Claude's inbound rate limit, identical-repeat drop and 50-item
  queue cap cover the Codex-originated leg; D4's reply budget covers the
  other and resets only by explicit user action.
- **R7. Name collisions.** `up <name>` refuses a name held by two live
  threads (register by UUID instead); a Codex name equal to a Claude session's is
  disambiguated by Claude with its `[ref]` as for same-named Claude sessions.
- **R8. One process, several threads.** A TUI can hold several threads (`/new`);
  `lsof` attributes each rollout to the same pid, which is correct for liveness,
  each thread still gets its own shim, and a thread unloaded from a live
  process (its rollout closed) is unregistered by the next liveness check.

## Open questions (resolved during implementation)

- Q1. **Resolved 2026-09-07**: `from` and `from-name` suffice; `from-session`
  and `from-mode` are optional; a wrapper without `from-mode` renders as a named
  peer (D7).
- Q2. **Resolved 2026-09-07**: yes, a record without a `.key` file is accepted
  on macOS.
- Q3. Does a queued turn produce the same `task_started` / `task_complete`
  pair as a typed one? Measured today for four queued turns: yes, with
  `last_agent_message` carrying the reply. M0 re-checks the two-items
  back-to-back case.
- Q4. Where exactly does `SessionStart` fire on 0.153.4 (first turn vs launch),
  and does a detached `up` survive the hook runner? Deferred (checklist item
  13); nothing in the delivery path depends on it.
