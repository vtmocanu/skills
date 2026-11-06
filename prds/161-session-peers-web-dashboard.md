# PRD #161: session-peers web dashboard (read-only)

**Issue**: [#161](https://github.com/vtmocanu/skills/issues/161) | **Label**: PRD | **Priority**: Medium
**Umbrella**: [#163](https://github.com/vtmocanu/skills/issues/163) (child 2 of 3)
**Blocked by**: [#157](https://github.com/vtmocanu/skills/issues/157) (journal, per-recipient ACKs, same-user read-only reader) and [#160](https://github.com/vtmocanu/skills/issues/160) (channels). Do not start until both are merged; then re-read their merged interfaces before implementing.
**Design mock**: `prds/mockups/161-session-peers-web/index.html` (a directory, because the mock is two files: open `index.html` in a browser; it loads `app.js` beside it, uses mock data, and fetches Google Fonts, so an offline render falls back to system fonts). The mock also shows the composer from #162; this PRD implements everything except sending.
**Area**: `skills/session-peers/` (a `web` command, its static assets, `SKILL.md`, `references/`).

## Problem

A person running a dozen or more Claude Code, Codex and OpenCode sessions on one machine cannot see, in one place, which sessions are live, which are paired, what was said in each channel, and which broadcasts were never acknowledged. Sessions themselves misread delivery: a queued message or an idle notice is taken for a reply.

## Outcome

`peers.py web` starts a local, read-only page and prints its URL:

- **Channels rail**: every channel from #160, `#general` first, members by runtime icon and name with a working/idle/unverified state, a buddy channel's handover chain, and an alert badge when a broadcast has missing ACKs. No unread counts (there is no read cursor).
- **Conversation**: a channel's history entries, oldest first (the body comes from the history; #160 stores it for every entry), with journaled entries joined to their #157 ACKs and transport errors by message ID:
  - verdict rendering for messages that start with `APPROVE`, `REVISE: N` or `BLOCK` (verdict, text, pinned SHA when present);
  - a delivery row per recipient using #160's outcome names (`recorded`, `transport accepted`, `queued (thread paused)`, `not woken (OpenCode idle)`, `refused: <reason>`, `skipped: gone`, `in progress`, `unknown`, `unverified`, `acked`);
  - ACK tallies on `@all` broadcasts;
  - each recipient's own transport error, when #157 recorded one for that recipient slot;
  - `left`, `gone` and `handed over to X` entries.
- **All channels**: every channel's entries merged by time, each tagged with its channel.
- **Details panel**: each member's runtime, state, typed address and, for a Codex member, its reply budget toward its current requester.
- **Coverage banner** on every channel that has a Claude Code member, and on All channels: native Claude-to-Claude messages are not in this view unless their sender recorded them.
- M2 adds light/dark themes, search and a phone layout.

The page never sends, never ACKs, never marks anything and never changes any session-peers state. It reads the journal through #157's same-user read-only reader.

Acceptance examples:

1. With two channels and four sessions, `peers.py web` prints `http://127.0.0.1:<port>/#token=<t>`. `GET /` and the static assets load without the token; `GET /api/state` with the token returns metadata only (sessions, channels, members, latest cursor); `GET /api/channel/<key>?before=<seq>&limit=200` returns one bounded page of a channel's history.
2. `GET /api/changes?since=<cursor>` returns nothing new; after a fixture `send --to '#ops'`, the same call returns the new entry with outcome `transport accepted`; after a fixture `ack`, it returns the outcome change to `acked`. (Tested on the cursor, with no sleeps.)
3. `/api/*` without the token, with a wrong token, with a non-GET method, or with a `Host` other than `127.0.0.1:<port>` or `localhost:<port>` returns 403 (405 for the method) and no data.

## Mock contract

- The mock is the presentation reference: layout, rail, conversation, verdict bands, delivery row, ACK tally, details panel, icons, colour roles, wording. Implement it, not only this text. Where this PRD's text specifies, the text wins.
- Small deviations for consistency are expected.
- **Required deviations**:
  - The mock's four-step ladder (Queued, Delivered, In turn, ACK) becomes #160's outcome names. "In turn" has no evidence today (#159).
  - The mock's per-message "Not journaled" row for native messages is dropped: such messages are absent from the data. The coverage banner states the limit instead.
  - Unread badges and the "Open gates" card are dropped (no data source).
  - The composer and `@all` warning belong to #162.
  - "Ping" and "Show shim log" are dropped (read-only page); "Copy address" stays (client-side only).
  - System fonts and separate CSS/JS files instead of Google Fonts and inline code, as the CSP requires.
- Any other larger deviation is listed in the PR body under `Mock deviations` with its reason. An absent list claims parity.
- **Visual parity is a landing check, not an in-run one.** The offline worker proves behaviour with tests; the landing session compares the result with the mock side by side (screenshots) and adds them to the PR before merge.
- Icons: the Claude mark (simple-icons, CC0-1.0) and the OpenAI mark for Codex (Bootstrap Icons 1.13.1, MIT) as inlined in the mock; OpenCode uses the mock's placeholder glyph. Add the Bootstrap Icons MIT notice in a new `skills/session-peers/THIRD_PARTY_NOTICES.md`.

## Out of scope

- Correlated `ask`/`dispatch` requests and replies and their settlement (`replied`, `consumed`): they are journaled but have no channel-history entry, so no channel conversation shows them. A journal-backed view with its own paging is a later PRD. Wherever a settlement is shown it stays distinct from ACKs and is never counted as one.
- Sending, `@all` from the page, the browser identity (#162).
- Raw session transcripts.
- Remote access (loopback only).
- Delivery into a running turn (#159).

## Modules and seams

- **Web server** (new, `session_peers/web.py`): Python standard library `http.server`, threaded, bound to `127.0.0.1` on an ephemeral port (or `--port`).
  - Static shell: `GET /`, `/app.css`, `/app.js` need no token and contain no data.
  - Data API: every `/api/*` route requires header `X-Peers-Token` equal to a random 32-byte token generated per start (compared with `hmac.compare_digest`). The token travels in the URL fragment, so it never reaches the server log or a `Referer`; the page reads it from the fragment and sends it as the header.
  - DNS rebinding: `Host` must be exactly `127.0.0.1:<port>` or `localhost:<port>` on every request, else 403.
  - Only `GET` exists; other methods get 405. Request bodies are refused.
  - Headers on every response: `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
  - Resource bounds: at most 16 concurrent connections (semaphore), history pages of at most 200 entries (the All channels view merges at most 200 entries across channels), `/api/state` never includes history, 10-second request timeout. Reads are bounded by the #157 and #160 caps.
- **Read model** (new, `session_peers/webdata.py`): pure functions that build every view's JSON from existing stores (registry, channels, history, journal through #157's read-only reader, ACKs, `budgets.py`). It computes everything with logic in it: verdict detection, outcome names, coverage flags, tallies, gone and unverified state, search matching. It holds no state and never writes.
- **Static assets** (`web/index.html`, `web/app.css`, `web/app.js`): rendering only. `app.js` builds the DOM through one helper that sets text with `textContent` and never uses `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`.
- **Live updates and cursors**: the page polls `GET /api/changes?since=<cursor>` every 2 seconds. The cursor is opaque to the page and encodes the last seen `seq` of each channel topic plus the #157 journal position. When a channel's seq is older than its oldest retained entry (pruned), the response carries `gap` for that channel and the page reloads that channel's latest page, showing that older entries were pruned.
- **Bounded reads**: the read model never scans a whole topic. It reads each topic from its end in chunks, at most 1 MiB per topic and 8 MiB in total per request, stopping once it has the entries it needs. The All channels view is paged by channel: each response covers the next batch of up to 32 channels in order of recent activity (by last `seq` and time from the topic index), at most 25 entries each, merged to at most 200, and stops early if the byte budget runs out. Every response states `partial: true` when channels were left out, the count of omitted channels, and a `next` token that continues with them; the page shows "Showing N of M channels" with a "Load more" control, and never presents a subset as all channels. A cursor older than what a bounded read can reach is answered with `gap`, as for pruning.
- **Bounded journal reads** (extends `journal.py`): `read_only_view()` parses the whole journal (up to its 16 MiB cap plus the 1 MiB event reserve), too much for 2-second polling. Add an incremental same-user reader beside it: it takes a journal cursor (last seen `seq`) and the **remaining byte budget** from `webdata.py`, reads from the journal's end in chunks within that budget, and returns the entries and events after the cursor, the bytes it used, the next cursor, and `gap` when the cursor's records are gone (a seq regression, a rewrite that pruned past it, or a budget too small to reach it). Like `read_only_view()` it takes no lock, repairs nothing and checks no identity. `webdata.py` charges those bytes to the same 8 MiB per-request total as the topic reads.

## Testing decisions

- Read model (Python, `peer_tests/`): every view from fixtures, including the outcome names, verdict detection, coverage flags, gone and unverified members per runtime, ACK tallies, and the cursor. Bounded reads at the configured caps, including the combined budget (a large journal plus large topics in one request stays within 8 MiB, and the journal reader stops at the budget it was given): 256 channels with large fixture topics, asserting bytes read per request stay within the limits (an instrumented reader), plus cursor and `gap` behaviour across many channels, and the All channels `partial`/omitted-count/`next` paging when the budget stops early.
- Server (Python): examples 1 to 3; headers; paging; the connection cap; static routes carry no data.
- Rendering safety (two layers): a test that fails if `app.js` contains `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write`; and a `node --test` test (the runner is already in `.github/workflows/test.yml`) that loads the render helper with a minimal stub document and asserts a fixture string containing `<img src=x onerror=…>` and control characters ends up only as text.
- Visual parity: the landing check above.

## Milestones

### M1: sessions, channels and history, read-only

`peers.py web` with the server security, the read model, the rail, conversation with verdict bands, outcome rows, membership entries and ACK tallies, the details panel and coverage banners; `SKILL.md` and `references/web.md`; `THIRD_PARTY_NOTICES.md`; tests.

- Blocked by: #157 and #160 merged.
- Acceptance: examples 1 to 3; rendering-safety tests.

### M2: all channels, live updates, search, themes and phone layout

The All channels view, 2-second cursor polling, search (computed in `webdata.py`), light/dark themes and the phone layout; tests.

- Blocked by: M1.
- Acceptance: example 2; search and cursor tests.

## Decision Log

- **Part of session-peers, not a separate service.** It reads session-peers' own stores and adds no transport. Rejected: adopting swarmchat (assessed 2026-11-06: swarmchat core's delivery is pull-only (its ProgramBench adapter injects only into headless agents it launches itself), an extra server and token lifecycle, duplicate records).
- **Static shell public, data API token-gated.** A fragment token cannot authenticate the first page load; the shell holds no data.
- **The page reads every journal entry through #157's same-user read-only reader.** The same OS user can already read those files; the exception is explicit, read-only and never ACKs.
- **Logic in Python, rendering in JS.** Keeps the testable surface in the existing Python test setup and the JS small.
- **Outcome names shared with #160**, never a richer ladder without evidence; "transport accepted" read as "read by the model" is the failure this work exists to fix.
- **No unread counts**, because no read cursor exists for a viewer.
- **Polling, not long-poll or WebSocket**: simpler server, fast enough for a person.
- **Visual parity at landing**: the offline worker has no browser; tests carry behaviour.
- **Blocked by #157 and #160**, because the page renders their records.
- **Incremental journal reader with a caller budget** (2026-11-06, after #157 merged): `read_only_view()` reads everything; a budget-taking reader keeps one per-request byte limit across journal and topics. Rejected: polling `read_only_view()`; a separate journal budget on top of the 8 MiB.
- **Per-recipient transport errors shown apart from ACKs**: #157 records them separately. Correlated requests and their settlement are deferred (no channel source). Rejected: a journal-backed request feed in this PRD.
