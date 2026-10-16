# Needs-you reminder

While coordinating, use one session-only reminder every 20 minutes by default.
Load this reference once; schedule a plain-text prompt such as `Check needs-you
changes from the ledger and reminder notes; apply the loaded reminder policy.`
Never use a slash command or reload the skill on fire. Treat `/skipper needs-you`
as a manual report-only pass; never arm from a callback. Record timer IDs,
last-reported items and snooze/quiet-hour settings in private session notes.
Reconcile existing timers after compaction; avoid duplicates and replay no missed ticks.

Pause reminders while the user is away, during quiet hours or an explicit snooze.
At setup, confirm the user's timezone and quiet hours; offer 22:00-08:00 in that
zone when none are configured. Record absolute snooze end times. Remove the
recurring reminder during a pause; use scheduling outside the quiet window or
one session-only resume timer when supported. Otherwise resume on user return
or a manual request and report that limitation. A delayed callback checks the
pause before reporting and never catches up missed reports. On return/resume,
report outstanding items once and restart the reminder without duplicates.
Keep patrol and incident monitoring running; route urgent incidents immediately.
Pausing reminders changes no grant, hold, acknowledgment or escalation gate.

Read the ledger and coordination notes first. Use
`<this skill's directory>/scripts/skipper status --cached` only when a roster
view is needed; preserve its age and unknown coverage. Refresh live observations
on patrol, on explicit refresh requests or before consequential decisions, not
on every reminder. Never infer completion or current absence from a cached row.

Track unresolved user items by stable ID, action owed and material state:

- Confirmed missing buddies without a user waiver, with the work held.
- Pending decisions/questions whose answer is still owed.
- Safe-to-close sessions with clear local end-of-session verdicts.
- Leads past the handover threshold, successor status and the user action owed.
- New unenrolled sessions and known local permission blockers, with their owner.

Report new, materially changed or resolved items only, except confirmed missing-
buddy blockers: repeat those every reminder interval outside pauses until bound
or explicitly waived, even with Patrol off. If nothing changed and no such
repeat is due, produce no user-facing output. Keep open
items in the ledger; silence never resolves them. Compare action/state, not
changing timestamps or observation ages. Include the complete outstanding list
on explicit user status requests and once on return/resume. Put missing-buddy
blockers first whenever a report is due. Confirm their absence and held work
with the lead; missing metadata stays unknown. Record waivers per lead UUID with
the user-message reference and scope; transfer none to successors. A waiver only
silences the repeated missing-buddy report; irreversible actions still need the user's
separate exception in that lead. Make no technical decision, repair or grant.

Keep this timer separate from patrol and from `/status-check`, when installed,
which reports broader current-session work. Use neither as a cache keep-alive.
Stop reminders when coordination ends or the user stops them. Without session
scheduling, say reminders are unarmed and report manually; create no daemon,
OS cron or shell sleep loop. Omitting Patrol disables patrol timers only.
