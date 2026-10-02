# Manage supervised reply budgets

- A Claude session binding a Codex buddy whose shim is running gets a TOTAL
  of 100 replies by default; `--replies N` (1..500) sets another. A total
  above 20 needs a shim started from this peers.py. The total is what this
  binding may receive, counting every delivered reply (the default first three included),
  across sequences (another peer, a direct Codex turn, 30 idle minutes) and
  shim restarts. It is never replenished; once spent, the default cap of 3 per
  sequence applies again. It is bound to this session and that buddy, and a
  thread carries one owner's total at a time (a second owner is refused until
  the first clears). `buddy clear` or binding another buddy revokes it and
  fails, keeping the binding, if the revoke cannot be written; `budget reset`,
  `up` and `restart` leave it. `buddy` shows "replies left". An explicit
  `--replies N` exits 1 unless a running (or attachable) shim is proven to read
  it: `peers.py restart <uuid>`, then bind again. The default is best-effort:
  with no running shim, an older shim, or another owner's total, the bind
  succeeds without it (a warning in the last two cases).

**Longer loops.** A bound Codex buddy's total usually covers a whole working
session. For another peer, or a buddy bound without a total, raise the reply
cap for a user-requested loop instead of resetting it every round:

```bash
<this skill's directory>/scripts/peers.py budget allow buddy --replies 10
```

`N` is the total for the current sequence (maximum 20); repeating a grant does
not replenish it. Raising it may deliver a held reply at once.

- The allowance ends with the sequence: another peer's request, a direct Codex
  turn, a reply more than 30 minutes after the previous one, `budget reset`, or
  `up`. `peers.py restart <uuid>` keeps a still-valid grant and the replies
  already spent; it never replenishes.
- Reset before granting, never after: a reset drops the grant. `budget reset`
  then `budget allow` back to back is safe.
- `allow` exits 1 unless the running shim's state proves it reads grants (a
  shim keeps the code it started with). Run `peers.py restart <uuid>`, then
  grant again.

- **Reply budget**: the shim delivers at most 3 consecutive replies to one peer,
  each within 30 minutes of the previous one (turn time counts). Your new
  message does NOT reset it: an unattended loop also sends one every round, so
  that is the pattern the guard stops. A direct Codex turn, a request from
  another peer, a reply more than 30 minutes after the previous one,
  `peers.py budget reset <name|uuid>`, or a fresh `up` resets the sequence.
  A blocked reply is HELD, not lost: the latest one per peer is kept (mode-0600
  state, never the log) and `budget reset` releases it, marked
  `[held reply, in reply to message <msg_id>]`, once the shim is up. Any other
  reset discards it, and the shim purges it from its state 30 minutes after
  holding it. The shim emits a correlated `failed` status
  (surfaced only when the requester tracks the originating message) and one
  plain, non-replyable notice per sequence naming the reset command.
- **Supervised multi-round work** (a user-requested review loop): first run
  `peers.py list`; a target showing `not registered` with no `shim <pid>` has no
  reply route, so run `peers.py up <name|uuid>` before sending. Then grant
  the loop's total with `peers.py budget allow <name|uuid|buddy> --replies N`
  (see [the core buddy rules](../SKILL.md#buddy)), or run `peers.py budget reset <name|uuid>` before every send
  from round 4 on: a reset zeroes the count, so the cap trips again three
  replies later. Grant or reset only for a loop the user asked for, never to
  prolong an unattended one.
