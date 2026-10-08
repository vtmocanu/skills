# Timer mechanics

## Claude Code

Use `CronList`, `CronCreate`, `CronDelete`.

1. `CronList` first. If a job with prompt `/away-mode tick` exists, keep it and arm nothing new. To change the interval, delete it first, then arm.
2. Pick an interval N that divides 60 (10, 15, 20, 30). Pick an offset `o` from 1 to N-1 that avoids minutes 0 and 30, then list `o, o+N, …` below 60. Example for 20 minutes: `7,27,47 * * * *`. Cron cannot express intervals that do not divide 60; say so instead of approximating silently.
3. `CronCreate` with that cron, `recurring: true`, prompt `/away-mode tick`.
4. Tell the user in one line: the interval, that ticks fire only while the session is running, idle, and the machine awake, that a recurring tick may fire up to half the interval late, and that the job stops firing when the session exits, is restored on `--resume`/`--continue`, and expires after 7 days.

Missed fires do not catch up: a tick due while the session is busy fires once when it goes idle. Backgrounding the session keeps its scheduled tasks running without a terminal. Session tasks are a convenience, not durable scheduling; for work that must run without an open session, the host's durable schedulers (cloud routines, desktop scheduled tasks, CI schedules) are the alternative.

Background watchers (a poller per item) complement the timer: they notify on a state change between ticks. A tick re-arms any that died.

## Other hosts

Without a scheduled-prompt tool there is no wakeup. An external trigger the user controls (a system scheduler that sends `/away-mode tick` into the session) works if the host accepts injected prompts; verify it delivers once before relying on it.
