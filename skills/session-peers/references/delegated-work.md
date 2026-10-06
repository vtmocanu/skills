# Delegate execution and steer it

**Delegated work (steer and execute).** When the user has the buddy do the
work while this session steers and reviews:

- A Codex worker sees queued messages only between turns, so corrections sent
  during a long turn pile up and its replies answer superseded instructions.
  Steer through check-ins instead of a message stream. A queued message never
  interrupts the active turn; each later runs as its own turn with its own
  reply, and an asynchronous reply spends the reply budget. Once the worker is
  in a long turn, answer only inside the correlated reply to its next check-in;
  an intervention that cannot wait for it needs the user. This includes new
  tasks: queue nothing while the worker is busy. Observed: tasks queued
  mid-turn drew several late "already complete" replies, and one task body
  never reached the active turn until resent inside a check-in reply.
- If the worker's turn completes without continuing to the next step and no
  check-in arrives, resend the next instruction as an ordinary message.
- **The worker checks in** at each gate, about every 15 minutes while actively
  working, and before any push, apply or other outward step: done (with SHAs),
  next, blockers, questions. Each check-in also restates the current task, its
  constraints and the last approved SHA, so the steerer sees drift after a
  context compaction at once. A routine check-in uses `dispatch`, so work
  continues, and the worker briefly `await`s that request at its next
  check-in; set `dispatch --timeout` longer than the interval, since expiry
  discards even an unread reply. A pending `await` (exit 124) stays resumable. A gate
  check-in uses `ask`, which blocks until the reply or its timeout; a timeout
  (exit 124) deletes the mailbox, so a late reply is lost, and permits only
  independent, already-authorized work. The gate stays closed.
- **The steerer answers in that reply**, folding in everything it would have
  sent: verdicts, user decisions, corrections. An ordinary message sent after
  the worker's turn completes marks one it replaces `supersedes <msg_id>`.
- Relay user decisions verbatim and say they came from the user; the worker
  cannot see this session's conversation.
- **Freeze what the user runs.** When the user runs a binary, app or test lane
  built from the worker's worktree, the worker builds and mutation-tests only
  in its own scratch build path, or an isolated checkout when wrappers
  hardcode output paths, until the steerer says the run finished. Staging
  locations the run copies from stay frozen too. Before handing a run over,
  the steerer confirms the tree is clean at the approved SHA with mutations
  restored, and a normal build from it succeeds. A binary timestamp or
  `strings` check is supplementary: an incremental build need not relink, so
  a correct binary can predate its commit.
- Write definitions, not labels, into the tracked brief: record each finding's
  text, not just its ID (`FN4-FN6`). Scratch notes and session transcripts are
  not automatically carried into the next lead's context.
- A sandboxed worker may lack host network (forge HTTPS, state backends, LAN
  hosts, secret stores). It sends the exact command; the steerer runs it only
  if the user authorized that action for this work, and returns the output.
  Never run an action the worker was denied permission for.
- A build step that needs a blocked package cache (e.g. a Nix-based render)
  and whose output the steerer reviews anyway: the worker skips it, commits,
  and hands over the SHA; the steerer runs the step on the host at that SHA.
  The worker never counts a step it could not run.
- Before implementation the worker probes its assigned worktree, Git metadata
  writes, and socket tests. When the ordinary sandbox blocks one, it first
  tries the authorized approval escalation, which avoids a relaunch and
  routing every command through the steerer. A denied approval is final: the
  worker neither runs nor relays that command. When escalation stays blocked
  without a denial, it relays the exact command, which the steerer runs only if
  the user authorized that action. A sandbox restriction is not an approval
  denial.
- The same probe compares each gate tool's version with the repo's CI pin.
  On a mismatch the worker provisions the pinned version with the repo's
  verified setup method in a temporary path or isolated environment (never a
  global install), or reports a blocker when that is unavailable. A gate run
  on the wrong version is reported, not counted.
- The worker's own review never counts as independent review of its work. The
  steerer reviews the diff and arranges an independent reviewer; the worker
  verifies that reviewer's findings.
- Correlated `ask`/`dispatch` replies bypass the shim reply budget; asynchronous
  replies do not. When the user's message gives a number, grant it as the
  delegation starts (`peers.py buddy set NAME --replies N`), not after a reply
  is held.
- **A Codex worker that builds and tests may need a widened sandbox.** On
  macOS with Codex 0.160.0, the default `workspace-write` blocked loopback
  sockets, a worktree outside the thread's working directory, and the home
  build caches. Have the user launch it from a shell (`WT` is the worker's
  worktree, `MAIN` the main checkout):

  ```bash
  cd "$WT"
  codex -C "$WT" -s workspace-write \
    -c 'sandbox_workspace_write.network_access=true' \
    -c "sandbox_workspace_write.writable_roots=[\"$WT\", \"$MAIN/.git\", \"$(getconf DARWIN_USER_CACHE_DIR)\", \"$HOME/Library/Developer/Xcode/DerivedData\", \"$HOME/Library/Caches/org.swift.swiftpm\", \"$HOME/Library/org.swift.swiftpm\"]"
  ```

  To keep an existing thread's history, run `codex fork <thread-uuid>` with
  the same flags instead of `codex`. Add any project cache to
  `writable_roots`; drop the Xcode and SwiftPM entries for other toolchains.
  - List the worktree explicitly, and the main checkout's `.git` (a
    worktree's git data lives there).
    Observed: `git update-ref` then worked inside the sandbox, but `git add`
    could not create the worktree's index lock; the worker committed through
    an approval-escalated command, staging explicit paths only.
  - Observed: a fork made inside a running Codex window inherits the parent
    thread's working directory, and `codex resume` refused ("open in another
    app") while the shared app-server daemon still held the thread after its
    window exited. A shell `codex fork` with `-C` avoided both.
  - Observed: SwiftPM and Xcode package resolution start their own
    `sandbox-exec`, which failed nested inside Codex's sandbox. The worker
    passes `--disable-sandbox` (SwiftPM) or
    `-IDEPackageSupportDisableManifestSandbox=YES
    -IDEPackageSupportDisablePluginExecutionSandbox=YES` (xcodebuild); the
    steerer runs wrappers that cannot take those flags outside any sandbox.
  - Test the settings with `codex sandbox` and the same `-c` flags before the
    user relaunches. Before any code, the worker probes loopback bind, a
    write to each root, `git update-ref`, and one build and test, then
    removes its probe files and refs.
  - Record the project's exact command in its own tracked agent docs.
