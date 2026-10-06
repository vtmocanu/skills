---
name: status-check
description: Reports what work is still open in the current session (running background jobs and subagents, items waiting on the user, on another session, on CI or a bot, held items, and what finished since the last check) as one table, then arms a session-only timer that repeats the report every 20 minutes until nothing is left. Read-only; it never merges, reruns, or edits. Use when the user runs /status-check, asks what is left, what is still open, or what you are waiting on, or wants periodic progress reports. Triggers include "/status-check", "what is left on your plate", "what's still open", "status check", "remind me what's pending".
---

# Status check

Report the open work in this session, then keep reporting on a timer. Report only: never merge, push, rerun, retry, edit, or message anyone while running this skill.

## Arguments

| Invocation | Do |
|---|---|
| `/status-check` | Report, then arm the timer (default every 20 minutes) |
| `/status-check every N` | Report, then arm the timer every N minutes (N divides 60: 5, 10, 15, 20, 30, 60) |
| `/status-check tick` | Report only. The timer sends this; never arm from it |
| `/status-check stop` | Delete the timer and confirm |

## The report

Build it from this session's own state. Gather, cheapest first:

1. **Background work**: every background shell job, subagent, monitor, and scheduled job still running, except this skill's own `/status-check tick` timer. For each, the condition it waits on and the log or output it writes. Read a job's latest output line when the state is not already known; never wait on a job.
2. **Durable trackers**, when they exist: the session's todo or task list, and read-only state the session already uses (a claims board, a trail file). Read them; never run a repository-provided script for this report.
3. **The conversation**: items handed to the user, items blocked on another session, on CI, on a review bot, on a remote run, on the network, items deliberately held with what unblocks them, and items finished since the last report.

Print one table, open items only, most actionable first:

| Item | State | Next action | Waiting on |
|---|---|---|---|

- `Waiting on` is one of: you (the user), me, a named session, CI, a named bot, a named run, network, or a time.
- Put items that need the user first and mark them **needs you**.
- Under the table, one line: `Done since last check:` with the items finished since the previous report, or `none`.
- Name what you could not verify (an unreadable log, a lookup that failed, state that may predate a context compaction) instead of guessing.

On a `tick`, when nothing changed since the previous report, print exactly one line instead: `No change since HH:MM. N open, M need you.`

## The timer

Use the scheduled-prompt tools (`CronList`, `CronCreate`, `CronDelete` in Claude Code). Before arming:

1. `CronList`. If a job with prompt `/status-check tick` exists, keep it; arm nothing new. On a new interval, delete it first, then arm.
2. Pick the cron minutes: an offset `o` from 1 to N-1 that avoids minutes 0 and 30, then `o, o+N, …` below 60. Example for 20 minutes: `7,27,47 * * * *`.
3. `CronCreate` with that cron, `recurring: true`, prompt `/status-check tick`.
4. Tell the user in one line: the interval, that ticks fire only while the session is idle and may run late, that the timer belongs to this session and expires after 7 days, and that `/status-check stop` ends it.

**Stop on its own.** When a report finds no open items, delete the timer (or skip arming it) and say the session has nothing left to track. The timer itself never counts as open work.

Without scheduled-prompt tools (Codex, other hosts), print the report once and say that this host cannot repeat it automatically.
