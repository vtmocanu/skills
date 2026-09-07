# PRD #44: Cross-session messaging between Claude Code and Codex CLI

**Issue**: [#44](https://github.com/vtmocanu/skills/issues/44) | **Label**: PRD | **Priority**: Medium
**Area**: new skill `skills/session-peers/` (`SKILL.md`, `scripts/peers.py`, `scripts/test_peers.py`, `references/spike-checklist.md`), one `test.yml` step, README rows, CHANGELOG. macOS and Linux only (Windows uses named pipes, which the stdlib cannot speak).
**Status**: Draft, created 2026-09-07, reviewed by two reviewers the same day (27 findings folded in).

**Evidence basis**: every mechanism below was measured on this machine on
2026-09-07 with Claude Code 2.1.263 (`~/.local/share/claude/versions/2.1.263`)
and Codex CLI 0.153.4 (Homebrew cask). A fact that comes from reading a binary
or upstream source rather than a documented contract is marked
**(undocumented)**; those are the drift risks, see R1 and R2. Nothing here is
inferred from a blog post: the widely quoted `codex queue --session <name>` and
`codex --session-name` flags do not exist in 0.153.4, the real flags are
`--thread` and `--message`.

**Terms**: a *Claude session* is one running `claude` process with its inbox
socket; a *Codex thread* is one running `codex` TUI with its rollout; a *peer*
is either, as the other side lists it; a *shim* is the process that stands in
for one Codex thread inside Claude's peer fabric. Outcomes are recorded inline
under each milestone box (PRD #13 style) and in the Status line at close;
there is no separate progress section.

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
| Claude to Codex | `codex queue --thread <uuid or exact name> --message "<text>"` (Codex 0.149.0+). No daemon: it inserts into `$CODEX_HOME/queue_1.sqlite` (`queued_items`), a running TUI polls SQLite's `data_version` and starts the item as its next user turn, queuing if mid-turn. The turn runs under the thread's own approval mode, exactly like a typed prompt. | Live: message queued into the user's TUI by UUID and by `/rename`d name, reply in 2 to 4 s |
| Codex to Claude | One newline-delimited JSON line to the session's inbox socket (path read from its registry record): `{"type":"user","message":{"role":"user","content":"..."}}`. Documented as a supported injection point (the session logs a `socat` recipe at startup). | Live from a host shell: arrived instantly. From inside Codex's `exec`: **exit 1**, blocked by the default sandbox (`workspace-write`, `network_access=false` also denies Unix-socket connects) |

## Solution

One skill, `session-peers`, in this repo, installed by `npx skills` to both
agents (D9). It contains:

1. **`scripts/peers.py`** (Python 3 stdlib only, `#!/usr/bin/env python3`,
   0755): the whole bridge behind one CLI. `list`, `send` (either direction),
   `shim` (run as one Codex thread's peer, D2), `up` / `down` (manual shim
   control), `stop-hook` and `session-hook` (the Codex hook entry points, D2/D4),
   `install-hook`, `doctor`. Roots come from `$CLAUDE_CONFIG_DIR` (default
   `~/.claude`) and `$CODEX_HOME` (default `~/.codex`); tests point both at temp
   directories and never touch the real ones.
2. **Peer shims**: for every *named* live Codex thread, one small process that
   owns a Claude-style registry record `$CLAUDE_CONFIG_DIR/sessions/<shim-pid>.json`
   and listens on a socket in Claude's allowlisted socket directory. To every
   Claude session the thread is then a normal peer: it appears in `ListAgents`
   and the `@` typeahead under its Codex name, `SendMessage` delivers a frame to
   the shim, the shim tags it with the sender and runs `codex queue`. A Codex
   `SessionStart` hook starts the shim; `SessionEnd` or the death of the `codex`
   process ends it.
3. **A Codex `Stop` hook** (`$CODEX_HOME/hooks.json`, installed by `peers.py
   install-hook` together with the `SessionStart`/`SessionEnd` entries): runs on
   the host outside the sandbox, receives `last_assistant_message`, `session_id`,
   `turn_id` and `transcript_path`, and posts the reply into the addressed Claude
   session(s): auto reply-to-sender for a turn that a Claude session queued, plus
   explicit `@<name>` on the first line of the final message (D4).
4. **`SKILL.md`** with one body that branches by agent: what a Claude session
   does (message `@<codex-name>` natively; `up` as the manual fallback), what a
   Codex thread does (list Claude peers, address a reply with `@name`).

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
  a listener sees: `rename`, `notify_when_idle`, `peer_idle_notice`.
- **Inbound gate**: `crossSessionInbound` unset means mode parity. A sender that
  asserts no permission class is delivered to a prompting-mode session and held
  for approval by a bypass-permissions session. Auth line optional on macOS and
  Linux. A connection with no complete line in 30 s is closed; a line over about
  a million characters drops the connection. The receiver rate-limits per
  sender, drops identical repeats, and queues at most 50 messages.
- **Codex names** come from `/rename` in the TUI (or `thread/name/set`), are
  indexed in `$CODEX_HOME/session_index.jsonl` (`{"id","thread_name",
  "updated_at"}`) and in the `threads` table of the highest-numbered
  `$CODEX_HOME/state_*.sqlite` (`id, rollout_path, cwd, name, updated_at, ...`;
  the numeric suffix is a schema version). A live thread is one whose
  `rollout_path` is held open by a `codex` process, checked with `lsof` (a
  `flock` probe on `$CODEX_HOME/thread-writer-locks/<uuid>.lock` was rejected:
  a probe acquires the lock for an instant and can make Codex's own acquire
  fail). `codex queue` resolves UUID first, then exact name, rejects an
  ambiguous name, and errors with `No active session found matching '<name>'`.
  Cap 100 items per thread (`MAX_QUEUE_ITEMS`), text cap 1 048 576 chars
  (`MAX_USER_INPUT_TEXT_CHARS`); only threads loaded in a live process drain; an
  interrupted turn pauses the queue. Source says a 10 s poll; measured under 1 s
  in three trials.
- **Codex hooks** (`features.hooks = true`, `$CODEX_HOME/hooks.json`, events
  `SessionStart SessionEnd UserPromptSubmit PreToolUse PermissionRequest
  PostToolUse Stop SubagentStart SubagentStop PreCompact PostCompact
  Interrupt`): every hook gets `session_id, cwd, hook_event_name,
  transcript_path, model, permission_mode` on stdin; turn-scoped ones add
  `turn_id`; `Stop` adds `last_assistant_message` and `stop_hook_active` and
  MUST answer with JSON (plain text is invalid); `SessionStart` adds `source`
  (`startup|resume|clear|compact`). Every entry is trust-gated:
  `config.toml` `[hooks.state."<file>:<event>:<i>:<j>"]` carries `enabled` and
  `trusted_hash`; a newly added entry is reviewed in the TUI (`/hooks`), and
  `--dangerously-bypass-hook-trust` is per-invocation only. The user's
  `hooks.json` already carries two third-party `Stop` entries (Clawd on Desk,
  Orca), so the installer MUST append into existing arrays, never replace, and
  any `Stop` hook may continue the turn, re-firing `Stop` with
  `stop_hook_active=true`.
- **Where Codex finds the skill**: `$CODEX_HOME/skills` and the npx shared
  store `~/.agents/skills/<name>/` (real files; the per-agent copies under
  `~/.claude/skills/` flip between symlink and copy on every `update`). The hook
  command is `python3 <realpath of peers.py> <verb>`, resolved at install time
  from the script's own location, so it works for a global or a project install
  and does not depend on npx preserving the exec bit.

## Decision log

- **D1. Native primitives, not MCP, Channels, or app-server.** `codex queue` and
  the inbox socket are the only paths that reach an *already running
  interactive* session on both sides. Channels needs a launch flag and an
  allowlisted plugin during its preview; `codex mcp-server` and `codex
  app-server` own their own threads; `codex-plugin-cc`'s broker is the wrong
  layer for this and is unmaintained.
- **D2. One shim process per Codex thread, registering the shim's own pid,
  started by a Codex `SessionStart` hook.** The per-process record is forced by
  the sender-side endpoint check and the one-record-per-pid registry layout. The
  hook shape replaces a supervisor: `session-hook` (on `SessionStart`)
  daemonises `peers.py shim --thread <session_id>`, which waits until the thread
  has a name (D6), writes the record, binds the socket, and serves; `SessionEnd`
  or the death of the `codex` pid ends it. `up` reconciles shims for already
  running named threads by hand, `down` stops them. Constraints: the shim
  accepts and handles in its own pid (a forked handler changes the peer pid);
  it unlinks a leftover socket before binding (orphan sockets from earlier
  sessions sit in `/tmp/cc-socks`); if a Claude session's stale cleaner deletes
  its record, it rewrites at most twice and then stops, so a disagreement never
  becomes write/delete churn; it mirrors `status` from the rollout's
  `task_started` / `task_complete` events. Registering the real `codex` pid was
  rejected: the socket's peer pid would not match. Sleep is a non-issue
  (`procStart` is stable); reboot clears `/tmp` and the hook restarts the shim.
- **D3. Python 3 stdlib only.** `sqlite3` reads Codex's databases, `socket`
  speaks the Unix socket without a `nc`/`socat` dependency, `subprocess` runs
  `codex queue`, `ps` and `lsof`, `json` does the rest. Matches this repo's
  existing scripts (`scripts/validate_skills.py`,
  `agent-team/scripts/sync.py`, tested by `test.yml`) and the skill-maker rule
  "stdlib only where possible". The `hooks.json` merge therefore lives in
  `peers.py install-hook`, not in a shell script.
- **D4. Reply policy: auto reply-to-sender plus explicit first-line `@<name>`.**
  The shim prefixes every queued message with one tag line
  `[session-peers from=@<claude-name> sid=<sessionId> reply=uds:<socket> hop=N]`.
  The `Stop` hook: skips when `stop_hook_active` is true; locates the user item
  for its `turn_id` in `transcript_path` (the rollout JSONL; M0 records whether
  the rollout carries `turn_id` or the last `role: user` item before EOF is
  used); parses the tag; re-reads the record for `reply=`, applies the liveness
  filter and posts only if its `sessionId` equals `sid=` (pids are reused and
  Claude's own sender guards do not run inside the hook); strips the tag line
  from the body; records `(session_id, turn_id)` in a small state file so a
  re-fired `Stop` never re-posts. Independently, when the **first line** of
  `last_assistant_message` begins with `@<claude-name>`, the hook posts to that
  session by name (prior-contact rule, D8). Both may fire on one turn;
  duplicates to one target collapse. `hop=N` is incremented by the shim and
  dropped at `hop>2` with a stderr line; there is no content-based drop, so a
  quoted tag in prose is harmless.
- **D5. Codex to Claude goes through a hook, never through sandboxed `exec`.**
  Measured: the default sandbox blocks the socket connect. Hooks run on the host.
  Asking Codex to escalate out of its sandbox per message was tried and is the
  wrong shape (and the Claude auto-mode classifier refuses to ask a peer to do
  it). `UserPromptSubmit` is not used: the tag travels inside the queued
  message, which the model sees anyway. `peers.py send` still exists for host
  shells; the SKILL.md tells Codex to use a first-line `@name` and let the hook
  deliver.
- **D6. Names are Codex's own thread names, and only named threads become
  peers.** `/rename <name>` in the TUI is the source; the shim registers the
  name verbatim and appears under it. An unnamed thread gets no shim (this
  machine holds 15 writer locks; most are throwaway threads that would only add
  noise and attack surface). The SKILL.md tells users to `/rename` and to add
  `thread-title` to `[tui] status_line` so the name is visible. `codex queue`
  refuses an ambiguous name; the shim falls back to the UUID on that error.
- **D7. Frame content: wrapped without a permission claim when the spike
  proves it, bare otherwise.** M0 sends, from a shim socket as `from`, a wrapper
  carrying `from`, `from-session` and `from-name` but **no `from-mode`**, and a
  bare string prefixed `Message from Codex thread <name>:`. Use the wrapper if
  the parser accepts it and it renders as a named peer; otherwise the bare form.
  Never assert `from-mode`: Codex has no Claude permission class, and an
  unclassified sender is exactly what a bypass-mode session holds for approval.
  Either way `from` is the shim's socket, so Claude's reply reaches Codex.
- **D8. Security: same-uid filesystem model, narrowed where the bridge would
  widen it.** Modes as measured: socket 0600, record 0644, both directories
  0700. The shim accepts a frame only from a client whose peer uid is ours, caps
  the body at `MAX_USER_INPUT_TEXT_CHARS`, and applies D4's hop cap. A Codex
  thread can only be steered (by a repo's `AGENTS.md`, say) into a Claude session
  that already messaged it: explicit `@name` delivery is allowed only to a
  session recorded in the shim's contact list, while reply-to-sender always
  works; `SESSION_PEERS_ALLOW_UNSOLICITED=1` lifts the rule for a user who wants
  it. The hook refuses a `reply=` path outside the allowlisted directories or
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
  (never a failure) from `list`, `send` and `doctor` when the installed version
  is newer (records on this machine already span 2.1.247 to 2.1.263, so
  mid-run upgrades are normal). When the registry format changes,
  `send --to codex:<name>` keeps working through `codex queue` without the shim;
  only the `@`-typeahead degrades.

## Milestones

**M0. Spike the undocumented seams (throwaway code; findings written into D2, D4, D7 and `references/spike-checklist.md`).**
- [ ] A hand-written shim writing the exact record M2 ships (`entrypoint:
  codex`, `kind: interactive`, `peerFeatures: []`, `peerProtocol: 1`) appears in a
  real Claude session's `ListAgents` under a Codex name; `SendMessage` to it
  passes the endpoint checks; the received frame is recorded. Also record
  whether a record without a `.key` file is accepted, and that the registry
  follows `CLAUDE_CONFIG_DIR`.
- [ ] From the shim's socket as `from`, post D7's two forms into a Claude
  session; record which renders as a named peer and whether the reply lands on
  the shim. Fill D7.
- [ ] Confirm `notify_when_idle` arrives at the shim as a `control` frame and
  that `peer_idle_notice` is accepted; confirm whether a rollout item carries
  `turn_id` (fill D4); confirm `Stop` fires for a queued turn (Q3).
- [ ] Write the steps as `references/spike-checklist.md` so R1 can be re-run
  after an upgrade.
Success: D2, D4 and D7 carry measured answers; the checklist exists; no spike
code is committed.

**M1. `peers.py` core: roots, discovery, rollout reader, one-shot send.**
- [ ] Roots from `CLAUDE_CONFIG_DIR` / `CODEX_HOME`; every read tolerates a
  missing file, DB or unknown schema (empty result plus a stderr warning, never
  a traceback).
- [ ] `list --json`: Claude sessions with the exact liveness filter (pid alive,
  `pidDomain`, `procStart` via `ps -o lstart=` under `LC_ALL=C TZ=UTC`); Codex
  threads from the highest-numbered `state_*.sqlite` plus `session_index.jsonl`,
  filtered to rollouts held open per `lsof`.
- [ ] One rollout reader used by both hooks: events to idle/busy status; the
  user item for a `turn_id` (or last `role: user`, per M0), with and without a
  tag line.
- [ ] `send --to codex:<name|uuid>`: liveness check first, tag line, `codex
  queue`, UUID fallback on the ambiguous-name error.
- [ ] `send --to cc:<name>`: registry lookup, optional auth line from the `.key`
  file, frame per D7, `from` set when a shim socket exists for the calling Codex
  thread, otherwise omitted; refuses a socket outside the allowlisted dirs.
- [ ] D11 version pin and warning.
Success: `list --json`, the rollout reader and both `send` forms pass
`test_peers.py` against M4's fixtures with a fake `codex`, `ps` and `lsof` on
`PATH` and both roots in temp dirs.

**M2. Shim, `session-hook`, `up` / `down`.**
- [ ] `shim --thread <id>`: waits for a name, writes the record, unlinks a
  leftover socket, binds in the allowlisted directory, serves NDJSON in its own
  pid, translates `user` frames to a tagged `codex queue`, keeps the contact
  list (D8), answers `notify_when_idle` with `peer_idle_notice` when the thread
  goes idle, ignores unknown `control` actions, applies the rewrite-at-most-twice
  rule, removes record and socket on `SIGTERM` or when the `codex` pid dies.
- [ ] `session-hook`: on `SessionStart` daemonises one shim for `session_id`
  (idempotent on `resume`); on `SessionEnd` stops it.
- [ ] `up` / `down`: manual reconcile against `list`, idempotent.
Success: a shim started against a temp registry and a temp socket accepts a
`user` frame from a test client, invokes the fake `codex queue` with the tagged
body, answers `notify_when_idle`, refuses a foreign-uid client, and removes its
record and socket on `SIGTERM`; `session-hook` is idempotent on a fixture
payload.

**M3. `stop-hook`, `install-hook`, `doctor`.**
- [ ] `stop-hook`: D4 in full (skip on `stop_hook_active`, `turn_id` lookup,
  `sid=` verification, prior-contact rule, tag strip, dedupe state file, hop
  cap); always prints `{}` on stdout; exits 0 on a delivery failure (stderr).
- [ ] `install-hook`: backs up `$CODEX_HOME/hooks.json`, appends one entry each
  to the existing `SessionStart`, `SessionEnd` and `Stop` arrays (creating the
  file or an array if absent) keyed by the command string for idempotency, sets
  `features.hooks = true` in `config.toml` if unset, and prints the `/hooks`
  trust step the user must do in Codex.
- [ ] `doctor`: reports each entry's trust state from `[hooks.state]`, the
  installed versions against the pins, the socket directory in use, and whether
  `codex` and `lsof` are on `PATH`.
Success: `stop-hook` fed a fixture payload and rollout posts one frame per
target to a temp socket the test listens on, prints `{}`, exits 0 on a refused
connect, and posts nothing on a second identical run; `install-hook` is
idempotent on a fixture `hooks.json` and preserves the two existing `Stop`
entries; `doctor` reads a fixture `config.toml`.

**M4. Tests in `test.yml`, no live agents needed.**
- [ ] `scripts/test_peers.py` (unittest, stdlib): registry parsing and
  liveness with a fake `ps`; Codex discovery against an in-test `state_5.sqlite`
  schema and a fake `lsof`; rollout reader (status, user item, tag); tag build
  and parse round trip; target extraction and dedupe; frame construction (auth
  line present/absent, wrapped/bare per D7); socket directory allowlist with
  both dir shapes as fixtures; shim end to end on a temp socket in a thread
  (socket paths kept under the 104-byte macOS cap); `stop-hook` end to end;
  `install-hook` merge, backup and idempotency; `doctor` on fixtures; version
  warning with injected version strings.
- [ ] One `test.yml` step: `python3 skills/session-peers/scripts/test_peers.py`;
  `scripts/validate_skills.py` green.
Success: green on `ubuntu-latest`; macOS is covered by the local run recorded in
M6.

**M5. Documentation.**
- [ ] `SKILL.md`: frontmatter per skill-maker (third person, single line, no
  unquoted colon-space, triggers such as "message the codex session", "tell
  codex", "list codex sessions", "reply to the claude session", "@codex"); body
  under 500 lines, branched by agent, with install (`npx skills add`,
  `install-hook`, the `/hooks` trust step, `/rename`, `thread-title` in
  `[tui] status_line`, the optional Claude `SessionStart` snippet that runs `up`),
  the reply policy, and the limits (sizes, ambiguous names, sandbox, macOS and
  Linux only). `agnix --target claude-code skills/session-peers/SKILL.md` clean
  before commit (local step; agnix is not in CI).
- [ ] README: one row in "All skills" and the count 19 to 20; CHANGELOG
  `Unreleased` under `### Added` as "`session-peers`: ...".
Success: M6 records that its first round trip was done by following `SKILL.md`
verbatim.

**M6. End-to-end validation on this machine, recorded inline here.**
- [ ] Claude `@codex-uzi` (a `/rename`d thread) via `SendMessage` starts a
  Codex turn (time it).
- [ ] Codex first-line `@<claude-name>` reply arrives in that Claude session via
  the hook, after prior contact.
- [ ] Auto reply-to-sender: a Claude-queued turn's answer lands in the sender
  with no `@`.
- [ ] `notify_when_idle` from Claude on a Codex peer fires once when the Codex
  turn ends.
- [ ] Negatives: a message to a thread whose TUI has exited is refused with the
  "no active session" text; an echo loop stops at `hop>2`; an unsolicited
  `@name` to a session with no prior contact is dropped with a stderr line.
Success: all five boxes carry timestamps and measured figures; the header
Status line is updated.

## Success criteria

1. A Claude session lists every live `/rename`d Codex thread on the machine as
   a peer with no Claude-side configuration.
2. Claude to Codex delivery by `@name` completes in under 15 s worst case (the
   10 s queue poll plus the turn start), typically under 2 s.
3. Codex to Claude delivery through the `Stop` hook needs no sandbox change and
   no per-message approval in Codex.
4. A turn that a Claude session queued is answered back to that session with
   no addressing by the Codex model.
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
- **R2. Codex internals used by discovery (`state_*.sqlite` schema,
  `session_index.jsonl`, rollout event names) are undocumented.** `codex queue`
  itself is a supported CLI and is the only thing `send` needs; discovery
  degrades to "name your thread and pass it" if the DB moves. Every DB read is
  wrapped; an unknown schema yields an empty list plus a warning.
- **R3. Codex's sandbox.** Handled by D5; a future Codex release could run
  hooks inside the sandbox, which M6's negative test would catch.
- **R4. Hook trust UX.** A new entry is inert until trusted in `/hooks`; a
  skipped step looks like "replies never arrive". `install-hook` prints the
  step and `doctor` reports an untrusted entry.
- **R5. Queue latency and lost wakes.** The queue reader polls every 10 s and a
  thread not loaded in any live process accumulates silently; `send` checks
  liveness first and says so instead of queueing into a dead thread.
- **R6. Loops and cost.** Each delivered message is a paid turn on the
  receiving side. Claude's inbound rate limit, identical-repeat drop and 50-item
  queue cap cover the Codex-originated leg; D4's hop cap covers the other.
- **R7. Name collisions.** Two Codex threads named the same, or a Codex name
  equal to a Claude session's, make `@name` ambiguous. Codex refuses the queue;
  Claude disambiguates with its `[ref]` as it does for same-named Claude
  sessions.
- **R8. Multiple `Stop` hooks.** A third-party hook that continues the turn
  re-fires `Stop`; the dedupe state file and the `stop_hook_active` skip keep
  delivery to exactly once.

## Open questions (resolved during implementation)

- Q1. Which wrapper attributes are mandatory for a wrapped frame to render as a
  named peer, and is a wrapper without `from-mode` accepted? Resolved by M0,
  written into D7.
- Q2. Is a record without a `.key` file accepted by `SendMessage`? Resolved by
  M0.
- Q3. Does Codex fire `Stop` for a turn started by a queued message exactly as
  for a typed one (expected yes; the transcript shows the queued message as an
  ordinary `role: user` item)? Resolved by M0.
- Q4. Does the rollout carry `turn_id` on items, so the hook can find its own
  turn's user message when several queued items landed? Resolved by M0.
