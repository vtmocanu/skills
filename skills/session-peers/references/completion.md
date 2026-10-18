# Background duties and continuation

Background execution is not a wake subscription. Before reporting a duty as
watched, name the exact owner and the mechanism that will produce its next turn.

| Host | Completion mechanism |
|---|---|
| Claude Code | Native `run_in_background` task notification. Verify the task actually started. |
| OpenCode | Native shell `background: true` completion notification. Verify the actual start; a shell ID alone is not proof of lifetime coverage. |
| Codex | A yielded exec session ID needs active retrieval unless a completion adapter is independently verified. Use the bounded observation below, an explicitly consenting native-capable peer observer, or report manual-only continuation. |

Native callbacks remain scoped to the host/session lifecycle. Do not promise
survival across host death, restart, machine sleep, or permission interruption
without separate evidence. Missing callbacks are unknown, not proof the run died.

## Bounded own-session observation

For an already-authorized read-only poller, Codex/OpenCode can detach one finite
observation from the launching turn:

```bash
<this skill's directory>/scripts/peers.py watch start --task "TASK exact-head-or-plan" --timeout 3600 --wake -- /absolute/path/to/poller ARG
<this skill's directory>/scripts/peers.py watch list --json
<this skill's directory>/scripts/peers.py watch handled WATCH_UUID --next "actual next action or explicit blocker"
```

- Obtain task-scoped user consent before `--wake`. Omitting it creates receipts
  only. Owner identity comes from this shell, not an arbitrary target or override.
  Claude callers are refused with the native `run_in_background` alternative.
- Timeout is required, finite, positive and at most 86400 seconds. Run only
  read-only pollers with their own finite bounds. This is one detached worker,
  not a daemon, scheduler, restart policy, recurring wake or new action grant.
- The launcher returns an invocation receipt, not proof the first poll ran.
  Inspect `watch list` and the private log before reporting STARTED coverage.
- Receipts/logs live under the caller's own `CODEX_HOME/session-peers/watches/`
  in private files. The receipt retains exact owner, task label, argv digest,
  loaded-code digest, deadline, worker PID/start identity, actual exit and result
  hint. Do not put credentials in labels or poller output. No automatic pruning.
- On exit or timeout, persist the outcome, then attempt at most one own-session
  notification if consented. Timeout terminates only the owned process group.
  If the host refuses cleanup, record `timeout_cleanup_refused`: the child/group
  may remain. Do not re-arm until its state is reconciled; no alternate kill,
  escalation, retry or automatic cleanup is attempted.
- A result hint is untrusted output, never a success verdict. Empty or unrecognized
  output stays unknown, including on exit zero. Read actual task/head/gates before
  acting. An observer can finish while the product task remains running or failed.
- Notifications are `queued_unacknowledged`, not read or handled. A missing,
  stopped, unavailable or paused owner leaves a durable receipt; a paused Codex
  owner is not queued or unpaused. Uncertain delivery is never retried/retargeted.
- `watch handled` records the real continuation or blocker, not permission to act.
  It is owner-only, requires a finished receipt and preserves the first entry.
  Reconcile outward effects before handling a replay. Ordinary self-`send`
  refusal, correlated request expiry and reply budgets remain unchanged.
- Re-read after host/session interruption. A mismatched/dead/unverifiable worker
  PID/start identity means lost/unknown coverage, never a successful observation.
  No claim transfer, restart, wake retry or observer re-arm follows automatically.

Machine sleep, service restart, owner death and TUI detachment are not continuity
guarantees. A test of one transport/runtime cannot certify another.

## Arm and hand back one observation

1. Record a private duty receipt: task key, full owner runtime/session address,
   target run or PR, exact head or approved-plan watermark, observer instance,
   stop set, deadline, next obligation and any dependent peer.
2. Preserve stdout/stderr in an owner-chosen private log path. Do not rely on the
   harness's temporary output file remaining available. Publish structured
   receipt updates atomically; an existing or nonempty file is not completeness.
3. Obtain STARTED evidence: native task ID, actual first status/head, start time,
   durable log path, exit contract and callback owner. A command accepted or
   queued without that evidence is not verified coverage.
4. Delegate only one observer per gate. Its contract is one callback on real exit,
   including expiry, error, or supersession, with no automatic intervention or
   re-arm. Different gates such as PR readiness and branch E2E stay separate.
5. The callback names the original duty and instance, observed head/watermark,
   actual exit code and final result, time and log. `ELAPSED`/timeout means
   **coverage ended, not done**. Exit zero alone is not a product success verdict.
6. The owner reconciles fresh task/claim/head state, handles only authorized next
   steps, and records the next observer or an explicit blocker. A callback read
   or an ACK alone is not proof the continuation happened. Notify dependents.

For OpenCode replies, `--allow-reply-wake` is single-use and lasts at most 3600
seconds. Size the observation episode to fit the grant; the default 600 seconds
cannot cover a longer watch. Do not silently extend consent or fall back to
`--wake` after expiry. A separately authorized task-scoped wake is distinct from
reply consent and still grants no action authority.

On resume, after long work and before reporting waiting/done, reconcile owned
unfinished observations and outstanding correlated request states. Replayed or
out-of-order receipts cannot repeat a merge, approval, dispatch or budget grant:
check task, instance, phase, head and prior action evidence first. Retire only
owned observers when their task is cancelled or superseded; do not kill an
unattributed process or infer new recovery work from a retained hold alone.

Queue/socket acceptance proves transport only. Require a matched reply or
correlated follow-up for read evidence, and a next-action/blocker receipt for
handled continuation. An absent/unverified owner retains the duty; it does not
authorize another session to steal claims or assume its permissions.
