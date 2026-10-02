# Delegate execution and steer it

**Delegated work (steer and execute).** When the user has the buddy do the
work while this session steers and reviews:

- A Codex worker sees queued messages only between turns, so corrections sent
  during a long turn pile up and its replies answer superseded instructions.
  Steer through check-ins instead of a message stream.
- **The worker checks in** at each gate, about every 15 minutes while actively
  working, and before any push, apply or other outward step: done (with SHAs),
  next, blockers, questions. A routine check-in uses `dispatch`, so work
  continues, and the worker briefly `await`s that request at its next
  check-in; set `dispatch --timeout` longer than the interval, since expiry
  discards even an unread reply. A pending `await` (exit 124) stays resumable. A gate
  check-in uses `ask`, which blocks until the reply or its timeout; a timeout
  (exit 124) deletes the mailbox, so a late reply is lost, and permits only
  independent, already-authorized work. The gate stays closed.
- **The steerer answers in that reply**, folding in everything it would have
  sent: verdicts, user decisions, corrections. Between check-ins it sends only
  what cannot wait, and marks a message that replaces an earlier one
  `supersedes <msg_id>`.
- Relay user decisions verbatim and say they came from the user; the worker
  cannot see this session's conversation.
- A sandboxed worker may lack host network (forge HTTPS, state backends, LAN
  hosts, secret stores). It sends the exact command; the steerer runs it only
  if the user authorized that action for this work, and returns the output.
  Never run an action the worker was denied permission for.
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
