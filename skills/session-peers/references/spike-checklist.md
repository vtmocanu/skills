# Spike checklist: re-verify the undocumented seams after an upgrade

Run this after a Claude Code or Codex CLI upgrade, or whenever `peers.py doctor`
prints a version warning. Every step is read-only except starting one throwaway
shim and sending a few messages. Record the result next to each item with the
versions tested. Last full run: 2026-09-07, Claude Code 2.1.263, Codex CLI 0.153.4.

## Claude side

1. **Registry record accepted.** Start a shim for a live Codex thread
   (`peers.py up <name>`), then in a Claude session run `/list-agents`. The
   thread must appear under its name as `interactive`.
   - 2026-09-07: appeared 4 s after start, `codex-uzi [a6a2b4] · interactive · idle`.
   - Record shape used: `pid, sessionId (= thread uuid), cwd, startedAt, procStart,
     version ("codex-<ver>"), peerProtocol 1, peerFeatures ["notify_idle"], kind
     "interactive", entrypoint "codex", pidDomain, messagingSocketPath, name,
     nameSource "user", nameSince, status, updatedAt, statusUpdatedAt`.
   - No `.key` file was written and the send still worked (auth line optional on
     macOS).
2. **Endpoint checks pass.** From that Claude session, `SendMessage` to the
   name. The shim log must show one `user` frame.
   - 2026-09-07: passed. Claude sent, with no auth line:
     `{"msgV":1,"msg_id":...,"type":"user","message":{"role":"user","content":"<cross-session-message from=\"uds:/tmp/cc-socks/<pid>.sock\" from-name=\"<name>\" from-mode=\"prompting\">\n...\n</cross-session-message>"},"priority":"next","from":"uds:/tmp/cc-socks/<pid>.sock"}`.
     `from-session` and `hop-chain` were absent, so both are optional.
3. **Socket directory.** The shim must bind in the directory of a live Claude
   record's `messagingSocketPath` (`/tmp/cc-socks` here). Binding elsewhere is
   refused by the sender.
4. **`notify_when_idle`.** `SendMessage` with `notify_when_idle: true`. With
   `peerFeatures: []` Claude refuses the subscription with "that session runs a
   version without idle notices"; with `["notify_idle"]` a
   `{"type":"control","action":"notify_when_idle","from":...,"msg_id":...}` frame
   must reach the shim, and a `peer_idle_notice` posted back to `from` must be
   accepted.
   - 2026-09-07: the refusal with `[]` measured; with `["notify_idle"]` the frame
     `{"type":"control","action":"notify_when_idle","from":"uds:...","from_mode":"prompting","msgV":1,"msg_id":"..."}`
     arrived and the notice `{"type":"control","action":"peer_idle_notice","orig_msg_id":"...","state":"idle","finished_at":<ms>,"detail":"..."}`
     rendered in Claude as a `[Cross-session idle notice]` line.
5. **Reply rendering (D7).** Post the wrapped form
   `<cross-session-message from="uds:<shim sock>" from-session="<uuid>" from-name="<name>">BODY</cross-session-message>`
   from the shim's socket into the Claude session. The terminal must show
   `› Message from @<name>: BODY (ctrl+o to expand)`.
   - 2026-09-07: wrapped form with and without `from-mode` both rendered as a
     named peer preview; the bare form rendered like a typed prompt with no name.
     The bridge sends the wrapper without `from-mode`.
6. **`CLAUDE_CONFIG_DIR`.** Assumed: the registry is `$CLAUDE_CONFIG_DIR/sessions`.
   Not proven on 2.1.263 (binary strings are fragmented); `peers.py` reads the
   variable with default `~/.claude`, which is correct for a default install.

## Codex side

7. **`codex queue` reaches a live thread.** `codex queue --thread <uuid> --message
   ping` into a live, idle, un-interrupted thread must start a turn within 10 s.
   - 2026-09-07: three trials under 1 s on a thread that had just completed a
     turn; 5.9 s on an idle thread (the poll interval).
8. **Turn boundary events.** In the rollout, one queued message produces
   `task_started {turn_id}` then the `role: user` item then `task_complete
   {turn_id, last_agent_message}`; an interrupt produces `turn_aborted {turn_id,
   reason}`.
9. **Interrupt pauses the queue.** After Ctrl-C the queued items stay in
   `queued_items` until the user types a new prompt; a `codex resume` of the
   thread alone does not drain them and appends no boundary event to the
   rollout (measured 2026-09-07: nothing between the `turn_aborted` and the next
   typed prompt's `task_started`). `peers.py` reports "paused after interrupt"
   instead of pretending delivery.
10. **Dead thread keeps its queue.** When the holding process exits, items stay
    in `queued_items` (measured 2026-09-07). Liveness must be checked before
    queueing, and the shim must exit when its thread is no longer held (neither
    its rollout nor its writer lock, see item 14).
11. **Signal cleanup.** A shim killed with SIGTERM must remove its record and
    socket. Python's default handler skips `finally`, so the shim installs a
    handler (measured 2026-09-07: orphan files left behind without one).
12. **Names.** `threads.name` in `state_*.sqlite` is also written by the title
    suggester (one-word titles allowed); registration is explicit, never inferred.
    `session_index.jsonl` lines are `{"id","thread_name","updated_at"}` (measured
    2026-09-07); a rename appends a new line for the same id.
13. **Startup during an active request.** Write a tagged request to the rollout
    before starting its shim, then finish the turn. Verify the final reply
    reaches the original session exactly once, and earlier completed turns
    are not replayed. Repeat with a partial request line and with a crash after
    shim startup followed by completion while it is down.
    - 2026-09-08, Codex CLI 0.153.4: the tagged request was logged 16 seconds
      before the shim started. First startup skipped to EOF and recovered only
      the busy status, losing the reply address. Reproduced with the shipped
      script; the fix reads historical events without emitting them, retaining
      the active turn and sender, then persists that state before readiness.
      Regression tests cover actual socket delivery and crash/restart recovery.
    - Exact `SessionStart` hook timing remains unverified. The bridge handles
      startup after the request regardless of what launched it.
14. **Rollout is lazy; the writer lock is not.** A brand-new thread (`/rename`d
    but not yet run a turn) has a `threads.rollout_path` in the DB but NO file at
    that path: Codex can defer creating the rollout until the first turn.
    Meanwhile a live Codex process holds `<CODEX_HOME>/thread-writer-locks/<thread
    uuid>.lock` open from thread creation. So liveness (`codex_threads`,
    `thread_is_held`) checks whichever handle is held; a fresh thread reads as
    live through the lock alone, and the rollout file appears in time for the
    reply tail. Confirm both: `lsof -- <rollout_path>` finds no holder on a
    just-created thread while `lsof -- <lock path>` finds the `codex` process;
    after one turn the rollout exists and is (transiently) held.
    - 2026-09-07, Codex CLI 0.153.4: measured exactly this. `up hi` refused a
      just-renamed thread until it had run one turn (rollout absent); the lock
      was held throughout. The lock-aware liveness landed in this same change.
    - Correction verified 2026-09-08 on 0.153.4: a rollout contained the tagged
      request and intermediate output before the first turn completed. The old
      claim that the file appears only after completion was too strong;
      liveness must tolerate an absent file without assuming when it appears.
