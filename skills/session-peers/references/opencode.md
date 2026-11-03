# OpenCode V2 sessions

Use the V2 API through the running service's authenticated CLI.

## Transport

- Every call runs `opencode api <operationId>`, the CLI's authenticated client
  for the background service. No auth, state or database file is read.
- Discovery reads only `session.get`, `session.list` (`parentID=null`) and
  `session.active`. The explicit inbox command additionally reads
  `session.inbox.list`, restricted to the caller's own queued peer input.
  Never `session.message.*`, `export` or `log`: no conversation history.
- Write only: `session.prompt` with `delivery: "queue"` and correlation in
  `metadata.session_peers` (`message_id`, `request_id`, `from`). No message
  `id` is supplied: the API requires `msg_` ids and orders them.
- `opencode api` starts a stopped service (`service.ensure`). Every call checks
  `opencode service status` first; `stopped` is reported, never started.
- Only the background service is covered, not `--standalone` private servers.
- The request body is one argv element, so a message must fit the exec budget.

## Identity and addressing

- Address an OpenCode session as `oc:<ses_id>` (or `oc:<title>` when exactly one
  recent top-level session has that title). Typed identities carry the `ses_`
  id unchanged in their `uuid` field; no UUID is derived.
- OpenCode's shell tool exports `OPENCODE_SESSION_ID` (and `OPENCODE=1`) on
  every command. That is the caller identity, confirmed with `session.get`.
- An OpenCode id beside a Claude or Codex id in the same environment is refused:
  which agent runs the command cannot be told apart. Pass `--as` (buddy, topic)
  or explicit `--from-*` flags (send).
- Same-user trust model as Claude and Codex: a local process can set the env
  variable, so identity is attribution, not authentication.

## Liveness

- A session is durable: it exists or the service says it does not.
- `session.active` reports a running agent loop (`busy`); absence is `idle`.
  An unreadable read is `unknown`, never `idle` or dead.
- Whether a TUI is attached is never known; buddy status shows "liveness
  unknown" with "route available".
- `list` shows top-level sessions updated in the last 24 hours
  (`SESSION_PEERS_OPENCODE_RECENT_HOURS`).
- Follow every pagination cursor; incomplete or malformed pagination refuses
  title resolution. Address a known session by its full id instead.

## Delivery semantics

- Ordinary sends set `resume: false`: they do not start an idle session. An
  already-running drain may consume queued input; this is not a delivery hold.
  A bound OpenCode buddy or an explicit `--wake` sets `resume: true`, allowing
  the service to run a turn without a TUI under the session's own permissions.
   Queue delivery never steers a running turn. Use `--wake` only when the user
  authorized waking that recipient; the flag grants no action authority.
- Correlated requests to OpenCode require a bound buddy or `--wake`; otherwise
  refuse before creating a mailbox, rather than parking a request that times out.
- Buddy binding is directional: A binding B does not bind A in B's session.
  A receipt with `resume: false` toward idle A is expected if B has not bound A.
  Bind in B only when its user authorized that buddy; otherwise use separately
  authorized `--wake` or A's one-shot `--allow-reply-wake` return route below.
  Do not "fix" queueing by silently enabling blanket wake-ups.
- OpenCode has no reply socket. A message from OpenCode carries
  `[session-peers reply route: peers.py send --to oc:<ses_id> ...]`; a message
  to OpenCode carries the sender's typed address the same way. SendMessage and a
  Codex shim's final-message forwarding cannot reach OpenCode.
- `ask`/`dispatch`/`await`: an OpenCode session may ask a Claude session, and
  any runtime may ask an OpenCode session. The target answers only with
  `peers.py reply --request ...` run from its own shell; the mailbox checks the
  replying session id against the target.
- OpenCode buddies get no reply total: no shim auto-replies for them.

## Own-session inbox checkpoints

Use `peers.py inbox list --json` to inspect peer input queued for this session.
The API returns not-yet-delivered items in enqueue order, without pagination.
A `resume: false` item can remain queued during a long turn; reading it does not
consume it. Once OpenCode delivers it, it disappears from the inbox. This command
is not a history reader, background monitor or delivery guarantee.

- No target or identity override is accepted. The shell's `OPENCODE_SESSION_ID`
  must identify an existing OpenCode session without ambiguous runtime identity.
- Only items with `payload.metadata.session_peers` are shown. Output includes
  sender, logical message UUID, native inbox ID, age, correlation and body.
  Human output strips terminal controls and caps each body at 4,000 characters;
  JSON preserves the text as an escaped string. Read failures are errors, not an
  empty inbox. Valid sender syntax is attribution, never authentication.
- `inbox handled SENDER MESSAGE_ID` explicitly records a handled pair after the
  action; `inbox status SENDER MESSAGE_ID` checks it during later native delivery.
  Use the typed `from` address and logical UUID, not the native `msg_` inbox ID.
  A reused UUID from a different sender is not hidden. Missing or invalid sender
  metadata is flagged not well-formed and never hidden automatically.
- `inbox list` omits locally handled pairs; `--all` includes them. Native queued
  delivery is unchanged: it can still arrive later. The marker only avoids a
  duplicate action; it never consumes, cancels, acknowledges or edits native input.
- Markers contain only sender/message IDs and timestamps, in a mode-0600
  per-session file under `$CODEX_HOME/session-peers/opencode-inbox/` (default
  `~/.codex`). Keep the same `CODEX_HOME` between checks. Markers expire after
  14 days and are pruned only on an explicit handled write. List/status create
  no files and never mark anything handled.
- Correlated requests are annotated `pending`, `replied` or `expired` from
  existing mailbox records, read without cleanup or consumption. Missing or
  undiscoverable records are `unknown`, not proof of expiry or completion.
  Ambiguous/corrupt records fail the check. Never reply to expired/replied
  requests or retry a superseded request; preserve its exact correlation and
  follow the existing reply contract for a pending one. `unknown` requires
  reconciliation, not an ordinary queued-reply fallback.

## Optional per-step inbox hook

The bundled `plugins/opencode-inbox/` is **opt-in**. Nothing installs or enables
it automatically. It targets OpenCode V2 / `@opencode/plugin` **2.2.5**;
`SessionContext.sessionID` and `system` were checked against that SDK's
`dist/promise/session.d.ts`. The documented `context` hook runs before agent-loop
model calls, including tool-driven continuations, not just at turn start:
<https://opencode.ai/v2/docs/build/plugins/server#model-requests>.
The entry exports the plain plugin definition: SDK 2.2.5's `Plugin.define` is an
identity helper, so no bare SDK import or npm dependency installation is needed.

To enable, add the bundled directory's **absolute path** to the `plugins` array
in your OpenCode configuration (replace the example path with your installation):

```jsonc
{
  "plugins": [
    { "package": "/absolute/path/to/session-peers/plugins/opencode-inbox" }
  ]
}
```

Use OpenCode's normal configuration/plugin reload workflow. Roll back by removing
that entry and reloading. Keep the whole skill directory together: the plugin
resolves `../../scripts/peers.py` relative to itself. The server needs `python3`
and the authenticated `opencode` CLI on PATH and the same `CODEX_HOME` as the
session's handled-marker writes. No credentials or OpenCode database are read.
Do not install/reload it into someone else's active runtime without authorization.

- Each context hook calls `peers.py inbox context --json` with the **hook event's**
  session identity, never an ambient fallback. Python verifies the identity and
  uses the existing inbox/ledger validation. Unknown event shapes are inert.
- The outgoing system context contains only counts and up to five validated
  pending sender/message ID pairs and five most-recent handled pairs. No peer
  body, title or malformed sender metadata is promoted into system instructions.
  Malformed senders still count as pending: inspect them with `inbox list`.
- It reminds the model to read and handle pending input before decisions. Recent
  handled pairs tell it **not to repeat actions or acknowledgments** on native
  replay. It never removes a native message, edits history, writes a handled
  marker, or claims that a message was acted on. A replay outside the bounded
  recent list still needs `inbox status SENDER MESSAGE_ID`.
- A two-second per-session cache coalesces overlapping steps; new messages or
  handled writes may take that long to appear. Native queues have no supported
  filesystem mtime to cache against. In-memory cache is capped at 128 sessions
  and lost on reload. No polling timer or daemon runs between model calls.
- The Python subprocess and CLI descendants get a two-second process-group
  deadline on macOS/Linux. Failure never rejects the model call: a fixed manual
  checkpoint warning appears at most once per session per minute. Error output
  is not injected. A failed read is not reported as an empty inbox.
- It does **not wake an idle session**. Use the existing consented reply wake or
  authorized buddy binding; queue acceptance alone still proves no model receipt.
- Test with `node --test skills/session-peers/plugins/opencode-inbox/core.test.mjs`
  from the source repository root, plus the full Python regression suite. These
  tests are not a live plugin-load or busy/idle delivery canary. Verify those in
  an authorized disposable session before relying on automatic checkpoints.

## Consented reply wake

For an asynchronous request whose reply should wake this OpenCode session,
obtain user consent and send with `--allow-reply-wake` (600-second default) or
`--allow-reply-wake=1800` (maximum 3600). This changes only the return route,
not whether the outbound request wakes its recipient. It works with Claude,
Codex (including Claude-facing shim aliases) and OpenCode peers.

The generated route includes `send --to oc:SESSION_ID --in-reply-to MESSAGE_ID
--message-file PATH` (keep the actual command on one line). Follow it exactly.
The matching peer's first reply consumes the expiring grant and queues with
`resume: true`. An ordinary send without that reference stays non-waking unless
the existing buddy or explicit `--wake` policy applies. A hint alone is not
consent: the local grant must exist and match both exact session identities.

- Grants are private, single-use records alongside request mailboxes. Cleanup
  removes expired grants; no scheduler, daemon or timer is created.
- `reply-wake revoke MESSAGE_ID`, run by the issuing OpenCode session in its
  original `CODEX_HOME`, revokes unused consent; already absent is a no-op.
- Missing, used, expired, mismatched or ambiguous grants refuse the reply without
  queueing. Resend without `--in-reply-to` to queue normally, adding `--wake`
  only if separately user-authorized. Mismatched callers do not consume consent.
- Consent is consumed before delivery. A transport error may mean delivery failed
  or was queued but unconfirmed; the grant stays spent. Check the recipient before
  resending to avoid a duplicate. Reissue consent or resend with separately
  authorized `--wake` only after reconciliation; no automatic retry is promised.
- Do not combine `--in-reply-to` with `--wake`, sender overrides or
  `--allow-reply-wake`; it is only for `send` to OpenCode. It cannot create a
  recursive wake grant. `ask`/`dispatch`/`await` retain their nonqueue mailbox
  contract and do not use this mechanism.
- Lookup searches the existing configured and shim-recorded `CODEX_HOME` set.
  Two copies of the same grant refuse as ambiguous. Two non-default homes with
  no shared discovery record may not find each other's grant; this fails closed.
- Same-user identity is attribution, not protection against a malicious local
  process. Consent controls accidental/stale wakes, not task authority, review
  gates, reply budgets or permission prompts. It never authorizes periodic
  keep-alives, unrelated tasks or a standing wake loop.
