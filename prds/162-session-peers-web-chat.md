# PRD #162: session-peers web chat

**Issue**: [#162](https://github.com/vtmocanu/skills/issues/162) | **Label**: PRD | **Priority**: Medium
**Umbrella**: [#163](https://github.com/vtmocanu/skills/issues/163) (child 3 of 3)
**Blocked by**: [#161](https://github.com/vtmocanu/skills/issues/161) (read-only dashboard). Do not start until it is merged.
**Design mock**: `prds/mockups/161-session-peers-web/index.html` (the composer, recipient picker, `@all` warning and confirm).
**Area**: `skills/session-peers/` (`web.py`, a browser peer, identity and provenance, `SKILL.md`, `references/web.md`).

## Problem

A person steering many sessions has to switch to each session's terminal to send it a message, and cannot post to a channel or to everyone from one place. Replies come back in each session's own terminal, so the conversation is scattered.

## Outcome

The #161 page gains a composer:

- Pick recipients in the current channel, or post to the channel, then send. In `#general`, a message starting with `@all` reaches every `#general` member after a confirmation listing the recipients.
- The browser user is a distinct peer with typed identity **`web:<uuid>`** (a UUID generated per server start), shown with the OS user name and its own icon. Every message it sends carries provenance stating it came from the browser and is **peer input, not user approval**. Sessions keep approving only through their own trusted channel.
- Replies come back to the page: Claude `SendMessage` to the browser peer, a Codex final answer routed by its shim, and `peers.py send --to web:<uuid>` from OpenCode all land in the browser peer's inbox and appear in the conversation.
- "Ask for ACK" requests a #157 ACK; the outcome row shows it when it arrives.

Acceptance examples:

1. In a channel holding L, the user selects L and sends "status?". L's fake socket receives it with browser provenance. A Claude-style reply to the browser peer's socket appears in `GET /api/changes`.
2. The user sends to Codex buddy X. X's fake shim routes the final answer to the browser peer's socket (the shim's requester check passes because the browser peer's record carries `sessionId = <uuid>`), and the reply appears in the page.
3. (M2) In `#general`, the user types `@all freeze main`. `POST /api/send` returns a confirm nonce bound to the sorted set of recipient typed identities, the message hash and a 60-second expiry. Confirming with that nonce sends to exactly that set and requests ACKs; reusing the nonce, or confirming after the set changed, is refused.
4. A `POST` without the token, from another `Origin`, with a non-JSON content type, over the rate limit, or (in M1) starting with `@all` or addressed to `#general` is refused and sends nothing.

## Mock contract

Same contract as #161 (presentation reference, text wins where it specifies, `Mock deviations` list in the PR, visual parity as a landing check). Composer-specific deviations:

- The mock's per-recipient checkbox picker stays; the mock's "Keep in journal" checkbox is dropped (browser sends are always journaled).
- The mock's local `confirm()` dialog becomes the server confirm round-trip in example 3, showing the recipient list, not only the count.
- The mock's simulated ladder and fake reply are replaced by real outcomes and replies.

## Out of scope

- Approving plans, granting budgets or any authority from the page.
- `@here` (idle-only broadcast).
- Remote access.

## Modules and seams

- **Browser peer** (new, `session_peers/webpeer.py`): lives in the web server process and registers like the Codex shim does (`shim.py` `record()`), with these differences:
  - Record: `pid` = the server's pid, `sessionId` = the per-start UUID, `name` = `web-<os user>`, `entrypoint` = `"web"`, socket in the Claude socket directory. Claude's sender checks that the process accepting on the socket is that pid, so the socket is served by threads in the same process, not forks.
  - Runtime: `protocol.sender_runtime` gains `web` for `entrypoint == "web"`, so provenance lines read "browser peer", never "Claude Code session". Identity resolution gains the `web:` kind; `web` is excluded from `BUDDY_KINDS`.
  - Every registry reader includes or excludes `web` on purpose, each with a test: `buddy.py` (excluded: never a buddy), `lifecycle.py` and `maintenance.py` (cleaned up like a shim record on exit), `storage.py` (listed with kind `web`), `codex.py` (a valid requester), `messaging.py` (a valid target), `diagnostics.py` (labelled `web`), and #160's derived `#general` (excluded; it is not a session).
  - One browser peer per OS user: the server takes an exclusive `flock` on a lock file in the state directory; a second `peers.py web` with chat enabled refuses with a clear error.
- **Inbox**: inbound messages to the browser peer are written to the #157 journal with the browser peer as recipient before the socket acknowledges them. If the write fails or the journal is full (#157's journal-full error), the socket acknowledgement is refused, so the sender sees a failed delivery, and nothing is shown as received.
  - While the server is down there is no socket: a Claude `SendMessage` fails at the sender, and a Codex shim logs "requester gone". Such replies are not delivered to the page; a sender that used `peers.py send --record` can still be read from the journal.
- **Send route** (extends `web.py`): `POST /api/send` with JSON `{to, text, ack}`. It journals and records first, then returns (`202` with the message ID); delivery runs in a background thread and outcomes arrive through `/api/changes`. On restart the server reconciles: every recorded browser send without a final outcome for a recipient gets the outcome `unknown` for that recipient, and nothing is retried, because an ambiguous send may already have been delivered. Request timeout 10 seconds.
  - Auth: the #161 token header, `Origin` equal to the page's own origin, `Content-Type: application/json` only. (No CSRF token: there are no cookies, and the token header already defeats cross-site requests.)
  - Resource bounds: body at most the existing 1 MiB cap; at most 1 send per second and 30 per minute per server; `@all` limits are #160's fan-out limits plus the confirm nonce here.
- **Codex reply budgets**: a Codex thread's replies to the browser peer count against the shim's existing per-requester budget, with the default cap. The details panel shows the remaining replies for the browser peer; the user can raise it with the existing `peers.py budget allow`.
- **OpenCode replies**: `peers.py send --to web:<uuid>` delivers to the browser peer's socket.

## Testing decisions

- Browser peer: record fields, socket accept in-process, pid check, the `flock`, cleanup on exit, provenance `web`, each registry reader's include/exclude test above.
- Inbox: journal-before-ack ordering; a failing journal write refuses the acknowledgement.
- Send route: examples 1 to 4 with fake routes; rate limits with an injectable clock; nonce binding, expiry and single use.
- Rendering safety: #161's tests extended to the composer and replies.
- Owner acceptance (not in-run, the landing session or user): how Claude Code's own `ListAgents` and `SendMessage` list and address the `web-<user>` record, checked on a real machine and noted in the PR.

## Milestones

### M1: send to sessions and channels, replies in the page

The browser peer, inbox, `POST /api/send` with its security (refusing `@all` and `#general`), the composer and recipient picker, "Ask for ACK", replies from all three runtimes; docs; tests.

- Blocked by: #161 merged.
- Acceptance: examples 1, 2 and 4.

### M2: `@all` with confirmation

The confirm nonce, the recipient-list confirmation and live ACK tally for browser broadcasts; lifting the M1 refusal; tests.

- Blocked by: M1.
- Acceptance: example 3.

## Decision Log

- **The browser user is a peer, not the user.** Claude Code labels cross-session messages as not typed by the user, and session-peers treats peer messages as information, never approval. Rejected: making web messages count as user input.
- **Register like the Codex shim**, so Claude sessions reply with native `SendMessage` and nothing changes on their side. Rejected: a pull-only browser mailbox.
- **A distinct `web` runtime**, because the existing code labels every non-Codex record as Claude, which would forge provenance.
- **`web:<uuid>` with a single instance per OS user**, because `web:<os user>` collides and two browser peers would split replies.
- **Journal before acknowledging**, so a reply the page cannot keep is never reported as delivered.
- **The `@all` nonce binds the exact set of recipient typed identities**, not a count, so membership changes between preview and confirm cannot redirect a broadcast.
- **M1 refuses `@all` and `#general`**, so every merged slice is safe on its own.
