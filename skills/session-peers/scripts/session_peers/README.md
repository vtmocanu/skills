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
| journal | Critical-message journal: records, ACKs, settlements, handovers, retention, and its `record`, `ack` and `inbox` commands |
| routes | Typed route check before send/ask/dispatch/wait, refusal advice, and delivery re-verification |
| messaging | Send, correlated requests/replies, await, and wait commands |
| buddy | Buddy records, binding transactions, and budget commands |
| hooks | Hook installation, trust metadata, and startup reconciliation |
| topics | Pull-only shared logs, retention, identity, and cursor commands |
| channels | Channel store under the user-wide root, membership-first history, member liveness, and the `channel` commands |
| webdata | Dashboard read model: bounded, read-only snapshots of channel history, journal ACK state and discovery, the poll cursor and the per-view JSON |
| web | Dashboard HTTP server (loopback, token, GET only), the discovery worker and the `web` command; serves the static assets in `web/` (`index.html`, `app.css`, `app.js`); see Dashboard server below |
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

Domain modules and shared fixtures live in the sibling
[peer_tests package](../peer_tests/README.md). The aggregate also accepts
existing test class and method selectors.

Keep vendor assumptions in the Claude/Codex adapters. Discovery preserves
unknown liveness separately from dead; transport verifies identity before
routing. The launcher imports only the CLI entrypoint; command handlers live in the
package and no module imports the launcher back.

`ReplyBudget` uses discovery, socket validation, and delivery callbacks supplied
by `Shim`. It owns no transport and adds no lock. Keep the existing shim and
cross-process binding locks at their original call sites. The shim remains the
only serializer, using the same persisted keys and lifecycle points.

## Dashboard server

User-facing behaviour is in `references/web.md`; the server's own guards are:

- Host must be exactly `127.0.0.1:<port>` or `localhost:<port>` (403), as a
  DNS-rebinding guard. Only GET is accepted (405); a request body is refused
  (400). `/`, `/app.css` and `/app.js` load without the token; every `/api/*`
  route needs `X-Peers-Token` (403).
- At most 16 connections at once; extra connections are closed unanswered. The
  request line and headers must arrive within 2 s and every connection is cut
  after 10 s. Memory worst case is about 16 connections times the 8 MiB request
  read budget.
- Every response carries a `Content-Security-Policy` (`default-src 'none'`,
  scripts and styles from `'self'` only, no framing, no forms),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` and same-origin resource/opener policies. Errors
  have an empty `{}` body. No request is logged.
- The token and the loopback bind limit access to one OS user's processes; they
  are not a defence against a hostile process of the same user.

Use the public `ReplyBudget` operations at the shim boundary; its validators
and implementation-only release helpers remain private.

Buddy binding publishes under owner lock followed by sorted binding locks:
revoke the old total, write the revocable owner record, then publish its grant.
Shared request preparation validates the target and creates one mailbox;
ask owns unconditional cleanup, dispatch preserves it for await, and pending
await retains an unexpired request. Keep those cleanup branches explicit.
Doctor probes preserve report order; CLI registration helpers preserve parser
insertion order and argument defaults.
