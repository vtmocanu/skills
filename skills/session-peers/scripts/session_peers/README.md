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
| web | Dashboard HTTP server (loopback, token, GET only; one POST route in chat mode), the discovery worker and the `web` command; serves the static assets in `web/` (`index.html`, `app.css`, `app.js`); see Dashboard server below |
| webchat | Chat mode state: the Browser feed log, status rows, `SendGate` (rate limits and queue capacity) and the single `SendWorker` |
| webpeer | The browser peer `web:<uuid>`: lock, registry record, socket and journaled inbox |
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

Use the public `ReplyBudget` operations at the shim boundary; its validators
and implementation-only release helpers remain private.

Buddy binding publishes under owner lock followed by sorted binding locks:
revoke the old total, write the revocable owner record, then publish its grant.
Shared request preparation validates the target and creates one mailbox;
ask owns unconditional cleanup, dispatch preserves it for await, and pending
await retains an unexpired request. Keep those cleanup branches explicit.
Doctor probes preserve report order; CLI registration helpers preserve parser
insertion order and argument defaults.

## Dashboard server

User-facing behaviour is in `references/web.md`; the server's own guards are:

- Host must be exactly `127.0.0.1:<port>` or `localhost:<port>` (403), as a
  DNS-rebinding guard. Only GET is accepted (405); a request body is refused
  (400). `/`, `/app.css` and `/app.js` load without the token; every `/api/*`
  route needs `X-Peers-Token` (403).
- At most 16 connections at once; extra connections are closed unanswered. The
  request line and headers must arrive within 2 s and every connection's socket
  is cut after 10 s; its slot is held until the handler returns. The 8 MiB
  request budget bounds the file bytes each request reads, not decoded objects
  or response buffers, so it is a read budget, not a memory ceiling.
- Every response carries a `Content-Security-Policy` (`default-src 'none'`,
  scripts and styles from `'self'` only, no framing, no forms),
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `Cache-Control: no-store` and same-origin resource/opener policies. Errors
  have an empty `{}` body. No request is logged.
- The loopback bind keeps the server off the network, but any local user or
  process can reach the port; the per-start token alone guards `/api/*`, so
  anyone who obtains it can read the API. Neither defends against a hostile
  process of the same user.

### Chat mode (`web --chat`)

User-facing behaviour is in `references/web.md#chat-mode---chat`. Without
`--chat` the rules above hold unchanged: POST is 405 and no peer is registered.
With it, `POST /api/send` is the one write route; a request passes these
checks in order, and each refusal has an empty `{}` body:

1. Host (403), then a malformed request target (404), then the path being
   `/api/send` (405 otherwise), then no query string (400).
2. `X-Peers-Token` (403).
3. `Origin` appears once and equals `http://` plus the Host (403).
4. One `Content-Type` of `application/json`, optionally `; charset=utf-8`
   (415).
5. No `Transfer-Encoding` (400); exactly one digits-only `Content-Length` of at
   least 1 (400), at most 1 MiB (413).
6. The body is read before the connection deadline, less 0.5 s (408 on a short
   read or a timeout).
7. Strict JSON (400): no duplicate keys, a numeral of at most 32 characters,
   exactly `{to, text, ack}` or, for a confirm, `{to, text, ack, confirm}`,
   `text` a non-empty string of at most `MAX_TEXT_CHARS` that encodes as UTF-8,
   `ack` a bool, `to` one `#` channel key or 1-64 distinct typed addresses with
   no `web:`; `confirm` a string of 1 to `CONFIRM_MAX` (128) characters, only
   with `to` `#general` and an `@all` text.
8. A plain `#general` post, or text matching `channels.ALL_RE` to any route but
   `#general` (403). An `@all` to `#general` goes to `_post_all` after the 503
   check (step 9): no `confirm` is a preview, a `confirm` is a confirm.
9. Chat has started (503).
10. `SendGate`, under one lock: the rate limits (1 per second, 30 per rolling
    minute) and the queue capacity (`SEND_QUEUE_MAX = 8`, queued plus running).
    A hit is 429 with nothing charged or accepted.
    A preview (200, nothing accepted or charged) reads the cached discovery and
    is 409 when discovery is pending, a `#general` member is unchecked or has
    no liveness entry, no recipient remains or more than
    `channels.ALL_MAX_RECIPIENTS` (64) do. A confirm calls
    `SendGate.confirm`, which under the same lock checks the nonce binding
    (route, browser peer, recipient set, text hash, ACK, `NONCE_TTL` = 60 s;
    409 when unknown, expired, used or mismatched, a mismatch leaving it
    usable), then the limits (429, nonce kept, nothing charged), then consumes
    it and charges. Nonces are memory only, `NONCE_MAX` = 32, oldest evicted.
11. Mint the `message_id`, create the `queued` row, enqueue, and reply
    `202 {"message_id", "state": "queued"}`.

The 202 means accepted in volatile memory only. The handler never resolves
recipients or writes the journal or history; one `SendWorker` thread does that
in FIFO order (a confirmed `@all` passes `expected_recipients` to
`channels.send_as_web`, which refuses with `recipients changed, preview again`
when the live set differs), under `web._DISCOVERY_LOCK` for resolution, commit and each
member delivery (never across a pacing sleep). Nothing is retried, a worker
failure keeps the rate charge, and queued sends are lost on restart. The Browser
feed (`BrowserState`) is bounded at `FEED_MAX_ENTRIES = 1000` entries and
`FEED_MAX_BYTES = 4 MiB`.
