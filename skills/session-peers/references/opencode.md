# OpenCode V2 sessions

Use the V2 API through the running service's authenticated CLI.

## Transport

- Every call runs `opencode api <operationId>`, the CLI's authenticated client
  for the background service. No auth, state or database file is read.
- Read only: `session.get`, `session.list` (`parentID=null`), `session.active`.
  Never `session.message.*`, `export` or `log`: no conversation bodies.
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
- OpenCode has no reply socket. A message from OpenCode carries
  `[session-peers reply route: peers.py send --to oc:<ses_id> ...]`; a message
  to OpenCode carries the sender's typed address the same way. SendMessage and a
  Codex shim's final-message forwarding cannot reach OpenCode.
- `ask`/`dispatch`/`await`: an OpenCode session may ask a Claude session, and
  any runtime may ask an OpenCode session. The target answers only with
  `peers.py reply --request ...` run from its own shell; the mailbox checks the
  replying session id against the target.
- OpenCode buddies get no reply total: no shim auto-replies for them.

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
