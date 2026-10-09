# Diagnose peer delivery

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
  paused), `failed` (queue error, dead thread, exhausted loop guard, or a turn
  that finished with no final message), and `truncated` (delivered, but cut to
  the argv budget). For a turn with no final message, the shim attempts one
  plain notice to the requester when its tag verifies against a live session,
  plus the correlated `failed` status when the message has an id. An unverified
  tag, or a requester that has gone away, still gets nothing: check the
  delivery log described below.

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

## Diagnostics

```bash
<this skill's directory>/scripts/peers.py doctor
```

Reports the socket directory, registered versus live shims, hook trust state,
process and Unix-socket capability, stale metadata count, `codex` and `lsof` on
`PATH`, and version drift. It also warns for each **live Codex thread that has no
shim and is not registered** (`NAME: live Codex thread, not attached (run peers.py up UUID)`).
In that state a message queues but no reply routes back, and `ListAgents`
cannot show it. Ordinary commands rate-limit an
unchanged version warning to once per 24 hours; `doctor` always reports the
current comparison. After an upgrade, read
`<this skill's directory>/references/spike-checklist.md` and perform its checks.

`list` also shows live and unverified Codex threads found under another
candidate `CODEX_HOME` (see `references/attach-and-lifecycle.md`), once each,
with `[home PATH]` when it differs from the caller's and `codex_home` in
`--json`. `reply --request` and `await` find a request mailbox under another
candidate home the same way.

Before claiming a reply was sent, check
`$CODEX_HOME/session-peers/<thread uuid>.log` (default home: `~/.codex`) for
`delivered turn <turn id> to <session name>`, or confirm receipt in the target
session. A successful socket write is transport evidence, not proof the target
agent has read or acted on it. A `queued inbound message <msg_id>` line proves
the shim passed that message to `codex queue`, not that Codex processed it. A
message that fails to queue (`queue failed for message` in
that log) also sends the sender one plain `was not queued` notice. The shim runs
from `$HOME` and queues from the thread's own directory (`$HOME` if that is
gone), so removing the directory `up` ran from no longer breaks it. The state JSON's `processed_turns` list includes
skipped replies and is only a deduplication ledger; older state files called
it `delivered`, which also did not prove a send. The script reads that legacy
key on upgrade. Use [the spike checklist](spike-checklist.md) to verify startup and restart behavior.

## Limits

- macOS and Linux only (Windows uses named pipes).
- Message body cap 1 MiB on both sides; Claude also refuses rapid bursts to one
  session and drops identical repeats.
- `ask` is Codex-to-Claude only, accepts one reply, waits at most one hour, and
  requires the Claude peer to run the included `reply` command.
- One reply per turn: a `Stop`-hook continuation or an interrupted turn
  (`turn_aborted`) delivers nothing; a completed turn with no final text
  delivers no reply; the shim only attempts a notice (and a `failed` status
  when the message has an id) to a verified live requester.
- Registry, socket allowlist and rollout event names are undocumented vendor
  internals; `doctor` warns on version drift and `send --to codex:` keeps working
  through `codex queue` even if the peer listing breaks.

## Running code after an upgrade

`list --json` reports `shim_code_status` for an attached thread: `current` when
the running shim's startup source digest matches this installed CLI, `stale`
when it differs, and `unknown` for old state without a digest, unreadable code,
or state that does not identify the running PID. With no shim, the field is null.
`doctor` and human-readable `list` also report the comparison. A stale shim prints
`peers.py restart <uuid>`; run it to adopt installed code while preserving spent
budgets. Detection never restarts a shim or resets an allowance. The digest is
diagnostic source identity, not authentication or proof of vendor compatibility.
