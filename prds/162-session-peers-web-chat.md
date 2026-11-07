# PRD #162: session-peers web chat

**Issue**: [#162](https://github.com/vtmocanu/skills/issues/162) | **Label**: PRD | **Priority**: Medium
**Umbrella**: [#163](https://github.com/vtmocanu/skills/issues/163) (child 3 of 3)
**Blocked by**: [#161](https://github.com/vtmocanu/skills/issues/161) (read-only dashboard), merged as PR #169 (3cafa8b). This PRD names the merged interfaces; re-read them before implementing.
**Design mock**: `prds/mockups/161-session-peers-web/index.html` (the composer, recipient picker, `@all` warning and confirm).
**Area**: `skills/session-peers/` (`web.py`, `webdata.py`, `web/`, a browser peer, identity, routing and provenance, `SKILL.md`, `references/web.md`).
**Status**: M1 and M2 implemented on branch `agent/issue-162`: `--chat`, the browser peer, `POST /api/send` (`202 queued`, one worker), the Browser feed, the `@all` preview and confirm. Evidence: one integration-gate run over the final code (commit `43ee66c`) passed the full `skills/session-peers/scripts/test_peers.py` suite (1714 tests), the web `render.test.mjs` (93 tests), `validate_skills.py` and `check_bundle_coverage.py`. The first two "Landing obligations" (the real-machine Claude Code `ListAgents`/`SendMessage` check and visual parity with the mock) are owner acceptance, pending and not verified.

## Problem

A person steering many sessions has to switch to each session's terminal to send it a message, and cannot post to a channel or to everyone from one place. Replies come back in each session's own terminal, so the conversation is scattered.

## Outcome

`peers.py web --chat` starts the #161 page with a composer. Plain `peers.py web` stays exactly the #161 read-only dashboard: it writes nothing and its no-mutation tests are unchanged.

- Pick recipients in the current channel, or post to the channel, then send. In `#general`, a message starting with `@all` reaches every `#general` member after a confirmation listing the recipients (M2).
- The browser user is a distinct peer with typed identity **`web:<uuid>`** (a UUID generated per server start), shown with the OS user name and its own icon. Every message it sends carries provenance stating it came from the browser and is **peer input, not user approval**. Sessions keep approving only through their own trusted channel.
- Replies come back to the page: Claude `SendMessage` to the browser peer, a Codex final answer routed by its shim, and `peers.py send --to web:<uuid>` from OpenCode all land in the browser peer's inbox and appear in the page's **Browser** conversation.
- "Ask for ACK" requests a #157 ACK; the outcome row shows it when it arrives. Without it the send is still journaled, but the recipient is not asked to ACK and its row reads "ACK not requested" (outside any tally). `@all` always requests ACKs.

Acceptance examples:

1. In a channel holding L, the user selects L and sends "status?". L's fake socket receives it with browser provenance. A Claude-style reply to the browser peer's socket appears in the Browser feed through `GET /api/changes`.
2. The user sends to Codex buddy X. X's fake shim routes the final answer to the browser peer's socket (the shim's requester check passes because the browser peer's record carries `sessionId = <uuid>`), and the reply appears in the page.
3. (M2) In `#general`, the user types `@all freeze main`. `POST /api/send` returns a confirm nonce (binding below) with a 60-second expiry. Confirming with that nonce is accepted as `202 queued` and the worker sends to exactly that recipient set and requests ACKs; reusing the nonce is refused, and a set that changed before the worker sends is refused as `not sent` (a `429` at confirm leaves the nonce usable).
4. A `POST` without the token, from another `Origin`, with a non-JSON content type, with a missing, repeated or oversized `Content-Length` or any `Transfer-Encoding`, over the rate limit, to a server started without `--chat`, or (in M1) starting with `@all` or addressed to `#general` is refused and sends nothing.

## Mock contract

Same contract as #161 (presentation reference, text wins where it specifies, `Mock deviations` list in the PR, visual parity as a landing check, screenshots kept local and the comparison written in the PR). Composer-specific deviations:

- The mock's per-recipient checkbox picker stays; the mock's "Keep in journal" checkbox is dropped (browser sends are always journaled).
- The mock's local `confirm()` dialog becomes the server confirm round-trip in example 3, showing the recipient list, not only the count.
- The mock's simulated ladder and fake reply are replaced by real outcomes and replies.
- #161 dropped the mock's "How @all works" card from `#general`'s details panel. M2 restores it, corrected to describe eligible recipients and recorded outcomes: the post goes to the members discovery found; gone or refused members, idle OpenCode sessions (not woken) and discovery gaps show as their recorded outcomes. The mock's `app.js` says every session gets a turn, which is too strong.

## Out of scope

- Approving plans, granting budgets or any authority from the page.
- `@here` (idle-only broadcast).
- Remote access.

## Modules and seams

- **Chat mode**: `peers.py web --chat` enables everything below. Without it, `POST` stays refused (405), no browser peer registers, and #161's read-only guarantees and tests hold unchanged. With it, startup and `POST /api/send` write state on purpose; #161's no-mutation tests are scoped to the GET/read paths.
- **Browser peer** (new, `session_peers/webpeer.py`): lives in the web server process. It writes a registry record with the shape of `Shim.record()` (`shim.py`) through the same atomic registry write:
  - `pid` = the server's pid, `sessionId` = the per-start UUID, `name` = `web-<os user>`, `entrypoint` = `"web"`, socket in the Claude socket directory. Claude's sender checks that the process accepting on the socket is that pid, so the socket is served by threads in the same process, not forks.
  - `webpeer.py` owns its socket and record: removed on normal shutdown, on a startup failure after either was created, and on a signal. `lifecycle.py` and `maintenance.py` manage Codex shims and bridge artifacts, not arbitrary registry peers; extend them only if a stale `web` record left by a crash must be swept, with a test.
  - One browser peer per OS user: the server takes an exclusive `flock` on a lock file under the HOME-derived `~/.codex/session-peers/` root that `channels_root()` and `journal_dir()` use, with their ownership and mode checks. Not `storage.state_dir()`, which follows `CODEX_HOME`, so two servers with different homes would both get a lock. A second `peers.py web --chat` refuses with a clear error.
- **Address, routing and provenance**: `web:` is a new typed address kind, distinct from buddy eligibility.
  - `identity.parse_typed` accepts only `BUDDY_KINDS` today; split address kinds from buddy kinds so `web:` parses as an address and is never a buddy (`constants.BUDDY_KINDS` unchanged).
  - `journal._norm` and journal recipient resolution, `routes.KINDS` and route verification, and `messaging._dispatch_send` accept `web:` as a target, so `peers.py send --to web:<uuid>` (OpenCode, shell) reaches the socket.
  - `protocol.sender_runtime` returns `web` for `entrypoint == "web"`, and `protocol.build_origin` states "browser peer", never "Claude Code session".
  - Registry readers include or exclude `web` on purpose, each with a test: `claude.read_claude_records()` and `read_claude_records_checked()` callers; `buddy.py` (excluded: never a buddy); `peers.py list` (`diagnostics.cmd_list`: runtime `web`, labelled `[browser peer]`; `storage.py` has no registry listing, and its one registry read, the shim-home lookup, takes only `entrypoint == "codex"`); `codex.py` (a valid requester); `messaging.py` (a valid target); #160's derived `#general` (excluded: `channels.py` today classifies every non-Codex record as `cc:`, so it must skip `entrypoint == "web"`); and #161's discovery and read model (shown as the browser peer, not as a Claude session).
- **Inbox** (`webpeer.py`): each inbound user frame is journaled through `journal.record_message` (verified sender, recipient the browser peer) under a freshly minted message id; the frame's own `msg_id` is kept only as `orig_msg_id` for correlation.
  - Only after that call returns is the message appended to the in-memory Browser log, and then one NDJSON control line `peer_inbox_ack` (with `orig_msg_id`) is written back on the sender's connection. If the log append fails or a stop is under way, the connection closes with no answer.
  - A failed or full journal (`JournalError`): nothing is shown; the connection gets a `peer_inbox_refused` line with the detail; a verified sender also gets a `peer_message_status` `failed` frame (when the frame carried a valid `msg_id`) and a "not kept" notice once per failure episode, both through a bounded notifier queue.
  - `peers.py send --to web:<uuid>` (`messaging._send_web`) half-closes after the frame and waits up to 15 s for the answer line: exit 0 on `peer_inbox_ack`; 1 on `peer_inbox_refused`, timeout or close (outcome unknown), or 76 under `--record` when the `transport_error` cannot be appended.
  - A `send --record` sender leaves two journal records: its own (its message id) and the inbox's (fresh id).
  - While the server is down there is no socket: a Claude `SendMessage` fails at the sender, and a Codex shim logs "requester gone". Such replies are not delivered to the page; a sender that used `peers.py send --record` can still be read from the journal.
- **Browser feed**: #161's `/api/changes` emits channel entries plus journal ACK/error updates and drops journal message records. Chat mode adds a Browser conversation, served from an in-memory acceptance log in the server (owner decision 2026-11-07, see the Decision Log), not from a `journal.read_since` scan of message records:
  - Source: `BrowserState.log` holds inbound messages only after the inbox's own journal write returned, and direct browser sends from the moment they are accepted (`queued`); each send's state comes from its status row. Channel posts never enter the log (their status row shows in the channel), so a channel post appears only in its channel.
  - Budget: every `/api/changes` response stays inside the existing 8 MiB request budget.
  - ACK and error state: polling reads new ACK and transport-error events from the journal tail (`read_since`). On a reload (no `"b"` cursor) the restored outbound entries are hydrated by one journal context read, and the first page's journal cursor is the position of that same read; if that read yields no position (unreadable journal, budget exhausted, incomplete read), the cursor falls back to a position taken before hydration, so events are re-read, never lost. Updates are idempotent per message and recipient. A pruned entry reads "expired from the journal"; an unreadable journal or exhausted budget reads "unverified".
  - Retention: at most 1000 entries and 4 MiB, oldest evicted first; a cursor behind the oldest retained entry gets a gap ("earlier messages expired").
  - Reload: with no `"b"` cursor the page gets the newest retained entries since this server started, with a note that the conversation starts at server start.
  - Restart: a new UUID means an empty log and `restarted: true`.
  - Tested: same-server reload, bounded retention and the eviction gap, ACK hydration, and the hydration/cursor race.
  - Direct-send outcomes (accepted, failed, pending per recipient) live in an in-memory view for the current UUID, because the journal records messages, ACKs and transport errors but no successful transport outcome. The view is lost on restart, together with the feed it belongs to; ACKs and transport errors stay readable from the journal. Channel sends keep their outcomes in channel history, which already derives `unknown` for a send with no outcome once its lock is released.
- **Send route** (extends `web.py`): `POST /api/send` with JSON `{to, text, ack}`, where `to` is a list of typed addresses or one channel key.
  - Security: every #161 check stays (loopback bind, exact Host, token header, fixed static routes, CSP, 16 connection slots, 2-second header deadline, 10-second socket cutoff). Added: `Origin` equal to the page's own origin; `Content-Type: application/json` only; exactly one `Content-Length`, within the existing 1 MiB cap; any `Transfer-Encoding` refused; the body read bounded by the connection's remaining deadline; JSON and field validation before anything is journaled. `_check_body` keeps refusing a body on every other route. (No CSRF token: there are no cookies, and the token header already defeats cross-site requests.)
  - Order: under one acceptance lock, check the rate limits and the worker's bounded FIFO queue, then charge the rate limits and reserve a slot (a full queue or a rate limit returns `429` with nothing accepted and nothing charged); then the server returns `202 {"message_id": <id>, "state": "queued"}`, which means accepted in volatile memory only, not recorded and not delivered. The single background worker then resolves recipients, journals and records, delivers and sets the final outcomes. A refusal before any write (journal full or unavailable, an unresolvable recipient, an oversized message, a changed `@all` recipient set) becomes the status `not sent: <reason>`, and that message ID never gets a journal entry; a channel post whose history append fails after its journal write becomes `not sent (journaled)`. Outcomes arrive through `/api/changes`. The request thread never resolves recipients and never writes the journal or history. A 64-recipient channel send can take over a minute because Claude deliveries are paced (`channels.py` `PACE_SECONDS`); the worker, not a request thread, waits.
  - Nothing is ever retried, because an ambiguous send may already have been delivered. A send interrupted by a restart keeps whatever its record shows: a channel send reads `unknown` through channel history; a direct send's in-memory outcome is gone with the old feed.
  - `ack`: `true` journals the message with an ACK request and the recipient instruction to ACK; `false` journals it with the ACK explicitly not requested and no instruction, and its rows read "ACK not requested" rather than `pending`. `@all` forces `true`. A channel post persists the choice as `ack_requested` in its send history entry, and the read model renders `false` as "ACK not requested" after a reload (the journal's recipient row alone reads `pending`); a direct send keeps it in the current UUID's in-memory view.
  - Rate limits: at most 1 send per second and 30 per minute per server, with an injectable clock; `@all` limits are #160's fan-out limits plus the confirm nonce.
- **Channel posts from the browser**: a channel send today takes its sender from the session environment and requires that sender in the stored channel or derived `#general`. Add a `channels` seam that takes an explicit, server-authorised sender `web:<uuid>` for a stored channel (the browser peer is the machine's user, not a member) and then uses the existing history append, fan-out, pacing, ACK and outcome rules unchanged. The worker publishes the send entry under the existing per-send lock and holds it through fan-out to the final outcome, so a published post reads `in progress` until then (readers infer state from that lock); a queued, not yet published post is shown by its in-memory `queued` status row, and no lock is handed between threads. Direct sends to picked recipients use `messaging`, not this seam. In `#general`, M1 refuses everything; M2 allows only `@all` posts through the confirm round-trip (plain posts stay refused, rather than recorded-only, so nothing is posted to everyone without a confirmation).
- **Codex reply budgets**: a Codex thread's replies to the browser peer count against the shim's existing per-requester budget, with the default cap. The details panel shows the remaining replies for the browser peer; the user can raise it with the existing `peers.py budget allow`.
- **Rendering**: add `web` to the read model's runtime label, typed-address validator, name rendering and icon (`webdata.py`, `web/app.js`). Browser channel posts join delivery rows and ACK tallies like any channel send; Browser-feed messages show their own per-recipient outcome and ACK.

## Testing decisions

- Chat mode off: `POST` is 405 and the #161 no-mutation tests pass unchanged.
- Browser peer: record fields and atomic write, socket accept in-process, pid check, the `flock`, cleanup on shutdown, startup failure and signal, provenance `web` in both `sender_runtime` and `build_origin`, each registry reader's include/exclude test above, `web:` parsed as an address but refused as a buddy.
- Inbox: journal-before-ack ordering; a failing journal write refuses the acknowledgement.
- Browser feed: first page, cursor advance, retention gap, restart, no duplicate of a channel post.
- Send route: examples 1 to 4 with fake routes; each body/header refusal; a body that arrives slower than the deadline; the `202 queued` answer arriving before any journal or history write; the `queued` row visible before the worker picks the job up; a refusal before any write becoming `not sent` with no journal or history entry; a channel history failure after the journal write becoming `not sent (journaled)`; queue full and rate limit (`429`) neither charging nor enqueuing; a worker failure consuming its rate-limit charge; rate limits with an injectable clock; channel authorisation; `ack=false` rows and recipient text, including a channel post after a reload; a published channel post reads `in progress` while the worker fans out; no retry, including after a restart.
- Lock: two servers with different `CODEX_HOME` values cannot both take the browser-peer lock.
- M2 nonce: binding fields, expiry, single use, atomic acceptance (a `429` leaves the nonce usable and charges nothing), a changed recipient set refused by the worker as `not sent`, the same text to another route with an identical roster refused.
- Rendering safety: #161's tests extended to the composer, the Browser feed and replies.

## Milestones

### M1: send to sessions and channels, replies in the page

Chat mode, the browser peer, address/routing/provenance, inbox, Browser feed, `POST /api/send` with its security and worker (refusing `@all` and `#general`), the channel-post seam, the composer and recipient picker, "Ask for ACK", replies from all three runtimes; docs; tests.

- Blocked by: #161 merged (done).
- Acceptance: examples 1, 2 and 4.

### M2: `@all` with confirmation

The confirm nonce, the recipient-list confirmation, live ACK tally for browser broadcasts and the corrected "How @all works" card; lifting the M1 `#general` refusal for `@all` only; tests.

- Blocked by: M1.
- Acceptance: example 3. The nonce binds the route (`#general`), the browser peer ID, the exact sorted recipient typed identities, the message hash, the ACK choice and the expiry, and is consumed atomically before delivery.

## Landing obligations (not in-run)

- Pre-merge gate: on a real machine, Claude Code's own `ListAgents` lists the `web-<user>` record and `SendMessage` to it lands in the Browser feed (example 1 depends on native addressing). Note the result in the PR.
- Visual parity per the Mock contract.
- Any CI workflow step the worker cannot push.

## Decision Log

- **The browser user is a peer, not the user.** Claude Code labels cross-session messages as not typed by the user, and session-peers treats peer messages as information, never approval. Rejected: making web messages count as user input.
- **Register like the Codex shim**, so Claude sessions reply with native `SendMessage` and nothing changes on their side. Rejected: a pull-only browser mailbox.
- **A distinct `web` runtime**, because the existing code labels every non-Codex record as Claude, which would forge provenance and put the browser into `#general` as a Claude session.
- **`web:<uuid>` with a single instance per OS user**, because `web:<os user>` collides and two browser peers would split replies.
- **Journal before acknowledging**, so a reply the page cannot keep is never reported as delivered.
- **The `@all` nonce binds route, sender, exact recipient set, message hash, ACK choice and expiry**, not a count or only the roster, so neither a membership change nor a different route with the same roster can reuse a preview.
- **M1 refuses `@all` and `#general`**, so every merged slice is safe on its own.
- **Chat is opt-in (`--chat`)** (2026-11-07, after #161 merged), so the default dashboard keeps #161's read-only guarantee and its tests. Rejected: making every `peers.py web` writable.
- **Accept, then commit on the single worker** (2026-11-07, owner decision during #162 planning): `POST /api/send` reserves a slot and charges the rate limits, then answers `202 queued` (accepted in memory only); one FIFO worker resolves recipients, journals, delivers and sets outcomes, and nothing is retried. Reason: recipient resolution can take up to 20 s (`lsof`, the `codex` CLI) against the 10 s connection deadline, and committing before the `202` required a separate committer thread, cancellation and new lock deadlines. One worker with a bounded queue remains, because the 16-connection cap does not bound background sends and paced channel fan-out can outlast a request. Rejected: a thread per send; commit-before-202 with a committer thread.
- **The Browser feed is an in-memory acceptance log** (2026-11-07, owner-accepted during #162 planning), because the journal writes a line before `fsync` and truncates it only on failure, while `read_since` takes no lock, so a journal scan could show a received message whose write then fails. The log holds only messages the inbox committed and sends the server accepted; ACK and error updates still come from the journal. Retained tests: same-server reload, bounded retention and the eviction gap, ACK hydration and the hydration/cursor race. Rejected: a journal `read_since` scan of message records.
- **Direct-send outcomes in memory, per UUID** (2026-11-07), because the journal has no success-outcome event and the Browser feed restarts empty anyway. Rejected: rewriting old sends to `unknown` at startup; adding a journal outcome event.
- **A Browser feed separate from channel history** (2026-11-07), because direct sends and replies have no channel entry and #161's changes feed drops journal message records.
- **F1: publication is the journal write returning** (2026-11-07). No socket acknowledgement exists today: `claude.send_frame` sends and closes, and the shim never writes back. So the inbox counts a message received once `journal.record_message` returns, and adds the `peer_inbox_ack` / `peer_inbox_refused` answer line for `peers.py send`.
- **D3: the browser is never the inherited session** (2026-11-07). Chat startup removes `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET`, `CODEX_THREAD_ID`, `CODEX_SESSION_ID` and `OPENCODE_SESSION_ID` from the server's environment. The browser sender travels only in a server-set `web_sender` with no CLI flag; a malformed one is an error, never an environment fallback. Rejected: deriving the sender from the environment as the CLI does.
- **D4: no journal schema change** (2026-11-07). `ack=false` lives in channel history (`ack_requested`) and in the in-memory status rows. The browser peer never ACKs (no self-ACK), so its records stay until every slot is ACKed or settled (only then can they be pruned to fit) or until the 14-day age prune, and they count toward the journal quotas. Rejected: a journal field for the ACK choice.
- **D5: first-page cursor from the hydration read** (2026-11-07). When a first page hydrates restored sends (and its journal tail read was not partial), its journal cursor is the hydration read's own position, falling back to a position taken before hydration, so an event is re-read, never lost.
- **F4: the lock reuses journal's checks** (2026-11-07). `~/.codex/session-peers/web-chat.lock` is opened with journal's directory and file ownership and mode checks, not new ones. Rejected: `storage.state_dir()` (follows `CODEX_HOME`).
- **F5: registry record via `write_json_atomic`** (2026-11-07), a temp file then `os.replace`, as the shim does: atomic for readers and safe across a process crash. Durability across power loss is not claimed (no `fsync`).
- **Distinct status codes for `POST /api/send`** (2026-11-07): 400, 403, 404 (malformed target), 405, 408, 409 (`@all` only), 413, 415, 429, 503, so the page can explain each refusal. Table: `references/web.md`.
- **The `"b"` cursor field is accepted only with a browser view** (2026-11-07), so plain-mode cursors are unchanged and a `"b"` there is refused.
- **Status rows in memory per UUID** (2026-11-07): `queued`; `pending` with per-recipient outcomes and ACK; `not sent: <reason>`; `not sent (journaled)`; `failed after recording` (a worker error after the journal write). At most 1000 rows, oldest evicted first but never one the worker still owns; lost on restart; never retried.
- **Registry listing correction** (2026-11-07): the reader that lists records with kind `web` is `peers.py list` (`diagnostics.cmd_list`), not `storage.py`, which lists nothing and reads only Codex shim records.
- **`@all` (M2) as built** (2026-11-07). The preview reads the cached discovery only and accepts and charges nothing; it is refused (409) while discovery is pending, when a `#general` member went unchecked or has no liveness entry, or with zero or more than 64 recipients; gone members are listed as excluded. A confirm checks the binding (route, sender, text hash, ACK), then the limits, then charges and consumes the nonce, all under one lock; a `429` keeps the nonce; a mismatch leaves it in place. The worker refuses (`not sent`) when the live member set differs from the nonce's recipients. Nonces live in memory only, at most 32 (oldest evicted), 60 s each.
