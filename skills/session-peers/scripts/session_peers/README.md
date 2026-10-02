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
routing. The command handlers remain in the launcher during this incremental
extraction; package modules do not import it back.
