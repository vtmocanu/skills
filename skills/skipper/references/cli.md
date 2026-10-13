# CLI and state

Invoke `<this skill's directory>/scripts/skipper` directly. Put global options
before the subcommand. Use a private state directory outside every repository:
`${XDG_STATE_HOME:-$HOME/.local/state}/skipper/`. Use `--state-dir PATH` for an
independent skipper ledger and `--owner SESSION_UUID` for every mutation.

## Observation

```sh
<this skill's directory>/scripts/skipper observe
<this skill's directory>/scripts/skipper --owner SESSION_UUID status --save
<this skill's directory>/scripts/skipper status --json
```

`observe` and ordinary `status` are read-only. `status --save` persists the
baseline for the next new/vanished diff. Use it for routine refreshes. Discovery
reads Claude's session registry and only assistant usage metadata from matching
transcripts across projects. Check PID, PID domain and process start time;
preserve failed probes as unverified. Session-peers supplies optional Codex
discovery; its failure cannot prove Codex disappearance. The CLI runs no sends.

Use `--home`, `--claude-dir`, `--export-dir` and `--peers` to override discovery
paths. Honor `CLAUDE_CONFIG_DIR` and `CC_STATUSLINE_CTX_EXPORT_DIR`. Execute only
a trusted session-peers installation. Runtime registries are vendor internals;
report unknown schema or liveness without claiming compatibility.

For Claude context, accept cc-statusline schema v1 only for the matching session
ID, `source` native/tokens, `stale: false`, integer percentage 0..100 and publication
age from zero inclusive to 180 seconds exclusive. Ignore extra fields. Export
files never establish liveness. Record runtime version and model when available.

Otherwise sum the latest non-synthetic assistant's input, cache-read and
cache-creation tokens. Require all three nonnegative finite fields. Select the
window from a valid native/tokens export for the same serving model published
within 24 hours, then `SKIPPER_DEFAULT_CONTEXT_WINDOW` (positive integer), then
1000000. Record `learned-export`, `configured` or `builtin` window provenance.
Always label transcript values `est`; missing/invalid usage means `unknown`.
Retain age in JSON; a fresh export publication does not mean a fresh API response.
Show percentage only in tables and `ctx est` beside the state. Show `?` for unknown
or Codex context. Never recommend a Codex handover from context.

## Records

```sh
<this skill's directory>/scripts/skipper --owner SESSION_UUID record --file /absolute/private/record.json
```

Pass one JSON object; use `--file -` for stdin. Supply a stable `id` to update a
record; omission creates a UUID. Record evidence references, not complete
transcripts, logs, code or credentials. The CLI records reported authorization;
it cannot authenticate that a grant really came from the user. The receiving
lead must establish that boundary. Session targets use `claude:UUID` or `codex:UUID`.

| kind | Required fields | Lifecycle and evidence |
|---|---|---|
| enrollment | target, skipper, scope | requested → authorized (`user_grant_ref`) → acked (`ack_ref`) → revoked; identities/scope immutable |
| command | target, task | queued → accepted (`ack_ref`) → done (`evidence`); accepted ↔ blocked; queued/accepted/blocked → superseded; irreversible requires reason |
| question | target, question, options, pair_recommendation | open → answered (`answer`, `user_answer_ref`) → acked (`ack_ref`); retain dissent, urgency, dependencies |
| handover | predecessor, successor, task | requested → ready (`handover_ref`) → acked (`successor_ack_ref`) → transferred (`predecessor_stop_ref`, enrolled successor) |
| session | target, task, state, report_ref | Latest transition report, optionally buddy, blockers, next and incident references |

Use a stable task ID in `task`; keep assignment detail in an optional `prompt`.
Commands require acknowledged enrollment and one accountable lead per active
task. A transferred handover supersedes the predecessor's pending commands;
create a new command ID for the successor. Changes to command intent require
superseding it and creating another ID. State recording does not replace buddy
review, approval gates, transport acknowledgments or the lead's permission policy.

## Barriers

```sh
<this skill's directory>/scripts/skipper --owner SESSION_UUID barrier request --id HOLD_ID --resource example-repo#main --scope merges --participants claude:LEAD_UUID --reason 'Release checkpoint'
<this skill's directory>/scripts/skipper --owner SESSION_UUID barrier ack --id HOLD_ID --participant claude:LEAD_UUID --inflight '[]' --ack-ref MESSAGE_ID
<this skill's directory>/scripts/skipper --owner SESSION_UUID barrier active --id HOLD_ID
<this skill's directory>/scripts/skipper --owner SESSION_UUID barrier release --id HOLD_ID --reason 'Release finished'
```

Use scopes `merges`, `pushes-ci` or `dispatches`. Request with unique enrolled
participants. An acknowledgment must name remaining in-flight work as a JSON
array; an empty array explicitly means quiescent. Activate only after all
participants acknowledge with no work remaining. A later nonempty acknowledgment
returns the hold to requested, as does revoking a participant's enrollment.
Release explicitly with a reason, including when
cancelling a requested barrier. A released ID cannot be reused. No expiration,
automatic release, broadcast or forge-side enforcement occurs.

## Persistence and recovery

Keep one skipper owner per ledger. Mutations take a nonblocking advisory file
lock, validate state, then fsync a private temporary file and atomically replace
`state.json`. Keep the latest 200 metadata events in the same snapshot. Retain
records and unreleased holds; no automatic pruning erases pending work. Invalid
JSON/schema and wrong owners fail closed with exit 2. Success is exit 0.

For a successor skipper, obtain fresh user grants and reconcile commands,
questions and holds with the leads before creating its own ledger. Retain the
old ledger until recovery is complete. Do not silently rewrite its owner.
