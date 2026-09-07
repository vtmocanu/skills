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
    queueing, and the shim must exit when its thread's rollout is no longer held.
11. **Signal cleanup.** A shim killed with SIGTERM must remove its record and
    socket. Python's default handler skips `finally`, so the shim installs a
    handler (measured 2026-09-07: orphan files left behind without one).
12. **Names.** `threads.name` in `state_*.sqlite` is also written by the title
    suggester (one-word titles allowed); registration is explicit, never inferred.
    `session_index.jsonl` lines are `{"id","thread_name","updated_at"}` (measured
    2026-09-07); a rename appends a new line for the same id.
13. **Hook timing (open).** Where `SessionStart` fires on this version (source
    for 0.153.2 defers it to the first turn) and whether a detached `up` survives
    the hook runner. Not measured yet; the bridge does not depend on it.
