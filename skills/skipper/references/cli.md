# CLI and state

Invoke `<this skill's directory>/scripts/skipper` directly. Put global options
before the subcommand. Use a private state directory outside every repository:
`${XDG_STATE_HOME:-$HOME/.local/state}/skipper/`. Use `--state-dir PATH` for an
independent skipper ledger and `--owner SESSION_UUID` for every mutation.
Refuse state paths with a `.git` entry in the directory or any ancestor. This
includes a home directory tracked as a dotfiles repository. Use `--state-dir`
with a private directory you own outside that repository, for example
`--state-dir /absolute/private/non-repo/skipper`; do not bypass the parent check.

## Observation

```sh
<this skill's directory>/scripts/skipper observe
<this skill's directory>/scripts/skipper --owner SESSION_UUID status --save
<this skill's directory>/scripts/skipper status --json
<this skill's directory>/scripts/skipper status --all --verbose
<this skill's directory>/scripts/skipper resolve NAME --runtime claude --json
```

`observe` and ordinary `status` are read-only. `status --save` persists the
baseline for the next new/vanished diff. Use it for routine refreshes. Discovery
reads Claude's session registry and only assistant usage metadata from matching
transcripts across projects. Check PID, PID domain and process start time;
preserve failed probes as unverified. Session-peers supplies optional Codex
discovery; its failure cannot prove Codex disappearance. The CLI runs no sends.

`status --json` and `observe` return an observation snapshot. `sessions` is a
mapping keyed by `claude:UUID` or `codex:UUID`; it contains each observed session
once. Read `.sessions[]`, not a recursive object sweep. Each row has `id`,
`session_id`, `name`, `runtime`, `liveness`, observed `state`, `cwd`,
`repo: {root, remote}` and `context`. `repo` is an object; use `.repo.root` for
the path. This is observation state, not the ledger's task/role report.

`new`, `vanished` and `unverified_missing` are ID arrays. `changes` carries
name/repo/runtime labels for those diffs, including absent sessions; those labels
are not additional roster rows. `complete` and `warnings` describe coverage.
Different sessions can share a name; never deduplicate identities by name.
For example, `jq '.sessions[] | {name, id, state, repo: .repo.root}'` reads the roster.

`resolve NAME` prints its bare UUID for enrollment/grants; `--json` returns its observation
row. Match exact names or UUIDs, never prefixes. Ambiguous, absent or unverified
matches fail with exit 2; use `--runtime claude|codex` or a qualified UUID to
disambiguate. Resolving an identity grants no coordinator role, buddy binding
or enrollment authority.
Use the row's qualified `id` for ledger targets and `session_id` for a bare UUID.
Resolution uses the observed roster; inspect coverage before assuming it is exhaustive.

Default tables show Claude leads with short repo names and an explicit buddy
column. Store a pair on a session/enrollment record as `buddy: {id: "codex:UUID",
name: "buddy-name"}`; resolve the displayed name by that ID, never by a name match.
Keep UUIDs in JSON; `--verbose` shows them and full repo paths. `--all` adds
Codex rows and internal reviewers. Footers use names and short repo labels and
follow the table's runtime filter. Preserve Codex state from direct peer fields,
shim status entries or the registry instead of overwriting it with unknown.

Exclude unattached/unregistered internal reviewer rows by default. Use a parent
peer alias plus shared holder PID when available; for Guardian review rows use
the shared holder with an attached parent, falling back to the title when no
parent relationship is available. A shared PID alone never excludes a thread.
Retain explicitly registered or shim-attached threads. Record exclusion reasons
in JSON; `observe --all` includes those rows for inspection. Exclusions are not
vanished-session events.

Use `--home`, `--claude-dir`, `--export-dir` and `--peers` to override discovery
paths. Honor `CLAUDE_CONFIG_DIR` and `CC_STATUSLINE_CTX_EXPORT_DIR`. Execute only
a trusted session-peers installation. Runtime registries are vendor internals;
report unknown schema or liveness without claiming compatibility.
Peer invocation failures report the selected script and the underlying error.
Inspect that diagnostic before changing paths; retry process/socket-denied probes
with authorized host permission. Status tables show Codex rows only with `--all`; the buddy column
shows only explicitly recorded pairs. Missing discovery never authorizes a guess.

For Claude context, accept cc-statusline schema v1 only for the matching session
ID, `source` native/tokens, `stale: false`, integer percentage 0..100 and publication
age from zero inclusive to 180 seconds exclusive. Ignore extra fields. Export
files never establish liveness. Record runtime version and model when available.

Otherwise sum the latest non-synthetic assistant's input, cache-read and
cache-creation tokens. Require all three nonnegative finite fields. Select the
window from a valid native/tokens export for the same serving model published
within 24 hours, then `SKIPPER_DEFAULT_CONTEXT_WINDOW` (positive integer), then
200000, the conservative built-in fallback window. It errs high when the actual
window is larger; the learned and configured windows still take precedence.
Record `learned-export`, `configured` or `builtin` window provenance.
Always label transcript values `est`; missing/invalid usage means `unknown`.
Retain age in JSON; a fresh export publication does not mean a fresh API response.
Show percentage only in tables and `ctx est` beside the state. Show `?` for unknown
or Codex context. Never recommend a Codex handover from context.
Display estimates above 100% as `>100%`; retain the raw percentage in JSON.

## Records

```sh
<this skill's directory>/scripts/skipper --owner SESSION_UUID record --file /absolute/private/record.json
```

Pass one JSON object; use `--file -` for stdin. Supply a stable `id` to update a
record; omission creates a UUID. An update may omit `kind` and retain the existing
record kind. Record evidence references, not complete
transcripts, logs, code or credentials. The CLI records reported authorization;
it cannot authenticate that a grant really came from the user. The receiving
lead must establish that boundary. Session targets use `claude:UUID` or `codex:UUID`.

| kind | Required fields | Lifecycle and evidence |
|---|---|---|
| enrollment | target, skipper, scope | requested → authorized (`user_grant_ref`) → acked (`ack_ref`) → revoked; identities/scope immutable |
| command | target, task | queued → accepted (`ack_ref`) → done (`evidence`); accepted ↔ blocked; queued/accepted/blocked → superseded; irreversible requires reason |
| question | target, question, options, pair_recommendation | open → answered (`answer`, `user_answer_ref`) → acked (`ack_ref`); retain dissent, urgency, dependencies |
| handover | predecessor, successor, task | requested → ready (`handover_ref`) → acked (`successor_ack_ref`) → transferred (`predecessor_stop_ref`, enrolled successor) |
| session | target, state, report_ref; task unless pending-handover | Latest transition report, optionally buddy, blockers, next and incident references |

A successor may record `state: "pending-handover"` with no task yet. This is an
acknowledgment of enrollment/readiness, not task ownership. Require a real task
when recording an assigned-work state; complete the normal handover gates.
Tasks in session reports are display only, never ownership. A pending-handover
update refuses a supplied task and drops any task stored on the prior report.

Use a stable task ID in `task`; keep assignment detail in an optional `prompt`.
New commands require acknowledged enrollment and one accountable lead per active
task. A transferred handover supersedes the predecessor's pending commands;
create a new command ID for the successor. Changes to command intent require
superseding it and creating another ID. State recording does not replace buddy
review, approval gates, transport acknowledgments or the lead's permission policy.
Revocation does not prevent recording an existing command's outcome or superseding
it. Supersede the old command before assigning that task to another enrolled lead;
recording an update grants no permission to continue work after revocation.
After revocation, allow existing-command updates only to `superseded`, `done`
or `blocked`, within the ordinary transition rules. Refuse `accepted` updates.

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
Returning to requested is not a release: the hold stays in force while its
acknowledgments and coverage are reconciled. Resume only after explicit release.
Release explicitly with a reason, including when
cancelling a requested barrier. A released ID cannot be reused. No expiration,
automatic release, broadcast or forge-side enforcement occurs.

## Persistence and recovery

Keep one skipper owner per ledger. Mutations take a nonblocking advisory file
lock, validate state, then fsync a private temporary file and atomically replace
`state.json`. Keep the latest 200 metadata events in the same snapshot. Retain
records and unreleased holds; no automatic pruning erases pending work. Keep
the state directory at mode 0700; mutations tighten an existing directory with
group/other permissions before reading or writing state. A failure to tighten
it fails closed. Invalid JSON/schema and wrong owners fail closed with exit 2.
Success is exit 0.

For a successor skipper, obtain fresh user grants and reconcile commands,
questions and holds with the leads before creating its own ledger. Retain the
old ledger until recovery is complete. Do not silently rewrite its owner.
