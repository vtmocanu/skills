# Attach and manage shims

### Automatic attachment (recommended)

Install the Codex `SessionStart` hook once:

```bash
<this skill's directory>/scripts/peers.py install-hook --auto-attach
```

Then open `/hooks` in Codex and trust the new entry. Installing it is the
explicit opt-in: every root Codex thread attaches on `startup` or `resume`,
using the `session_id` from hook input. The attachment is session-scoped and
does not add an entry to the persistent manual-registration list.

Codex exposes no rename hook. The shim therefore re-reads the thread title every
30 seconds while retaining its 5-second liveness check. Set
`SESSION_PEERS_ALIAS_REFRESH_INTERVAL` to change the rename cadence. A valid
`/rename` value (letters, digits, dot, underscore and hyphen, up to 64
characters) becomes the peer alias without restarting the shim. An absent,
unsafe or conflicting title uses `codex-<uuid prefix>` instead. Confirm with
`/list-agents` (or `ListAgents`); the peer appears as `interactive` with `idle`
or `busy` status.

**Addressing caution.** A title that is not a valid alias (spaces, other
punctuation) shows only as the opaque `codex-<first 8 hex>` in `ListAgents`, and
two threads sharing those 8 hex fall back to the full UUID. `ListAgents` alone
cannot tell you which thread an opaque `codex-…` peer is, so before messaging
one run `peers.py list` to read its human title (and `peers.py doctor` to catch
a live thread that has no shim yet) rather than guessing, messaging the wrong
thread is silent.

### Manual persistent attachment

Without the automatic hook, name the Codex thread and register it explicitly:

```bash
<this skill's directory>/scripts/peers.py up <name|uuid>
```

Manual registration persists the UUID so a later bare `up` can recreate its
shim. Discovery needs the thread's row in Codex's state DB. A thread that has a
row but no rollout yet (for example `/rename`d before its first turn) still
attaches, because liveness also counts the writer lock. A thread whose lock is
held but has no row yet cannot attach; `up` then names the row, lock and rollout
it checked. Check `peers.py list` and `peers.py doctor`.

### Keeping shims alive

Shims exit when their thread's process holds neither the rollout nor the writer
lock (i.e. the thread is gone), on `down`, or on reboot (`/tmp` is cleared).
Claude already manages its own registry and rename lifecycle, so no Claude hook
is needed. A bare `down` stops every shim
and keeps the registrations (a bare `up` brings them back); `down <name|uuid>`
also unregisters. On first startup, the shim recovers an already-running turn's
request and reply address, without replaying completed turns. A restart from
saved state delivers a reply that completed while it was down only if that
turn finished within the last 15 minutes. An unnamed thread
registered by UUID appears as `codex-<first 8 hex of the uuid>`.

A running shim keeps the code it started with, so a session-peers upgrade reaches
an attached thread only after its shim restarts: `peers.py restart <uuid>`. It
keeps the registration, the reply budget, the replies already spent and any
grant or buddy total that is still valid, and never replenishes. `down <uuid>`
then `up <uuid>` is a deliberate reset instead: `up` zeroes the budget and drops
any grant.

## Codex hook

`install-hook --auto-attach` appends one `startup|resume` `SessionStart` entry
to `$CODEX_HOME/hooks.json`, backing up an existing file first. Re-running it
updates the session-peers entry in place and preserves other hook groups. Codex
requires review through `/hooks` before a new or changed entry runs.

Hooks are enabled by default. The installer never edits `config.toml` and never
overrides an explicit `[features] hooks = false`; `doctor` reports that disable.
Plain `install-hook` retains reconcile-only mode for users who want only manual
persistent registrations.

## Garbage collection

The SessionStart worker removes bridge metadata whose thread has been inactive
for seven days. It rechecks that the Codex thread is not live and that no shim
owns the PID file before deleting anything. Only files under
`$CODEX_HOME/session-peers/` are eligible; Codex rollouts, writer locks, and
queued messages are never touched.

Persistent manual registrations are bridge metadata and expire too. Resuming a
thread with the auto-attach hook exposes it again; without that hook, run `up`
again after a seven-day inactive period.

```bash
<this skill's directory>/scripts/peers.py gc --dry-run
<this skill's directory>/scripts/peers.py gc --days 7
```

Set `SESSION_PEERS_GC_DAYS` to change automatic retention.
A manual `gc` also applies topic retention; `post` and `tail` apply it to
their own topic.

- First startup retains the active request and skips completed history.
  Delivery across a restart from saved state is bounded: a tagged turn that
  completed while no shim ran is delivered only if it finished within 15
  minutes of the shim starting; older ones are skipped.
