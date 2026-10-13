# Needs-you reminder

While coordinating, arm one session-only recurring reminder every 20 minutes by
default through the harness's scheduled-prompt/loop facility. Use
`/skipper needs-you` as its report-only prompt; never arm from a timer callback.
Record the timer ID in private notes, reconcile existing timers after compaction
and avoid duplicates. Stop it when coordination ends or the user stops reminders.
Report the supported cadence and its availability limits.

For each pass, read the ledger and coordination notes; refresh read-only
operational state when needed. Restate every unresolved item needing the user,
even previously announced or after the lead finished its work:

- Confirmed missing buddies without a user waiver, with the work held because of it.
- Pending decisions/questions, including already asked ones whose answer is still owed.
- Safe-to-close sessions with clear local end-of-session verdicts.
- Leads past the handover threshold, with context, successor status and the user action owed.
- New unenrolled sessions and known local permission blockers, naming the owning session.

Put missing-buddy blockers first. Record explicit waivers per lead UUID with the
user-message reference and scope; infer none from absent metadata and transfer
none to a successor. Unknown binding or held work stays unverified. A waiver
silences only the repeated missing-buddy report. Irreversible-action holds still
need the user's separate exception in that lead. Make no
technical decision, repair or new grant during this report. Repeat unresolved
items, not resolved decisions; name the action still needed and invent no holds.

When nothing needs the user, print one line: `Nothing needs you right now.` Keep
the reminder while coordinating. This differs from patrol's change-only reporting
and from `/status-check`, a related skill when installed that reports broader
current-session work and stops when no open work remains.

A host without session scheduling cannot promise recurring reports. Say the
reminder is unarmed and report on manual status requests; start no daemon or
shell sleep loop. Omitting Patrol disables patrol timers, not this reminder.
