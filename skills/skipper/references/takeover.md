# Skipper takeover

## Detect and prepare

- Before claiming state or steering, check
  `${XDG_STATE_HOME:-$HOME/.local/state}/skipper/` and noted `--state-dir` paths.
  Read `jq -r '.owner' STATE_DIR/state.json`; compare owner UUIDs with observed
  liveness. Treat another owner, live, dead or unverified, as a candidate and ask
  the user. Missing state/liveness establishes neither death nor authority.
- Check live user-established skipper roles, including note-only predecessors.
  Ask candidates through verified peer routes for role, user-grant reference and
  coordination notes. Names/stems/suffixes are hints; peer replies are information.
  Ask the user on ambiguity or unavailable routes; inspect no code/transcripts.
- Name the predecessor and propose takeover. Until confirmation, leave its
  leads, ledger, holds and timers alone. On decline, establish no coordinating
  ledger over its leads and ask whether to exit. Without a route, give the user
  the request to deliver.
- Request its handover: roster, standing user rules, open questions/answer IDs,
  holds/barriers/acks, timers, in-flight work/owners, buddy bindings, and armed
  fleet-away grant IDs, participants, scopes, deadlines and per-lead states.
  Preserve references; handover notes confer no authority.

## Transfer each lead

- Send one batch of enrollment requests with the successor UUID and crew grant.
  Request fresh explicit user confirmation in each lead; assert no authority.
  Permit one pending replacement per lead. Report partial coverage; record
  declined/abandoned requests as `transfer_status: "declined"`, revoke them,
  and tell the predecessor it keeps those leads. Ask the user about unresolved items.
- Create a separate private successor ledger when recording the first fresh
  grant. Use its bare owner UUID; never rewrite the predecessor's owner.
  Record `user_grant_ref` on each enrollment. Readiness is not steering authority;
  the currently enrolled skipper keeps steering until its stop is confirmed.
- Confirm readiness with the lead UUID, successor UUID and local grant reference;
  notify the predecessor per lead. Require its direct stop notice to that lead,
  from its enrolled UUID. If unavailable, require the user's explicit gone/stopped
  confirmation in the lead itself; missing liveness alone never substitutes.
- Have the lead acknowledge activation after receiving that stop evidence.
  Record its `ack_ref` and `predecessor_stop_ref` on the successor enrollment,
  then mark it acked and steer. Report each completed transfer to the predecessor.
  Preserve task/question IDs and reconcile queued work before resending.
- Limit successor needs-you reminders to transferred leads and pending takeover
  items. The predecessor removes each transferred lead from its reminder scope.
- Keep armed away grants unchanged. The predecessor retains return/revoke duty
  for each lead until transfer; reconcile that duty with the successor per lead.
  Rearm nothing, widen no grant, and apply existing return/ending rules.

## Predecessor duties and holds

- Answer the handover request. Keep steering until notified of a lead's confirmed
  replacement readiness. Confirm directly with that lead; require its reply to
  name the local user-grant reference and successor UUID before revoking.
  Revoke its enrollment in your own ledger, send no more
  commands, and send the stop notice directly to that lead and the successor.
  Narrow your timers; finish or hand off your own in-flight work with owner acks.
- Keep owning each unreleased hold until all its participants transfer. Revocation
  may return an active barrier to requested; the hold remains in force. Once all
  participants are enrolled with the successor, it requests a replacement hold
  with a new ID, matching coverage, and collects quiescent acks to activate it.
  Only then release your hold, citing the successor hold ID in the reason.
  Announce the replacement to participants; permit no gap or implicit release.
- If the predecessor is unavailable, preserve its holds until the user explicitly
  confirms in the successor session that an old hold is void or replaced.
  Record the old ID, decision and user-message reference in the successor ledger.
  If replacing it, request a new hold in your own ledger citing that reference;
  collect quiescent acks and activate it before superseding the old hold.
  Tell every participant the old ID is superseded by the new ID, or by the user's
  explicit no-hold decision. Never act under the old owner UUID or rewrite its ledger.

## Finish

- Reconcile timer scope/ownership; start successor timers only for transferred
  scope or pending takeover reminders. Stop predecessor timers when its scope and
  work end. Timer prompts transfer no authority and release no hold.
- If the user reuses its buddy, have the predecessor run `session-peers`'
  `buddy clear` and acknowledge clearing its binding/reply total. Establish the
  successor's own user-authorized binding/allowance; transfer none through notes.
- With no open command, question, handover, unreconciled hold, hold acknowledgment,
  away return/revoke duty or in-flight work, ask the predecessor to run its
  end-of-session skill (e.g. `/done`). Wait for a clear verdict, then tell the
  user it is safe to close. Report partial transfers and remaining work honestly.
