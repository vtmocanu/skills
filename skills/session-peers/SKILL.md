---
name: session-peers
description: Messages between Claude Code sessions and Codex CLI threads on one machine. Registers a Codex thread as a real Claude peer so it shows in ListAgents and the @ typeahead, delivers with codex queue, and pushes Codex replies back into the Claude session that asked. Use when (1) a Claude session must tell, ask or hand work to a running Codex thread, (2) a Codex thread must answer or notify a Claude session, (3) listing which Codex threads or Claude sessions are live, (4) a message to a Codex peer seems stuck (paused queue, dead thread, budget). Triggers include "message the codex session", "tell codex", "ask codex", "list codex sessions", "reply to the claude session", "@codex", "codex peer", "session peers".
---

# session-peers

One script, `<this skill's directory>/scripts/peers.py` (Python 3 stdlib, macOS
and Linux), bridges the two vendors' native primitives: Claude Code's per-session
inbox socket and registry, and Codex CLI's `codex queue`. Read the section for
the agent you are.

## Terms

- **Claude session**: one running `claude` process; it has a name (`/rename`) and
  an inbox socket.
- **Codex thread**: one conversation held open by a `codex` process; it has a
  name only after `/rename <name>` in the Codex TUI.
- **Shim**: a small process `peers.py` runs per registered Codex thread. It is
  what Claude sees as the peer. No shim, no peer.

## If you are Claude Code

### First time on this machine

1. The user names the Codex thread in its TUI: `/rename <name>`. Letters,
   digits, dot, underscore and hyphen only, up to 64 characters; anything else
   is refused at `up`. Add `thread-title` to `[tui] status_line` in
   `$CODEX_HOME/config.toml` to see the name in the status bar.
2. Register it and start its shim:

   ```bash
   <this skill's directory>/scripts/peers.py up <name>
   ```

   `up` resolves the name once, refuses a name held by two live threads
   (register by UUID instead), persists the UUID, and daemonises the shim. A
   later `/rename` of a new thread needs another `up <name>`; a bare `up`
   restarts shims for every registered thread that is live again.

   A brand-new thread registers even before it has run a turn: liveness comes
   from the writer lock Codex holds from thread creation, not from the rollout
   `.jsonl`, which Codex may create later (it can appear during the first turn,
   verified on codex-cli 0.153.4 on 2026-09-08). If a build without the
   writer lock ever refuses `up <name>` on a just-renamed thread with "no live
   Codex thread named ... (N past thread(s) carried that name)", have the user
   type any prompt directly in that Codex TUI so its rollout is written, then
   retry. `peers.py send` will not help there: it runs the same liveness check,
   so without a rollout or a lock it refuses the send too.
3. Confirm with `/list-agents` (or `ListAgents`): the thread appears under its
   Codex name as `interactive`, `idle` or `busy`.

### Messaging a Codex thread

Use `SendMessage` (or `@<name>` in the prompt) exactly as for another Claude
session. The shim tags the message with your name and reply address and queues
it; Codex runs it as its next user turn under the thread's own approval mode.

- **Replies normally come back on their own**: when that turn completes, the
  shim posts Codex's final message into this session as `Message from @<name>`,
  subject to the live-session and reply-budget checks below. If it is missing,
  check the delivery log (Diagnostics); completion and idle notices do not
  prove delivery. `notify_when_idle: true` fires once per turn end.
- **Reply budget**: the shim delivers at most 3 replies in a row to
  bridge-originated turns per (thread, session); past that it drops replies with
  a stderr line until the user runs `peers.py budget reset <name>` or a fresh
  `up <name>`. This stops two agents from ping-ponging unattended.
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
  paused), `failed` (queue error or thread died), `truncated` (delivered, but
  cut to the argv budget).

### One-shot from a shell, no shim

```bash
<this skill's directory>/scripts/peers.py list --json
<this skill's directory>/scripts/peers.py send --to codex:<name|uuid> --message "<text>"
```

When the Codex state schema is recognised, `send` checks liveness first and
queues by UUID. In degraded mode (unknown schema, see `doctor`), a UUID target
still queues and prints `liveness unverified`, while a name target is refused.
Without a shim the reply is visible only in the Codex TUI. `--from-socket <path>` routes the reply to the
Claude session listening on that socket: `send` resolves its live registry
record to fill the name and session id itself and refuses when nothing
listens there; a tag without a session id is never auto-delivered. `send --to cc:<name>` posts a wrapped message straight
into a Claude session from a host shell.

### Keeping shims alive

Shims exit when their thread's process holds neither the rollout nor the writer
lock (i.e. the thread is gone), on `down`, or on reboot (`/tmp` is cleared). No Claude-side hook is part of delivery. As a
convenience only, a Claude `SessionStart` hook in the effective settings file
(`$CLAUDE_CONFIG_DIR/settings.json` when that variable is set, otherwise
`~/.claude/settings.json`) that runs `<this skill's directory>/scripts/peers.py
up` re-creates shims for already registered threads at Claude start; it
registers nothing and is safe to omit. A bare `down` stops every shim
and keeps the registrations (a bare `up` brings them back); `down <name|uuid>`
also unregisters. On first startup, the shim recovers an already-running turn's
request and reply address, without replaying completed turns. A restart from
saved state delivers a reply that completed while it was down only if that
turn finished within the last 15 minutes. An unnamed thread
registered by UUID appears as `codex-<first 8 hex of the uuid>`.

## If you are Codex

The default sandbox blocks Unix sockets; the shim normally delivers for you.
Two ways a message reaches a Claude session:

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
user in your TUI. To see live Claude sessions and their names:

```bash
<this skill's directory>/scripts/peers.py list --json
```

If automatic forwarding failed and sending is authorized, use
`peers.py send --to cc:<name> --message "<text>"` from an execution context that
permits local sockets, requesting tool escalation when required. Otherwise ask
the user to run it from a host shell. Confirm the command's successful
`sent to <name>` result before reporting delivery.

## Optional Codex hook

`<this skill's directory>/scripts/peers.py install-hook` appends one
`SessionStart` entry to `$CODEX_HOME/hooks.json` (backing it up first) that runs
`up` on every session start, so registered threads get their shims back without
a manual step. Codex trusts a new hook only after the user reviews it in the TUI
with `/hooks`; until then it is inert. `install-hook` prints that step. It also
sets `hooks = true` under `[features]` in `config.toml`, but only on Python 3.11
or newer, where it can parse the file before and after and prove the edit
touched nothing else; on 3.9 and 3.10, or when the file does not parse or the
edit would reach beyond that one key, it leaves `config.toml` alone and prints
the line to add by hand. Nothing in delivery depends on the hook.

## Diagnostics

```bash
<this skill's directory>/scripts/peers.py doctor
```

Reports the socket directory in use, registered versus live shims, the hook's
trust state, `codex` and `lsof` on `PATH`, and a warning when the installed
Claude Code or Codex version is newer than the one this skill was validated on
(then re-run `<this skill's directory>/references/spike-checklist.md`).

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
