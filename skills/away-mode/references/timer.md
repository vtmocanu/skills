# Timer mechanics

## Claude Code

Use `CronList`, `CronCreate`, `CronDelete`. `CronList` freely to discover existing coverage; `CronCreate` only for an uncovered item or deadline (SKILL.md Timer); `CronDelete` only the grant's own timer: when nothing it covers would be left uncovered, to replace it (step 1), or when the grant ends (Return, `off`, Closing).

1. `CronList` first. An existing job counts as a wake source only if it fires often enough and its prompt actually runs a watch pass over the away items; name it in the wake plan and arm nothing for what it covers. If a job with prompt `/away-mode tick` exists, keep it and arm nothing new. To change the interval, delete it, arm the replacement, and confirm it in `CronList` before recording its coverage.
2. Pick an interval N that divides 60 (10, 15, 20, 30). Pick an offset `o` from 1 to N-1 that avoids minutes 0 and 30, then list `o, o+N, …` below 60. Example for 20 minutes: `7,27,47 * * * *`. Cron cannot express intervals that do not divide 60; say so instead of approximating silently.
3. `CronCreate` with that cron, `recurring: true`, prompt `/away-mode tick`. For a deadline-only gap, a one-shot instead: pin minute, hour, day and month to the deadline (local time), `recurring: false`.
4. Tell the user in one line: the interval, that ticks fire only while the session is running, idle, and the machine awake, that a recurring tick may fire up to half the interval late, and that the job stops firing when the session exits, is restored on `--resume`/`--continue`, and expires after 7 days.

Missed fires do not catch up: a tick due while the session is busy fires once when it goes idle. Backgrounding the session keeps its scheduled tasks running without a terminal. Session tasks are a convenience, not durable scheduling; for work that must run without an open session, the host's durable schedulers (cloud routines, desktop scheduled tasks, CI schedules) are the alternative.

Background watchers (a poller per item) notify on a state change between passes, but cannot report their own silent death; they cover an item only together with another bounded wake (SKILL.md Timer). Each pass re-arms any that died.

## Other hosts

Without a scheduled-prompt tool there is no periodic or deadline wakeup (watcher exits and peer messages still wake the session). An external trigger the user controls (a system scheduler that sends `/away-mode tick` into the session) works if the host accepts injected prompts; verify it delivers once before relying on it.
