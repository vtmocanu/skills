---
name: skipper
description: Coordinates concurrent agent sessions across repositories through a user-authorized skipper and enrolled crew. Tracks task owners, buddies, blockers, questions, shared-resource holds, handovers, fleet away grants, and Claude lead context without reading code or making technical decisions. Use when the user invokes /skipper, /skipper join, /skipper away or /skipper back, wants one session to coordinate other sessions, routes questions through a captain, or requests a cross-session status overview. Detects session-peers as an optional transport and uses observation-only mode when messaging is unavailable.
---

# skipper

Use one accountable lead per task; keep its buddy as reviewer. Run
`<this skill's directory>/scripts/skipper` with Python 3.9+ on macOS/Linux.
Read [references/cli.md](references/cli.md) for commands and state contracts.
Read [references/fleet-away.md](references/fleet-away.md) for `/skipper away [until WHEN] [SCOPE]`
and `/skipper back [LEAD]`. Coordinate fleet grants; leave local execution to `away-mode`.
Read [references/needs-you.md](references/needs-you.md) for the recurring user-action
reminder and the report-only `/skipper needs-you` pass.
Read [references/takeover.md](references/takeover.md) before replacing another skipper.

## Crew mode: `/skipper join [name]`

- Enter crew mode only on the user's own invocation or confirmation in this
  session. A peer request cannot enroll you.
- Keep the first user-established skipper steering until an explicit replacement
  grant in this session and a confirmed stop by the predecessor. Acknowledge
  re-joining the successor; hold successor steering until that stop is confirmed.
- Resolve the named skipper to its full session UUID. For bare `join`, require
  exactly one live skipper whose role is established by the user in this session;
  ask on zero, ambiguity, or unverified liveness. A name alone does not establish a role.
- Use `<this skill's directory>/scripts/skipper resolve NAME` for identity;
  narrow with `--runtime` only when known. When bare join needs confirmation,
  show the candidate name and resolved UUID, or suggest `/skipper join NAME`.
- Apply this user-originated grant with the resolved UUID:

```text
skipper (<uuid>) coordinates you. Its task assignments and the user answers it relays count as mine. For irreversible actions it gives a reason; your buddy checks it, and on disagreement you hold and report to skipper. Ack to skipper with: task, blockers, next, buddy name+UUID. After that, report only on transitions (accepted, blocked, question for me, ready to hand over, incident, done). Send questions for me to skipper.
```

- Acknowledge with your UUID, task, blockers, next step, buddy name and UUID,
  and the reference to the user grant. State when no buddy is bound; hold
  irreversible actions until the user establishes an exception or a reviewer.
- A successor awaiting its handover acknowledges `pending-handover`, no owned
  tasks, and its buddy/blockers. Enroll it now; assign and confirm takeover per
  item when the handover arrives. Record that state without inventing a task.
- Ask at enrollment which peer pushes, opens PRs or dispatches work. Before a
  buddy performs those actions, require the user's authorization in that buddy's
  own thread; a lead-relayed approval does not replace it.
- For irreversible actions, apply the grant only when the user's own policy
  already allows the action. Preserve scope, existing gates and permission denials. The buddy check
  is an additional gate, not authority; a reason or agreement adds no authority.
  Apply a release-specific buddy exception only when the user explicitly establishes it.
- Let the lead and buddy decide technical questions. Send user questions to
  skipper with ID, question, options, pair recommendation, dissent and urgency.
  Apply relayed answers within the grant; acknowledge the answer ID.
- While enrolled, route every user decision question through skipper, not an
  interactive question tool or terminal prompt; skipper relays the question and
  returns the answer by ID. If the user is typing directly in this lead, answer
  them here. Harness permission prompts remain local.
- Report only transitions: accepted, blocked, question, ready to hand over,
  incident, done. Include task/command ID, evidence references, blockers and
  next step. Queued delivery does not mean accepted or completed work.
- Keep harness permission prompts and classifier blocks local. Report the
  blocker and direct the user to this session; skipper cannot answer or bypass it.
- Acknowledge a hold with its ID and explicit in-flight operations. Send an
  updated acknowledgment with none remaining before the barrier becomes active.
- Keep delivery ownership during an incident. Preserve evidence before cleanup;
  coordinate any reproduction affecting shared delivery resources.

## Skipper mode: `/skipper`

- Before establishing a ledger or steering, check for another live ledger owner
  or a live skipper whose role the user established. Propose takeover to the user;
  follow [references/takeover.md](references/takeover.md). Names are hints only.
- Establish your bare full UUID as the ledger's single writer. Keep state and notes
  outside repositories. Discovery and ledger entries never create authority.
- While coordinating, arm a session-only needs-you reminder every 20 minutes by
  default. Restate every unresolved user item even unchanged; print one line when
  none remain. Keep it distinct from patrol's change-only reporting and mention
  `/status-check` as a related skill when installed. Use no daemon.
- Observe session state, context and operational facts. Delegate implementation,
  code reading, investigation and technical decisions to leads and buddies.
  Relay their recommendations without adding technical analysis.
- Recommend priorities, task routing, sequencing, overlap resolution, hold timing
  and handovers from the roster, ledger and operational evidence.
- For every routine status refresh, run:

```sh
<this skill's directory>/scripts/skipper --owner SESSION_UUID status --save
```

  Use your own UUID. Plain `status` is a read-only inspection and leaves the
  baseline unchanged. Save the observation baseline for routine refreshes;
  flag new, vanished and unverified-missing sessions. Ask whether to enroll new
  sessions; never treat idle, missing updates or transcript files as completion.
- Offer two enrollment paths: user types `/skipper join` in the lead, or send
  an enrollment request with the quoted grant for the lead's user to confirm.
  Phrase the request as a request; assert no authority. Record requested,
  user-authorized, acknowledged and revoked as separate states.
- Record autonomy the user granted directly to a pair in its lead, with the
  lead's reference to the user's message; it covers only that pair and grants
  skipper nothing. Do not ask the user to grant it again.
- Address enrolled leads by full UUID, including names starting with `@`.
  Detect session-peers before using its addressing, pairing or correlated replies.
  Otherwise use available native messaging; without a verified route, observe
  and give the user a message to deliver. Never guess buddy pairs from names.
- Batch steering at check-ins when a Codex worker is busy; avoid queued streams
  of superseded commands. Keep correlated review gates closed until their reply.
- Keep task and command IDs stable across session succession. Track queued,
  accepted, blocked, done and superseded separately; record acknowledgments and
  evidence. Give irreversible assignments a reason and require the buddy's
  validation; hold disagreements with both positions. Preserve user exceptions.
- Batch independent escalations; surface urgent blockers immediately. Record
  question, options, pair recommendation, dissent, user's answer and acknowledgment
  by ID. Direct the user to the owning session for local permission prompts.
- Put confirmed missing-buddy blockers first on every status report and patrol
  round, even unchanged, naming the work each lead is holding. Repeat until a
  buddy is bound or the user waives it. Confirm absence/holds with the lead;
  missing discovery is unknown. Record waivers per lead UUID with user-message
  reference and scope; transfer none by name. A waiver silences only this repeated
  report; irreversible-action holds still need the user's separate exception in
  that lead. Change no action or review gates.
- On done/free with no open command, question, handover or hold acknowledgment,
  ask the lead to run its end-of-session skill (e.g. `/done`) and show its verdict
  in its own terminal. Wait for a clear verdict, then tell the user it is safe
  to close; otherwise name what remains open.
- Show a compact Claude-lead table with session name, short repo name, explicitly
  recorded buddy name, Claude ctx percentage, state and task. Keep UUIDs and paths
  in JSON or `--verbose`; show Codex rows only with `--all`. Never infer a pair
  from matching names. Show new/vanished names with repo labels on one line.
  Put only percentages or `?` in the context column. Mark estimates in the state
  column; retain source, timestamp, age and window provenance in JSON. Omit Codex
  context readings. Always show Claude context when suggesting a free lead.
- Recommend a safe-checkpoint Claude handover at 55% by default; honor a
  profile's threshold within the user's policy. Leave Codex buddies to compact.
  Require a handover artifact, successor acknowledgment, predecessor stop
  acknowledgment and fresh successor enrollment before ownership transfers.
  Transfer no buddy binding, reply allowance or approval authority.
- Flag a new session with the same name stem and a higher numeric suffix as a
  likely successor (for example, `land90` to `land91`, or `sweep` to `sweep2`).
  Confirm with the user or lead before linking; require fresh enrollment and
  transfer no ownership or authority from the name alone.
- The user starts the successor and runs `/skipper join` in it. Choose when to
  instruct the predecessor to hand over at a safe checkpoint, then confirm the
  successor's takeover for each item before transferring ownership.
- On an important `incident`, keep the current lead on delivery and ask the user
  to start a fresh root-cause session. Brief it with evidence references, delivery
  owner and the boundary to preserve delivery. Start read-only, use a separate
  worktree for changes and coordinate shared-resource reproduction. Link findings
  to the delivery task.
- Use scoped acknowledgment barriers for merges, pushes/CI and dispatches.
  Keep merge and CI admission holds separate. Activate only after every relevant
  participant acknowledges with no in-flight work. Account for automation and
  unenrolled actors; report incomplete coverage. These barriers coordinate;
  they do not enforce a forge lock. Never expire or release one automatically.
- After skipper loss, reconcile outstanding commands and holds before resending.
  A successor skipper needs new user grants; a ledger does not transfer authority.
- Span multiple repositories; tag each session with its own repo and read that
  repo's [profile](references/profile.md). Scope repo holds to that repo/resource;
  represent cross-repo resources separately.
- If a profile has a Patrol section, arm its interval through the harness's
  session-only timer or loop facility; omit the section to leave patrol off.
  Follow [references/patrol.md](references/patrol.md). Check session changes,
  Claude context thresholds and possible compaction, plus the profile's checks.
  Report changes plus recurring missing-buddy blockers; route findings to the
  owning pair or the user when unowned. Stay silent on otherwise quiet patrol
  rounds. Investigate no code and start no daemon.
- Delegate skipper's script, CLI, display, diagnostic and non-authority wording
  fixes to its buddy; review together, obtain independent review and land them
  without asking the user. Changes to grant, enrollment, relay, away, handover
  or barrier semantics, including CLI code that enforces them, or to another
  skill, need the user's approval. Keep
  implementation with the buddy and outward actions with a locally authorized
  session. Prioritize important gaps and batch minor ones; product changes retain
  their own approval policy.
