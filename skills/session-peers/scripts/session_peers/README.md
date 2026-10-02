# Runtime package

Run the sibling `../peers.py` launcher. Implementation modules are bundled
inside the skill directory; no Python package installation or third-party
Python dependency is required. The launcher path also drives shim subprocesses, hook commands, and
the reply command included in a request envelope.

| Module | Owns |
| --- | --- |
| constants | Protocol limits, patterns, version observations, event records |
| runtime | Shared time, input, subprocess, atomic-file, and source-digest helpers |
| config | Stdlib TOML reader and Python 3.9/3.10 fallback |
| protocol | Tags, wrappers, frames, validation, and reply headers |
| rollout | Bounded incremental reading and turn-boundary correlation |
| storage | Bridge paths, registry metadata, and ownership/binding locks |
| process | PID identity, file-holder probes, peer credentials, and ancestry |
| claude | Verified registry discovery, allowlisted sockets, and transport |
| codex | Thread discovery, alias resolution, writer locks, and queue transport |
| requests | Mailbox metadata, correlation, expiry, and single-use reply claims |
| lifecycle | Shim ownership, readiness, attachment, restart, and stopping |
| diagnostics | Version observations and rate-limited warnings |
| shim | Process/socket ownership, contacts, polling, delivery, and state serialization |
| budgets | Sequence/binding accounting, allowance markers, and held-reply policy |
| identity | Typed peer resolution and caller attribution |
| messaging | Send, correlated requests/replies, await, and wait commands |
| buddy | Buddy records, binding transactions, and budget commands |
| hooks | Hook installation, trust metadata, and startup reconciliation |
| topics | Pull-only shared logs, retention, identity, and cursor commands |
| maintenance | Owned bridge metadata and buddy garbage collection |
| cli | Argument registration and command dispatch |

Import modules eagerly in `__init__.py` before a shim serves traffic. Capture
the loaded source digest after those imports and keep it for the process's
lifetime. Restart adopts installed code explicitly; do not lazy-load replaced
modules into a running shim. Resolve executable paths through
`runtime.entrypoint_path()`, never an implementation module's `__file__`.

Tests patch the owning module directly. Replacing an attribute on the launcher
does not replace a function's defining globals; dynamic patches and cleanup
callbacks must target the same owner too. Run the complete aggregate suite:

```sh
python3 skills/session-peers/scripts/test_peers.py
```

Keep vendor assumptions in the Claude/Codex adapters. Discovery preserves
unknown liveness separately from dead; transport verifies identity before
routing. The launcher imports only the CLI entrypoint; command handlers live in the
package and no module imports the launcher back.

`ReplyBudget` uses discovery, socket validation, and delivery callbacks supplied
by `Shim`. It owns no transport and adds no lock. Keep the existing shim and
cross-process binding locks at their original call sites. The shim remains the
only serializer, using the same persisted keys and lifecycle points.

Use the public `ReplyBudget` operations at the shim boundary; its validators
and implementation-only release helpers remain private.
