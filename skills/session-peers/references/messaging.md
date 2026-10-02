# Detailed messaging contracts

### Messaging a Codex thread

Use `SendMessage` (or `@<name>` in the prompt) exactly as for another Claude
session. The shim tags the message with your name and reply address and queues
it; Codex runs it as its next user turn under the thread's own approval mode.

For a request whose reply gates an action, use `peers.py list` to identify the
live thread and its busy/idle state, then match the reply's message-id header
before acting. `SendMessage` and `peers.py send --to codex:<uuid>` both report
queue acceptance before a busy thread processes the message. Use the direct
`send` command when an alias is ambiguous or you need its explicit CLI result;
it does not bypass the busy thread's queue.

- **Peer unreachable**: when `SendMessage` reports no reachable agent by that
  name, or `ListAgents` omits a Codex thread you expect, run `peers.py list`. A
  live thread shown `not registered` with no `shim <pid>` has no active shim: run
  `peers.py up <uuid>`, then send by the bare name.
- **Replies normally come back on their own**: when that turn completes, the
  shim posts Codex's final message into this session as `Message from @<name>`,
  subject to live-session verification and [reply budgets](budgets.md). If it is missing,
  check the delivery log ([diagnostics](diagnostics.md)); completion and idle notices do not
  prove delivery. `notify_when_idle: true` fires once per turn end.
- **Reply header**: a reply to your own message starts with
  `[in reply to message <msg_id>]`, the `msg_id` your `SendMessage` returned.
  When messages cross, match it before acting on the reply; a reply to an
  older message cannot satisfy the current gate (see [the core buddy rules](../SKILL.md#buddy)). Strip that first
  line before parsing a reply body as exact text or JSON. Correlated
  `ask`/`await` replies and `@name` replies to another session carry no header.

### Replying to a waiting Codex request

A message wrapped in `<session-peers-request ...>` came from `peers.py ask` or `peers.py dispatch`.
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

`list --json` reports Codex threads under `.codex[]` (proven live) and
`.codex_unverified[]` (liveness unproven), and each entry carries an explicit
`live` field, `true` on a `.codex[]` entry, `null` on an unverified one, so a
caller reads liveness directly instead of inferring it from `holder_pid`.
`.codex[]` stays live-only, so `live` is never `false` there.

Every direct `send` generates a message id, includes it as `mid` on a Codex
message, and reports it in `--json` output. When `send --to codex:...` runs from
a Claude Code Bash tool, it automatically uses
`CLAUDE_CODE_MESSAGING_SOCKET` to resolve the sender's current name, UUID,
and reply route. Do not hand-build `--from-name`, `--from-sid`, or
`--from-socket` there. Outside Claude Code, an identity-free send is still
allowed but prints a warning and cannot route an automatic reply.
`status: queued` means `codex queue` accepted the message, not that the thread
has read it. A unique live `codex-<UUID prefix>` or raw UUID prefix of at least
eight hex characters also resolves; ambiguous prefixes fail with candidate
UUIDs and titles. Use the full UUID for consequential requests.

### Codex to Claude: choose `ask`, `dispatch`/`await`, or `send`

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

Use `dispatch` plus `await` when the request is `ask`'s but the coordinator must
keep working instead of blocking one whole tool call for the peer's task:

```bash
<this skill's directory>/scripts/peers.py dispatch \
  --to cc:<name|uuid> --message-file <request-path> --timeout 1800 --json
# ... do independent local work ...
<this skill's directory>/scripts/peers.py await --request <uuid> --timeout 600 --json
```

`dispatch` creates the same single-use mailbox, correlation envelope, and
`reply_route=False` safety as `ask`, sends it, and returns at once with
`status: socket_write_succeeded` and the `request_id`, target session UUID, and
`expires_at`. Its `--timeout` sets the request lifetime, not a wait. `await`
consumes only that request's reply, exactly once, and prints it like `ask`. The
two timeouts are distinct: `await --timeout` bounds only that call. A call that
times out while the request is still live returns `status: pending` (exit 124)
and leaves the mailbox intact, so a later `await` resumes it; a request past its
`expires_at` returns `status: expired` (exit 1). `await` is bound to the
dispatching thread and the target session UUID, so a rename cannot retarget it
and only the dispatcher may consume the reply. A reply that arrives after an
`await` gave up is still just a filesystem write: it never becomes a queued
Codex turn. This path does not consume the shim reply budget, so keep the loop
supervised and bounded: dispatch one task, work, await and verify, then dispatch
the next only when it is useful.

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
