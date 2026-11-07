# PRD #162: session-peers web chat

**Issue**: [#162](https://github.com/vtmocanu/skills/issues/162) | **Label**: PRD | **Priority**: Medium
**Umbrella**: [#163](https://github.com/vtmocanu/skills/issues/163) (child 3 of 3)
**Blocked by**: [#161](https://github.com/vtmocanu/skills/issues/161) (read-only dashboard), merged as PR #169 (3cafa8b). This PRD names the merged interfaces; re-read them before implementing.
**Design mock**: `prds/mockups/161-session-peers-web/index.html` (the composer, recipient picker, `@all` warning and confirm).
**Area**: `skills/session-peers/` (`web.py`, `webdata.py`, `web/`, a browser peer, identity, routing and provenance, `SKILL.md`, `references/web.md`).

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
3. (M2) In `#general`, the user types `@all freeze main`. `POST /api/send` returns a confirm nonce (binding below) with a 60-second expiry. Confirming with that nonce sends to exactly that recipient set and requests ACKs; reusing the nonce, or confirming after the set changed, is refused.
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
  - Registry readers include or exclude `web` on purpose, each with a test: `claude.read_claude_records()` and `read_claude_records_checked()` callers; `buddy.py` (excluded: never a buddy); `storage.py` (listed with kind `web`); `codex.py` (a valid requester); `messaging.py` (a valid target); `diagnostics.py` (labelled `web`); #160's derived `#general` (excluded: `channels.py` today classifies every non-Codex record as `cc:`, so it must skip `entrypoint == "web"`); and #161's discovery and read model (shown as the browser peer, not as a Claude session).
- **Inbox**: inbound messages to the browser peer are written to the #157 journal with the browser peer as recipient before the socket acknowledges them. If the write fails or the journal is full (#157's journal-full error), the socket acknowledgement is refused, so the sender sees a failed delivery, and nothing is shown as received.
  - While the server is down there is no socket: a Claude `SendMessage` fails at the sender, and a Codex shim logs "requester gone". Such replies are not delivered to the page; a sender that used `peers.py send --record` can still be read from the journal.
- **Browser feed**: #161's `/api/changes` emits channel entries plus journal ACK/error updates and drops journal message records. Chat mode adds a Browser conversation: journal records sent by or to `web:<uuid>` that are not channel posts, read with `journal.read_since` inside the existing 8 MiB request budget. The changes cursor gains one journal position for this feed; the first page, polling, a retention gap ("earlier messages expired") and a server restart (a new UUID, so the feed starts empty and says so) are defined and tested. A channel post appears only in its channel, never also in the Browser feed.
  - Direct-send outcomes (accepted, failed, pending per recipient) live in an in-memory view for the current UUID, because the journal records messages, ACKs and transport errors but no successful transport outcome. The view is lost on restart, together with the feed it belongs to; ACKs and transport errors stay readable from the journal. Channel sends keep their outcomes in channel history, which already derives `unknown` for a send with no outcome once its lock is released.
- **Send route** (extends `web.py`): `POST /api/send` with JSON `{to, text, ack}`, where `to` is a list of typed addresses or one channel key.
  - Security: every #161 check stays (loopback bind, exact Host, token header, fixed static routes, CSP, 16 connection slots, 2-second header deadline, 10-second socket cutoff). Added: `Origin` equal to the page's own origin; `Content-Type: application/json` only; exactly one `Content-Length`, within the existing 1 MiB cap; any `Transfer-Encoding` refused; the body read bounded by the connection's remaining deadline; JSON and field validation before anything is journaled. `_check_body` keeps refusing a body on every other route. (No CSRF token: there are no cookies, and the token header already defeats cross-site requests.)
  - Order: reserve a slot in the worker's bounded FIFO queue first (a full queue returns `429` with nothing recorded); then journal and record; on success the job becomes visible to the worker and the server returns `202` with the message ID; if the commit fails, the reservation is released. Delivery runs on one background worker thread; outcomes arrive through `/api/changes`. A 64-recipient channel send can take over a minute because Claude deliveries are paced (`channels.py` `PACE_SECONDS`); the worker, not a request thread, waits.
  - Nothing is ever retried, because an ambiguous send may already have been delivered. A send interrupted by a restart keeps whatever its record shows: a channel send reads `unknown` through channel history; a direct send's in-memory outcome is gone with the old feed.
  - `ack`: `true` journals the message with an ACK request and the recipient instruction to ACK; `false` journals it with the ACK explicitly not requested and no instruction, and its rows read "ACK not requested" rather than `pending`. `@all` forces `true`. A channel post persists the choice as `ack_requested` in its send history entry, and the read model renders `false` as "ACK not requested" after a reload (the journal's recipient row alone reads `pending`); a direct send keeps it in the current UUID's in-memory view.
  - Rate limits: at most 1 send per second and 30 per minute per server, with an injectable clock; `@all` limits are #160's fan-out limits plus the confirm nonce.
- **Channel posts from the browser**: a channel send today takes its sender from the session environment and requires that sender in the stored channel or derived `#general`. Add a `channels` seam that takes an explicit, server-authorised sender `web:<uuid>` for a stored channel (the browser peer is the machine's user, not a member) and then uses the existing history append, fan-out, pacing, ACK and outcome rules unchanged. The seam acquires the existing per-send lock before publishing the send entry, hands its ownership to the queued job, and releases it only after the final outcome, so a queued but unstarted post reads `in progress`, not `unknown` (readers infer state from that lock). Direct sends to picked recipients use `messaging`, not this seam. In `#general`, M1 refuses everything; M2 allows only `@all` posts through the confirm round-trip (plain posts stay refused, rather than recorded-only, so nothing is posted to everyone without a confirmation).
- **Codex reply budgets**: a Codex thread's replies to the browser peer count against the shim's existing per-requester budget, with the default cap. The details panel shows the remaining replies for the browser peer; the user can raise it with the existing `peers.py budget allow`.
- **Rendering**: add `web` to the read model's runtime label, typed-address validator, name rendering and icon (`webdata.py`, `web/app.js`). Browser channel posts join delivery rows and ACK tallies like any channel send; Browser-feed messages show their own per-recipient outcome and ACK.

## Testing decisions

- Chat mode off: `POST` is 405 and the #161 no-mutation tests pass unchanged.
- Browser peer: record fields and atomic write, socket accept in-process, pid check, the `flock`, cleanup on shutdown, startup failure and signal, provenance `web` in both `sender_runtime` and `build_origin`, each registry reader's include/exclude test above, `web:` parsed as an address but refused as a buddy.
- Inbox: journal-before-ack ordering; a failing journal write refuses the acknowledgement.
- Browser feed: first page, cursor advance, retention gap, restart, no duplicate of a channel post.
- Send route: examples 1 to 4 with fake routes; each body/header refusal; a body that arrives slower than the deadline; queue full with nothing recorded, and the reserve/commit race at that boundary; a failed commit releasing its slot; rate limits with an injectable clock; channel authorisation; `ack=false` rows and recipient text, including a channel post after a reload; a reader between the `202` and worker pickup sees `in progress`; no retry after a restart.
- Lock: two servers with different `CODEX_HOME` values cannot both take the browser-peer lock.
- M2 nonce: binding fields, expiry, single use, atomic consumption, a changed recipient set refused, the same text to another route with an identical roster refused.
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
- **One send worker with a bounded queue, slot reserved before the commit** (2026-11-07), because the 16-connection cap does not bound background sends and paced channel fan-out can outlast a request, and a full queue must not leave a recorded but undeliverable send. Rejected: a thread per send.
- **Direct-send outcomes in memory, per UUID** (2026-11-07), because the journal has no success-outcome event and the Browser feed restarts empty anyway. Rejected: rewriting old sends to `unknown` at startup; adding a journal outcome event.
- **A Browser feed separate from channel history** (2026-11-07), because direct sends and replies have no channel entry and #161's changes feed drops journal message records.
