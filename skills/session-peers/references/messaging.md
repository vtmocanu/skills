# Detailed messaging contracts

### Messaging a Codex thread

Use `SendMessage` (or `@<name>` in the prompt) exactly as for another Claude
session. The shim tags the message with the available name and reply address,
adds the verified runtime and identity (or `peer of unverified runtime` when
identity is unavailable), and queues it. Codex runs it as its next user turn
under the thread's own approval mode.

For a request whose reply gates an action, use `peers.py list` to identify the
live thread and its busy/idle state, then match the reply's message-id header
before acting. `SendMessage` and `peers.py send --to codex:<uuid>` both report
queue acceptance before a busy thread processes the message. Use the direct
`send` command when an alias is ambiguous or you need its explicit CLI result;
it does not bypass the busy thread's queue.

- **Peer unreachable**: when `SendMessage` reports no reachable agent by that
  name, or `ListAgents` omits a Codex thread you expect, run `peers.py list`. A
  live thread shown `not registered` with no `shim <pid>` has no active shim: run
  `peers.py up <uuid>`, then `SendMessage` it by name (`peers.py send` needs a
  typed target, see [Route check](#route-check)).
- **Replies normally come back on their own**: when that turn completes, the
  shim posts Codex's final message into this session as `Message from @<name>`,
  subject to live-session verification and [reply budgets](budgets.md). If it is missing,
  check the delivery log ([diagnostics](diagnostics.md)); completion and idle notices do not
  prove delivery. `notify_when_idle: true` fires once per turn end.
- **Reply header**: a reply to your own message starts with
  `[in reply to message <msg_id>]`, the `msg_id` your `SendMessage` returned.
  When messages cross, match it before acting on the reply; a reply to an
  older message cannot satisfy the current gate (see [the core buddy rules](../SKILL.md#buddy)). It follows the
  `[session-peers from Codex thread NAME (UUID)]` provenance line. Strip both
  lines before parsing a reply body as exact text or JSON. Correlated
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
auto-delivered. `send --to cc:<name|uuid>` also permits plain-shell sends,
labelled `peer of unverified runtime` when no thread identity is available;
UUID is stable across renames. Claude Code sessions must use `SendMessage`
for a Claude peer instead.

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
`--from-socket` there. An identity-free send to a Codex target is still
allowed but prints a warning and cannot route an automatic reply.
`status: queued` means `codex queue` accepted the message, not that the thread
has read it. A unique live `codex-<UUID prefix>` or raw UUID prefix of at least
eight hex characters also resolves; ambiguous prefixes fail with candidate
UUIDs and titles. Use the full UUID for consequential requests.

### Critical-message journal

- `send --record` journals the body before the transport call; the journal id
  equals the message id, and `--json` adds a `journal` object. A journal failure
  exits before sending (75 full, 1 otherwise). Exit 76 means a journal update
  after a transport attempt failed: if the printed transport result reports
  success the message went out, so do not resend; if it reports a delivery
  failure, delivery is unconfirmed, so check the recipient before resending. See
  [the journal](journal.md#exit-codes).
- Every correlated `ask`/`dispatch` request and its `reply` is journaled, with no
  flag. The request mailbox is written first, then the request is journaled,
  then it is sent. If the mailbox write fails, the command exits 1 with nothing
  journaled or sent. If journaling fails, the mailbox is removed and the command
  exits 75 (full) or 1. A crash between the two leaves only an unjournaled
  mailbox until it expires; the normal expiry cleanup reclaims it and it holds no
  journal capacity.
- `ask`, `dispatch` and `await` refuse (exit 2) a `--from-thread` that differs
  from the Codex thread the environment names (`CODEX_THREAD_ID`/
  `CODEX_SESSION_ID`), because the journal's `from` is that identity. The check
  applies only when both values are UUIDs; `send --from-thread` has no such
  check.

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

### Sending to the browser peer

`peers.py web --chat` registers the browser as the peer `web:<uuid>` (see
[web](web.md#chat-mode---chat); the address is in the page's details panel and
in the `chat` object of its state). OpenCode, a shell or any session replies or
writes to it with `send`:

```bash
<this skill's directory>/scripts/peers.py send --to web:<uuid> --message-file <path>
```

The message arrives in the page's Browser feed. It shows your verified identity only when
the call comes from a Claude Code session or a Codex shim, because the frame's
`from` is set only from a Claude-registry record at the caller's socket; from
OpenCode, a shell or a Codex thread calling `send` directly it appears as an
unverified sender and the journal records `from: null`.
`send` waits for the browser peer's own acknowledgement, which it gives only
after the message is in the [journal](journal.md); the exit code says what is
known:

| Exit / outcome | Meaning |
| --- | --- |
| 0 | Delivered: the browser peer journaled the message and acknowledged it. |
| 1, not delivered | The browser peer refused it (for example journal full) or could not be reached. |
| 1, outcome unknown | No acknowledgement within 15 s, or the connection closed first. The message may or may not have been kept; check before resending: the page's Browser feed, or `inbox journal` for a `--record` send. |
| 75 | `--record` with a full journal: nothing was sent. |
| 76 | With `--record`, a refused or unknown outcome whose transport error could not be journaled. |

- **Offline:** with no running browser peer, `send` is refused (exit 1) and
  nothing is sent. With `--record` it journals the message, reports the
  transport failure and exits 1 (76 if that failure could not be journaled), so
  the page can show it once it starts.
- **Duplicate record:** with `--record` a delivered message leaves two journal
  records, the sender's own and the browser peer's inbox record. Both count
  against retention. The sender's own record counts against its quota; the
  inbox record counts against the sender's quota only for a verified sender,
  and against the journal's unattributed (`null`) bucket for an unverified one.
- **No correlation:** `ask`, `dispatch` and `wait` refuse a `web:` target
  (rc 2): a person answers by hand, with no request id or await. Use `send`.
- A direct `send` is not an approval: the recipient is a person at a page, and
  what you send is read as peer input.

### Route check

`send`, `ask`, `dispatch` and `wait` check the route before delivering. The
target must be typed (`cc:`, `codex:`, `oc:` or, for `send` only,
`web:`) or the `buddy` alias, which
expands to the bound buddy's typed ID first; a typed name or UUID prefix still
resolves as before.

- **Bare session name**: refused (rc 2) with the typed command to run, other options
  kept. A `--message` becomes `--message-file <PATH>` followed by
  `(write the message to PATH)`. If the target's ID is not a well-formed UUID or
  `ses_` ID, no command is printed. Resolved IDs that are not well-formed print
  quoted (`repr`); a session name that is not a plain peer name is replaced by
  its typed ID. A target with control characters is printed escaped. An option
  with control characters suppresses command advice. A name that resolves only
  to a dead Codex thread
  gets no command: nothing can deliver to it.
- **Ambiguous name**: the error lists every candidate with kind, liveness and
  typed ID (live first, then recent OpenCode sessions, then unverified, then
  dead; nothing is capped),
  including dead or stale ones and Codex threads that are all dead. A shim's
  registry record merges into its `codex:<uuid>` line. A bare-name ambiguity is
  rc 2; an ambiguous typed name keeps the resolver message and rc 1, with the
  candidates after `pick one typed ID:`. `buddy set`, `up`, `restart`, `down`
  and `budget` print the same candidate lines.
- **Check and delivery agree**: delivery re-verifies the checked identity and
  refuses, sending nothing, with `<target> changed between the route check and
  delivery` (rc 1).

| Sender | Verb and target | Result |
| --- | --- | --- |
| any | `ask`/`dispatch` to a Codex thread (`codex:`, a `cc:` shim, or a Codex buddy) | Refused. `send` is asynchronous (no request id, single-use reply or await) and is not an equivalent. For a Claude sender it also cites #88: no correlated Claude-to-Codex request exists yet. |
| any | `wait` on a Codex thread | With a running shim, run `wait --for cc:<uuid>`. Live thread without a shim: `peers.py restart <uuid>` first (it keeps the reply budget, unlike `up`), then that `wait`. Dead thread: no equivalent. |
| Claude session | `send`/`ask`/`dispatch` to a Claude session (`wait` is allowed) | Refused: use native `SendMessage`. The name is advised only when verified unique (complete registry read, no live or unverified namesake, and the same live process still holds that name); otherwise the advice says to `/rename` or pick it in `ListAgents`. The typed ID is shown for disambiguation only; never pass it to `SendMessage`. |

The sender counts as a Claude session when `CLAUDE_CODE_SESSION_ID` or
`CLAUDE_CODE_MESSAGING_SOCKET` is set and there is no valid Codex id
(`--from-thread`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID`); a valid Codex id
makes it a Codex sender. Beside a Claude session, an invalid Codex id is
refused (rc 2): the sender cannot be attributed, nothing is sent. An OpenCode id
beside a Claude or valid Codex one, without explicit `--from-*` flags, cannot be
attributed: `send`/`ask`/`dispatch` to a Claude session are refused (rc 2) with
"pass explicit --from-* flags", and the Claude-only #88 note is omitted. Once
the target resolves, its other routes are left to each
command's existing identity checks, unchanged (for example, `send` refuses it with "pass explicit
`--from-*` flags"). `ask`, `dispatch` and `wait` validate `--timeout` before the
route check.

A `#NAME` target is a [channel](channels.md): it is resolved against the channel
store (unknown channel: refused, rc 2) and fanned out, each member checked with
this route check. The Claude-session refusal above has one exemption: a channel
delivery to a Claude member, only after the channel exists and the sender is a
member. A direct `send --to cc:<uuid>` from a Claude session stays refused.

Not covered: a native `SendMessage` aimed at a Codex shim never passes through
`peers.py`, so it cannot be intercepted. It is asynchronous and uncorrelated,
and there is no correlated equivalent until #88. The CLI tests prove which
advice is emitted, not that native `SendMessage` accepts the advised name.
[spike-checklist.md](spike-checklist.md) measures a by-name `SendMessage` to a
Codex shim's registry record only; by-name delivery to a Claude session is not
separately measured.
