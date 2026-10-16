# Patrol

Enable patrol only while coordinating and only for repositories with an optional
Patrol profile section. Use its positive interval and listed operational checks.
Other bullets, such as Scope, narrow those checks; they add no checks or authority.
Without that section, arm no patrol timer. Ask about incomplete or invalid configuration.

## Session timer

- Use the harness's session-only timer or loop facility, such as a Claude Code
  session cron/loop. Group repositories sharing an interval; retain other
  intervals separately. Run built-in checks once per due round, plus checks for
  the due profiles. Keep rounds serial and prevent duplicate timers.
- Record timer IDs and due profiles in private session notes. Reconcile existing
  timers after compaction; do not assume a recorded timer still runs. Remove or
  update timers when profiles change, coordination ends or the user stops patrol.
- If no session timer facility is available, report that patrol is unarmed and
  use manual refreshes. Create no background daemon, shell sleep loop, OS cron,
  remote schedule or detached agent to replace the missing facility.

## Each round

1. Reread due profiles; disarm timers whose Patrol section was removed. Read the
   previous saved observation before refreshing. Run
   `<this skill's directory>/scripts/skipper --owner SESSION_UUID status --json --save`
   with your own UUID; reuse its discovery, context readings and new/vanished diff.
2. Check new, vanished and unverified-missing sessions. Preserve unknown liveness
   as unknown; request enrollment rather than asserting authority over a new lead.
   Also check confirmed missing bound buddies without an applicable per-lead user
   waiver. Repeat these user blockers first on every round, naming held work,
   even unchanged outside needs-you pauses; during away, snooze or quiet hours,
   retain these nonurgent reminders for return/resume. Keep checking and route
   urgent incidents immediately. Confirm with the lead; blank metadata or discovery gaps are unknown.
3. Check Claude leads against their profile's handover threshold. Recommend a
   safe checkpoint on a new crossing; leave Codex buddies to compact.
4. Compare successive Claude readings only when model, window, estimate provenance
   and reading quality are comparable. An abrupt context drop suggests compaction;
   ask the lead to confirm its task ownership, outstanding gates and next step.
   Do not declare compaction from a percentage alone or from a changed window,
   model, stale reading or source. Wait for confirmation before changing ownership.
5. Run due profile checks as read-only operational observations. Preserve failed
   or incomplete lookups as unknown; missing visibility never proves an orphan,
   duplicate, exhausted capacity or clean state. Make no repair, run dispatch, claim,
   merge, cancellation, schedule deletion or code investigation during patrol.
6. Route each new or materially changed finding to its accountable lead/buddy pair;
   assign read-only investigation of an unowned finding to an enrolled lead pair
   with capacity, including buddy reply allowance to spare (prefer the pair
   holding the evidence), without asking the user. Report its verdict once;
   if user action is needed, keep it as a needs-you item until resolved.
   If none has capacity, report it as unassigned.
   Include evidence references and the owning
   resource. Escalate user decisions with the pair's recommendation; keep technical
   diagnosis and remediation with that pair.

Track finding identity, evidence freshness, disposition and owner in private
notes alongside the ledger. Except for recurring missing-buddy blockers, report
new, changed and resolved findings once; keep unchanged findings quiet. Re-alert
on a material change, not on every timer tick. A round with no changed findings
and no due missing-buddy reminders produces no user-facing report. Report loss of
observation or timer coverage once and its recovery once; do not call an
unreadable check clean.
