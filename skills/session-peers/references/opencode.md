# OpenCode V2 sessions

Verified against OpenCode 2.0.30 (2026-10-17) through the running service's
OpenAPI (`opencode api GET /openapi.json`) and the operations below.

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

## Delivery semantics

- A queued input runs as the session's next turn on the service, with or
  without a TUI, under that session's own permissions. It never steers a
  running turn.
- OpenCode has no reply socket. A message from OpenCode carries
  `[session-peers reply route: peers.py send --to oc:<ses_id> ...]`; a message
  to OpenCode carries the sender's typed address the same way. SendMessage and a
  Codex shim's final-message forwarding cannot reach OpenCode.
- `ask`/`dispatch`/`await`: an OpenCode session may ask a Claude session, and
  any runtime may ask an OpenCode session. The target answers only with
  `peers.py reply --request ...` run from its own shell; the mailbox checks the
  replying session id against the target.
- OpenCode buddies get no reply total: no shim auto-replies for them.
