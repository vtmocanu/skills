---
name: session-peers
description: Messages between Claude Code sessions and Codex CLI threads on one machine, including correlated request/reply without stale queued turns. Registers Codex as a Claude peer, delivers asynchronous messages through codex queue, and provides ask/reply/wait commands for supervised multi-round work. Use when sending cross-session messages or handoffs, requesting peer review, waiting on a peer, listing live sessions, or diagnosing a stuck, paused, or dead peer. Triggers include "message codex", "ask claude", "reply to the session", "@codex", "session peers", "peer review".
---

# session-peers

One script, `<this skill's directory>/scripts/peers.py` (Python 3 stdlib, macOS
and Linux), bridges the two vendors' native primitives: Claude Code's per-session
inbox socket and registry, and Codex CLI's `codex queue`. Read the section for
the agent you are.

## Terms

- **Claude session**: one running `claude` process; it has a name (`/rename`) and
  an inbox socket. Claude registers it natively and updates the record on rename.
  Do not create a second Claude registration hook.
- **Codex thread**: one conversation held open by a `codex` process; it has a
  name only after `/rename <name>` in the Codex TUI.
- **Shim**: a small process `peers.py` runs per registered Codex thread. It is
  what Claude sees as the peer. No shim, no peer.
- **Identity versus alias**: the session UUID is durable; the displayed peer
  name is mutable. Address by UUID when a rename may race a send.
- **Notification versus request**: `send` is asynchronous and may arrive in a
  later turn; `ask` waits for one correlated reply in the caller's current
  Codex turn and consumes it instead of queueing it later.

## If you are Claude Code

### Automatic attachment (recommended)

Install the Codex `SessionStart` hook once:

```bash
<this skill's directory>/scripts/peers.py install-hook --auto-attach
```

Then open `/hooks` in Codex and trust the new entry. Installing it is the
explicit opt-in: every root Codex thread attaches on `startup` or `resume`,
using the `session_id` from hook input. The attachment is session-scoped and
does not add an entry to the persistent manual-registration list.

Codex exposes no rename hook. The shim therefore re-reads the thread title every
30 seconds while retaining its 5-second liveness check. Set
`SESSION_PEERS_ALIAS_REFRESH_INTERVAL` to change the rename cadence. A valid
`/rename` value (letters, digits, dot, underscore and hyphen, up to 64
characters) becomes the peer alias without restarting the shim. An absent,
unsafe or conflicting title uses `codex-<uuid prefix>` instead. Confirm with
`/list-agents` (or `ListAgents`); the peer appears as `interactive` with `idle`
or `busy` status.

### Manual persistent attachment

Without the automatic hook, name the Codex thread and register it explicitly:

```bash
<this skill's directory>/scripts/peers.py up <name|uuid>
```

Manual registration persists the UUID so a later bare `up` can recreate its
shim. A brand-new thread can attach before its first turn because liveness uses
the writer lock Codex holds from thread creation, not only its lazy rollout.

### Messaging a Codex thread

Use `SendMessage` (or `@<name>` in the prompt) exactly as for another Claude
session. The shim tags the message with your name and reply address and queues
it; Codex runs it as its next user turn under the thread's own approval mode.

- **Replies normally come back on their own**: when that turn completes, the
  shim posts Codex's final message into this session as `Message from @<name>`,
  subject to the live-session and reply-budget checks below. If it is missing,
  check the delivery log (Diagnostics); completion and idle notices do not
  prove delivery. `notify_when_idle: true` fires once per turn end.
- **Reply budget**: the shim delivers at most 3 consecutive replies to one peer
  inside a 30-minute idle window. A direct Codex turn, a request from another
  peer, the idle window, `peers.py budget reset <name|uuid>`, or a fresh `up`
  resets the sequence. A blocked fourth reply stays visible in Codex, and the
  requesting Claude session receives a correlated `failed` status instead of
  waiting silently. This stops tight unattended ping-pong without turning the
  budget into a lifetime counter.
- **Idle thread latency**: up to 10 s (Codex polls its queue), then the turn.
- **Busy thread**: the message queues and runs after the current turn.
- **Continuously-driven thread**: a thread another driver keeps feeding
  back-to-back turns (an autonomous loop, or another session in a tight drive)
  never goes idle to poll the queue, so your message can sit undelivered for a
  long time -- it is not busy for one turn but busy on a stream of them. If no
  reply arrives, check the thread's rollout for your message text; if it is
  absent, the queue has not drained. Ask the user to let the thread idle, or
  deliver out of band.
- **Paused thread**: after the user's Ctrl-C in Codex the queue stays paused,
  even across `codex resume`, until they type any prompt there. Your message is
  still queued; the shim posts a status `held` with the detail "queued, but the
  Codex thread is paused after an interrupt; it drains when its user types the
  next prompt", but whether that renders as a notice in your session is not yet
  verified on 2.1.263. The reliable diagnostic is the shim log
  `$CODEX_HOME/session-peers/<thread uuid>.log`, which records `paused after an
  interrupt`. Tell the user to type something in that Codex window.
- **Dead thread**: `send` refuses with `no active session ... its process has
  exited`; the shim reports status `failed`. Ask the user to
  `codex resume <uuid>` and type a prompt.
- **Size**: the text must fit one `codex queue` argument, so the real cap is
  the OS argv budget in UTF-8 bytes (well under Codex's own 1 MiB character cap
  on macOS); `send` refuses an oversized text, the shim trims it on a UTF-8
  boundary and reports status `truncated` with the trimmed length.
- **Status frames** you may receive about a message: `held` (queued, thread
  paused), `failed` (queue error, dead thread, or exhausted loop guard), and
  `truncated` (delivered, but cut to the argv budget).

### Replying to a waiting Codex request

A message wrapped in `<session-peers-request ...>` came from `peers.py ask`.
Complete the requested work, write the complete response to a private scratch
file, then run the exact `peers.py reply --request ... --message-file ...`
command included in the message. Confirm `replied to request ...` before ending.

Do not use `SendMessage` or `send --to codex:` for that response. Those are
asynchronous and would reach Codex as a later user turn after `ask` timed out or
moved on. A request is single-use, bound to this Claude session, and expires at
its stated timeout. An expired or unknown request must fail rather than create a
fallback queue message.

### One-shot from a shell, no shim

```bash
<this skill's directory>/scripts/peers.py list --json
<this skill's directory>/scripts/peers.py send \
  --to codex:<name|uuid> --message-file <path> --json
```

When the Codex state schema is recognised, `send` checks liveness first and
queues by UUID. In degraded mode (unknown schema, see `doctor`), a UUID target
still queues and prints `liveness unverified`, while a name target is refused.
Without a shim the reply is visible only in the Codex TUI. `--from-socket <path>`
routes the reply to the Claude session listening on that socket:
`send` resolves its live registry record to fill the name and session id and
refuses when nothing listens there. A tag without a session id is never
auto-delivered. `send --to cc:<name|uuid>` posts a wrapped message directly
into a Claude session from a host shell; UUID is stable across renames.

Every direct `send` generates a message id, includes it as `mid` on a Codex
message, and reports it in `--json` output. When `send --to codex:...` runs from
a Claude Code Bash tool, it automatically uses
`CLAUDE_CODE_MESSAGING_SOCKET` to resolve the sender's current name, UUID,
and reply route. Do not hand-build `--from-name`, `--from-sid`, or
`--from-socket` there. Outside Claude Code, an identity-free send is still
allowed but prints a warning and cannot route an automatic reply.

### Keeping shims alive

Shims exit when their thread's process holds neither the rollout nor the writer
lock (i.e. the thread is gone), on `down`, or on reboot (`/tmp` is cleared).
Claude already manages its own registry and rename lifecycle, so no Claude hook
is needed. A bare `down` stops every shim
and keeps the registrations (a bare `up` brings them back); `down <name|uuid>`
also unregisters. On first startup, the shim recovers an already-running turn's
request and reply address, without replaying completed turns. A restart from
saved state delivers a reply that completed while it was down only if that
turn finished within the last 15 minutes. An unnamed thread
registered by UUID appears as `codex-<first 8 hex of the uuid>`.

## If you are Codex

The default sandbox can block Unix sockets, the Claude-side `ps` probe, and the
Codex-side `lsof` probe. A failed `lsof` probe keeps a running shim alive but
refuses new queueing and GC until liveness can be verified.
Paths proven absent are omitted before a batched `lsof` call, so a lazy rollout
or stale database row does not hide holders returned for other paths. Any
failure to inspect a path still makes liveness unverified. An `lsof` exit 1
with no diagnostic is treated as a verified partial or empty result.
Run `list`, `doctor`, and direct `send --to cc:...` with host permission from
the outset. If permission is unavailable, an `unverified` result is not
evidence that no session exists. A Claude record missing `procStart` is also
unverified rather than trusted across possible PID reuse. Two ways a message
reaches Claude:

1. **Reply to the sender**: when your turn was started by a Claude session (the
   user message starts with a `[session-peers from=@<name> ...]` line), your
   final message is forwarded after the turn completes, subject to the checks
   above. Write the answer as your last message; keep it self-contained. Do
   not claim it was sent before the delivery log confirms the send.
2. **Address a session**: put `@<claude-session-name>` as the very first line of
   your final message. Delivery is allowed only to a session that has messaged
   this thread before (prior contact), unless the user set
   `SESSION_PEERS_ALLOW_UNSOLICITED=1` for the shim.

Both count toward the reply budget above; a dropped reply is still shown to the
user in your TUI. To see live Claude sessions, mutable names, and stable UUIDs:

```bash
<this skill's directory>/scripts/peers.py list --json
```

### Codex to Claude: choose `ask` or `send`

Use `ask` for peer review, brainstorming, or any multi-round task whose answer
must be consumed in the current Codex turn:

```bash
<this skill's directory>/scripts/peers.py ask \
  --to cc:<name|uuid> --message-file <request-path> --timeout 600 --json
```

`ask` reads `CODEX_THREAD_ID` automatically (or accepts `--from-thread`), sends
a single-use request mailbox to that exact Claude session, waits 600 seconds by
default (`--timeout`, maximum 3600), and prints the reply without placing it in
`codex queue`. Timeout exits
124 and deletes the mailbox, so a late response cannot appear as a stale user
turn. Request/reply files are mode 0600 inside the mode-0700 bridge directory;
only the intended Claude session may answer. The request deliberately omits the
shim/native reply route, so even a mistaken ordinary peer reply has no route
back to the Codex queue; the included `reply` command is the only response path.

Use asynchronous `send` for notifications, handoffs, or a final response that
may safely become a later turn:

```bash
<this skill's directory>/scripts/peers.py send \
  --to cc:<name|uuid> --message-file <path> --json
```

`send` also reads `CODEX_THREAD_ID` automatically. A live shim makes the native
reply route available; without one the command warns that replies cannot route.
Every send returns a message id. Use `--message` for short text and
`--message-file` for substantial content to avoid shell quoting and command
substitution. Message files must be valid UTF-8; invalid bytes fail instead of
being silently rewritten.

Wait for an asynchronously working Claude peer without polling `list`:

```bash
<this skill's directory>/scripts/peers.py wait \
  --for cc:<name|uuid> --state idle --timeout 600 --json
```

For multi-round collaboration, use one `ask` per round and send only the final
artifact asynchronously. Never scrape Claude's internal transcript JSONL to
obtain a reply: that bypasses correlation, can expose unrelated or sensitive
context, and leaves the queued reply to arrive stale later.

If normal automatic forwarding failed and asynchronous sending is authorized,
use `send` with host permission. Otherwise ask the user to run it from a host
shell. Confirm the successful result and message id before reporting delivery.

## Codex hook

`install-hook --auto-attach` appends one `startup|resume` `SessionStart` entry
to `$CODEX_HOME/hooks.json`, backing up an existing file first. Re-running it
updates the session-peers entry in place and preserves other hook groups. Codex
requires review through `/hooks` before a new or changed entry runs.

Hooks are enabled by default. The installer never edits `config.toml` and never
overrides an explicit `[features] hooks = false`; `doctor` reports that disable.
Plain `install-hook` retains reconcile-only mode for users who want only manual
persistent registrations.

## Garbage collection

The SessionStart worker removes bridge metadata whose thread has been inactive
for seven days. It rechecks that the Codex thread is not live and that no shim
owns the PID file before deleting anything. Only files under
`$CODEX_HOME/session-peers/` are eligible; Codex rollouts, writer locks, and
queued messages are never touched.

Persistent manual registrations are bridge metadata and expire too. Resuming a
thread with the auto-attach hook exposes it again; without that hook, run `up`
again after a seven-day inactive period.

```bash
<this skill's directory>/scripts/peers.py gc --dry-run
<this skill's directory>/scripts/peers.py gc --days 7
```

Set `SESSION_PEERS_GC_DAYS` to change automatic retention.

## Diagnostics

```bash
<this skill's directory>/scripts/peers.py doctor
```

Reports the socket directory, registered versus live shims, hook trust state,
process and Unix-socket capability, stale metadata count, `codex` and `lsof` on
`PATH`, and version drift. Ordinary commands rate-limit an unchanged version
warning to once per 24 hours; `doctor` always reports the current comparison. Re-run
`<this skill's directory>/references/spike-checklist.md` after an upgrade.

Before claiming a reply was sent, check
`$CODEX_HOME/session-peers/<thread uuid>.log` (default home: `~/.codex`) for
`delivered turn <turn id> to <session name>`, or confirm receipt in the target
session. A successful socket write is transport evidence, not proof the target
agent has read or acted on it. The state JSON's `processed_turns` list includes
skipped replies and is only a deduplication ledger; older state files called
it `delivered`, which also did not prove a send. The script reads that legacy
key on upgrade. The startup failure and misleading field were reproduced and
corrected on 2026-09-08; see the startup checks in the spike checklist.

## Limits

- macOS and Linux only (Windows uses named pipes).
- Message body cap 1 MiB on both sides; Claude also refuses rapid bursts to one
  session and drops identical repeats.
- `ask` is Codex-to-Claude only, accepts one reply, waits at most one hour, and
  requires the Claude peer to run the included `reply` command.
- One reply per turn: a `Stop`-hook continuation or an interrupted turn
  (`turn_aborted`) delivers nothing; a completed turn with no final text
  delivers nothing.
- First startup retains the active request and skips completed history.
  Delivery across a restart from saved state is bounded: a tagged turn that
  completed while no shim ran is delivered only if it finished within 15
  minutes of the shim starting; older ones are skipped.
- Registry, socket allowlist and rollout event names are undocumented vendor
  internals; `doctor` warns on version drift and `send --to codex:` keeps working
  through `codex queue` even if the peer listing breaks.
