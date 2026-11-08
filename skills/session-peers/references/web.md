# Web dashboard

`peers.py web` serves a local, read-only browser view of the channels, their
history, who is in them and the ACK state of recorded messages. It reads the
channel store and the [journal](journal.md); by default it never sends, ACKs,
marks, hands over or changes anything, and it creates or changes no
session-peers file. `peers.py web --chat` is the one exception: it registers a
browser peer and sends on purpose, see [Chat mode](#chat-mode---chat). The
rest of this page, up to that section, describes the default mode.

## Start it

Run `<this skill's directory>/scripts/peers.py web [--port N]`. `--port` is `0`
(default, any free port) or 1024-65535; anything else exits 2, and a port that
cannot be bound exits 1. After binding, it prints one line and serves until
SIGINT or SIGTERM:

```
http://127.0.0.1:<port>/#token=<token>
```

Open that exact URL in a browser on the same machine. Remote access is not
supported: the server listens on `127.0.0.1` only.

- The server listens on loopback only and, without `--chat`, accepts GET only; it is read-only.
- The token is new on every start and travels in the URL fragment (never sent
  to the server or logged); the page keeps it in memory and sends it as
  `X-Peers-Token`.
- A reload or a copied link without the fragment cannot read data (403).

The loopback bind keeps the server off the network, but any local user or
process on the machine can reach the port; the per-start token is what guards
`/api/*`, so anyone who obtains it (for example from the printed URL or the
browser history) can read the API. Neither defends against a hostile process of
the same user. Connection, header and read-budget details are in the
[runtime package README](../scripts/session_peers/README.md#dashboard-server).

## What the page shows

- **Channels rail.** `#general` and the Browser lane first, then stored
  channels. A buddy channel is shown by its label (the pair's names, for
  example `#lead-buddy`), the same string as in [`channel list`](channels.md#buddy-channel-labels),
  and the header shows it too; the `#buddy-<hash>` key is still what the page
  posts to. A label is marked `stale` when there is no current buddy
  or a pair member is gone, and `pair unverified` when the record does not
  establish the pair; the rail, the header and the details show the same
  marker. Each channel lists
  its members with runtime icon and name and a state: working, idle, unverified or
  gone. A handover chain (buddy channels) and a `missing ACKs` (or `ACKs
  unverified`) badge cover the latest 25 entries of the channel; `unverified`
  appears when the read budget ran out before the ACK state could be
  established.
  - **Order.** `#general` and the Browser lane are pinned. The other channels
    follow by their newest send (newest first), then channels with no posts,
    then channels whose activity is unknown; ties break by label, then key.
    Only sends count (not joins, leaves or handovers), so a membership change
    does not move a channel (an oversized entry, or a send pushed out of the
    read window, can make a channel's activity unknown, which does move it). A channel whose newest send is stamped more than
    300 s in the future counts as unknown. The channel you selected stays
    selected and is scrolled into view whenever the rail reorders, on a poll or a
    state refresh.
  - **Activity unknown.** A channel with no posts shows nothing; the
    "activity unknown" note appears only when its newest send could not be established: the read
    did not reach one within its bounds, met an oversized entry that may hide a
    newer send, or was skipped or answered with a gap on a poll
    (`activity_unknown` in `/api/changes`).
  - **Renames.** A session rename shows on the next state refresh after the
    next discovery pass (discovery is cached about 5 s), not on the next poll.
- **Conversation.** Entries oldest first. A verdict message shows a band
  (`APPROVE`, `REVISE: N`, `BLOCK`) with its pinned SHA. A send lists one row
  per recipient with the [channels](channels.md#outcomes) outcome
  (`transport accepted`, `queued (thread paused)`, `not woken (OpenCode idle)`,
  `skipped: gone`, `refused: <reason>`, `recorded`, or `acked` once the ACK is
  journaled) and the send's own state (`recorded`, `complete`, `in progress`,
  `unknown`, `unverified`). An `@all` post adds an ACK tally and names the
  missing recipients (refused and gone recipients, whose ACK was never
  requested, are outside it); a recipient's journaled transport error is shown on its
  row, except one that only repeats a `refused: <reason>` (the reason shows once). Membership changes (joined, left, removed, handover, created) are
  entries too.
- **Details panel.** Runtime, state and the typed address of a member. `Copy
  address` is active only for a strict typed id (`cc:`, `codex:`, `oc:` form);
  otherwise it is disabled. A Codex member's reply budget is labelled "as last
  saved by the shim" (read from its state file; unknown when the thread's home
  is not known).
- **Coverage banner.** Native Claude-to-Claude messages (`SendMessage`) are not
  in this view unless their sender recorded them; the banner says so for a
  channel with a Claude Code member. On `#general` the banner also shows the
  current discovery's OpenCode coverage and discovery gaps; each `#general`
  post shows its own recorded `opencode_coverage` and `discovery_incomplete`
  under that post.
- **All channels.** Newest entries of many channels merged by time, read in
  batches. It shows "Showing N of M channels" with `Load more`, and an
  "Out of date" note when a new channel or a gap means the loaded set no longer
  covers every channel. It also appears for channels that arrive while the view
  is loading and after a cursor reset.
- **Search.** Case-insensitive substring search (1-256 characters) of body,
  actor and member text in each channel's newest 200 entries, in batches with
  `Load more`.
- **Live.** The page polls every 2 s (backing off after failures), remembers a
  light or dark theme in `localStorage` (its only stored value; the default
  follows the system), and has a phone layout.

`transport accepted` never means the model read the message; only an ACK
(`inbox acks <id>`) does. A correlated `ask`/`dispatch` request and its
settlement are journaled but are not channel history entries, so the page does
not show them.

## Limits and scope

Everything is bounded; a request that hits a bound says so in the page
(`partial`, `unverified`, a gap) instead of showing a silent omission.

| Bound | Value |
| --- | --- |
| Read budget per request | 8 MiB across history, channel records, journal and send state. |
| Per channel | 1 MiB read from the end of its history. A request never returns nothing for want of bytes alone: one complete maximum-size entry can raise one channel's read up to the entry line cap while the request budget holds it. |
| History page | At most 200 entries (`limit`, `before`). |
| All channels / search batch | 32 channels per batch, newest 25 entries each (search: 200), All channels merged to the newest 200. |
| Journal | Read from its end within the remaining request budget. When the read could not reach some message's record, the page notes that ACK state was read from the newest part of the journal only, and those ACKs show as `unverified`. An early stop that found every wanted ACK adds no note. The 2 s poll reads at most 2 MiB of it. |
| Poll cursor | Opaque, at most 48 KiB. Only `#general` and the first 256 stored channels are tracked; others show "not live-tracked; reload the page to see them". |

- An entry too large to read whole shows "Entry too large to display". History
  pruned by retention or rewritten shows a gap and the latest page.
- **Discovery** (who is live, working or idle) runs on one background thread,
  its result cached 5 s. A pass starts no new member check after 8 s and
  checks at most 64 Codex or OpenCode sessions; members not reached are
  `unverified` and the page notes "discovery stopped early". A check already
  running (`lsof`, an OpenCode API call) can overrun the 8 s. A request waits
  at most 7 s, then serves the last result as stale or an empty pending one.
- **Labels and order.** Labels are computed on the discovery thread over at
  most 256 stored channel records (each read with its own budget), so a request
  adds no lookups. Pair identities (a buddy channel's lead and current buddy)
  are checked before any other member: liveness first, then OpenCode name
  lookups, which share the 64-check allowance, are capped at 32 per pass and stop
  at the 8 s budget. So those limits reach labels only when pair identities
  alone exceed them (for example more than 64 non-`cc` pair identities, or a
  slow pass). An identity a pass could not check is checked first on the next
  pass, except that one whose lookup failed goes last, so it cannot starve the
  others. A pair identity a pass could not check (out of time, a failed or
  skipped OpenCode lookup) keeps the previous pass's name and liveness if there
  is one, else its short id and `unverified` liveness; either way the pass is
  reported incomplete (`discovery stopped early`). A deleted OpenCode session
  (reported not found) drops its name and does not mark the pass incomplete; an
  OpenCode session that exists but has no title counts as checked (short id,
  pass not incomplete). Labels for a channel newer than the last discovery
  pass, or before the first pass, are derived on the request thread without
  lookups, from the names and liveness the last pass looked up for labels:
  an identity that pass used in a label keeps its name; any other identity
  (and everything before the first pass) shows a short id and is never marked
  stale until the next pass. Finding the newest send continues past the 25-entry
  alert page within the channel's 1 MiB read, reading at most 256 KiB more per
  channel and, across all tracked channels, about a quarter of what is left of
  the 8 MiB request; a channel it cannot settle within that is activity
  unknown.
- **What it touches.** A non-blocking probe of each channel's per-send lock,
  pidfile probes, `ps`, `lsof` and `opencode` subprocesses, and the Codex
  state SQLite opened read-only (opening a WAL database read-only may touch its
  `-wal`/`-shm` sidecars). It does not create the session-peers state
  directories, rewrite a topic log, or take a store or journal lock.

Peer-written names, addresses and message bodies are shown with control,
bidirectional and hidden characters escaped (`\uXXXX`, or `\UXXXXXXXX` beyond
U+FFFF); bodies keep newlines, tabs and real emoji sequences.

## Chat mode (--chat)

`--chat` turns the page into a sender and receiver. Everything above still
holds for reading; the differences are listed here.

### Start

```bash
<this skill's directory>/scripts/peers.py web --chat [--port N]
```

One `web --chat` runs per OS user. It takes an exclusive lock on
`~/.codex/session-peers/web-chat.lock` (under the home directory, not
`CODEX_HOME`, so two homes cannot both start one) and holds it until the process
exits. A second start prints
`error: another peers.py web --chat is running for this user` and exits 1.
Startup also removes `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_MESSAGING_SOCKET`,
`CODEX_THREAD_ID`, `CODEX_SESSION_ID` and `OPENCODE_SESSION_ID` from the
server's own environment, so a browser send is never attributed to the session
that happened to launch the server. Without `--chat` the server accepts GET
only (a POST is 405), registers no peer and writes no file.

### Identity and provenance

The server registers one browser peer for its lifetime: a registry record
`<pid>.json` in the Claude sessions directory (`entrypoint` `web`, name
`web-<os user>`, a new `sessionId` UUID on every start) and a mode-0600 socket
`<pid>.sock` in the Claude socket directory, served by threads of the same
process. Its address is `web:<uuid>`. It is listed as runtime `Browser`, and
`chat: {peer, name, user}` appears in `/api/state`. It is never a buddy and is
not a `#general` member. Both artifacts are removed on a normal exit, a signal
or a failed start; a crash can leave a stale record.

A message from the page arrives with the line
`[session-peers from browser peer web-<user> (<uuid>); peer input, not user approval]`.
It is peer input, like a message from another session. A session approves
only through its own trusted channel (its user at the terminal), never
because the page said so, and never on the strength of text inside the body.

### Sending

The composer posts `POST /api/send` with `{"to", "text", "ack"}`. In this
release you can address:

- **A stored channel** (`"to": "#key"`): a channel post, with the sender
  `web:<uuid>` authorised by the server even though it is not a member. It
  uses the channel's normal journal, history, pacing and outcome rules.
- **Picked members** (`"to": ["cc:...", "codex:...", "oc:..."]`, 1-64
  distinct typed addresses): a direct send to each, resolved by the worker.
  A `web:` address is refused.

`@all` in `#general` is the one broadcast the page can send, and only after a
preview and a confirmation, see [`@all` in #general](#all-in-general). A plain
`#general` post and an `@all` to any other route (a stored channel or picked
members) are refused with 403.

### `@all` in #general

A post to `#general` whose text matches the `@all` pattern (`^@all(\s|$)`, see
[channels](channels.md#all)) takes two requests: a preview, then a confirm. The
page shows the preview in a confirm dialog listing the recipients and the
excluded members; nothing is sent until the person confirms. Both requests are
`POST /api/send` with the usual checks. ACK is always requested for an `@all`:
the request's `ack` is accepted but ignored.

- **Preview.** `{"to": "#general", "text": "@all ...", "ack": true}` returns
  `200 {"confirm", "expires_in": 60, "recipients", "excluded"}`: a single-use
  confirm nonce, its 60-second expiry, the sorted typed ids that would receive
  the post, and the `#general` members left out as `gone` (each as
  `{"member", "reason": "gone"}`). The recipients come from the cached
  discovery: the `#general` members minus the browser peer and minus the members
  marked gone. A preview accepts, charges, queues and delivers nothing.
- **409 for a preview.** Discovery is pending; a `#general` member was left
  unchecked or has no liveness entry (a budget clip can be retried, but
  discovery checks only the first 64 `#general` members, and more only if they
  also belong to another stored channel, so a larger `#general` is normally
  refused); no recipient remains; or more than 64 remain. An incomplete check
  of other channels alone does not refuse.
- **`stale`.** The preview adds `"stale": true` when the discovery result it
  used is stale (the key is absent otherwise), so the page can say so.
- **Confirm.** The same body plus `"confirm": "<nonce>"` (exactly these four
  keys; the nonce is a string of 1-128 characters and only valid with
  `#general` and an `@all` text, else 400). The nonce is bound to the route, the
  browser peer, the exact recipient set, a hash of the message text, ACK
  (always requested) and the expiry. It is checked and consumed atomically, so
  it works once.
- **Confirm refusals.** 409 for a nonce that is unknown, expired (an expired one
  is dropped), already used or bound to different text. A text mismatch leaves
  the nonce usable.
- **429.** The rate limit or a full queue: the nonce stays usable and nothing is
  charged; confirm again with the same body.
- **202 queued.** `202 {"message_id", "state": "queued"}` consumes the nonce
  and charges the send, as for any accepted send. The worker then sends to
  exactly the previewed set. If the live members (the `#general` members not
  marked gone) differ from it, the row reads `not sent: recipients changed, preview again` and the person
  previews again. A live member the journal cannot record becomes a recorded
  `refused: <reason>` outcome with ACK not requested, not a "recipients
  changed" failure. A worker failure still spends the attempt and the charge.
- **Rate limit.** At most one `@all` per sender per 60 seconds, through the
  existing per-sender allowance ([channels](channels.md#all)); a second one in
  that window reads `not sent: at most one @all per sender every 60 seconds ...`.
  A restart gives the browser peer a new identity, and so a fresh allowance.
- **Still refused.** A plain `#general` post and an `@all` to a stored channel
  or to picked members stay 403.
- **Restart.** Nonces are kept in server memory only (at most 32, the oldest
  evicted), so a restart invalidates every outstanding one: preview again.

**How `@all` works.** The post goes to the members discovery found for
`#general`. A member that is gone is excluded at preview time and shows as
`skipped: gone`; a member that refuses shows its `refused: <reason>`; an idle
OpenCode session is not woken (`not woken (OpenCode idle)`); and discovery gaps
are shown under the post. Each shows as its recorded outcome, and nothing
outside the previewed set is sent to.

### The 202 queued contract

`202 {"message_id", "state": "queued"}` means the send was accepted in the
server's memory and nothing else: it is not recorded and not delivered.
One worker thread then handles accepted sends in order: it resolves the
recipients, journals the message, delivers and sets the final state. The
request thread never resolves recipients or writes the journal or history. The
state shows in the page's status row for that `message_id`:

| State | Meaning |
| --- | --- |
| `queued` | Accepted in memory; nothing written yet. |
| `pending` | Journaled and delivered or attempted. Each recipient shows its outcome; each recipient's ACK reads pending until acked (or `ACK not requested`). `delivering` is true while the worker is still delivering. When every recipient is `refused: ...` or `skipped: ...` (none ACKed, none `refused: outcome unknown`), the row reads "No delivery confirmed: every recipient was refused or skipped" in the bad tone instead of "Recorded". It does not claim nothing arrived: a `refused:` outcome can follow a send whose write had already begun. The ACK tally reads "N of M acknowledged", plus "(K unverified)" when some pending recipients are unverified. |
| `not sent: <reason>` | Refused before any write (journal full or unavailable, an unresolvable recipient, oversized text, an unusable channel). The id never gets a journal entry. |
| `not sent (journaled)` | A channel post was journaled but its history append failed; nothing was delivered or asked of anyone. |
| `failed after recording` | The worker hit an unexpected error after the message was journaled. Check the recipients before resending. |

Nothing is retried, because an ambiguous send may already have been delivered.
A queued send is lost if the server stops; a send cut short by a restart keeps
only what the journal or channel history recorded (a channel post then reads
`unknown`; a direct send's row is gone with the old feed). A worker failure
still uses the attempt's rate charge.

### Limits and refusals

| Limit | Value |
| --- | --- |
| Rate | 1 accepted send per second, 30 per rolling minute, per server. |
| Queue | 8 sends queued or running; more get 429. |
| Body | 1 MiB (`Content-Length`); text 1-1048576 characters. |
| Recipients | 1-64 distinct typed addresses, or one channel key. |

A 429 accepts and charges nothing. Every refusal has an empty `{}` body:

| Code | Refused because |
| --- | --- |
| 400 | Bad JSON or fields (duplicate keys, extra fields, a numeral over 32 characters, text that is not valid UTF-8 such as a lone surrogate, a `web:` recipient), any `Transfer-Encoding`, a query string on `/api/send`, or a missing, repeated, non-numeric or zero `Content-Length`. |
| 403 | Host, token or `Origin` rejected (`Origin` must be `http://` plus the Host), a plain `#general` post, or an `@all` to any route but `#general`. |
| 404 | A GET to an unknown path, or a malformed request target. |
| 405 | A POST to a path other than `/api/send`, a method other than GET or POST, or any POST when `--chat` is off. |
| 408 | The body was short or arrived too slowly. |
| 409 | Only for `@all` in `#general`. A preview: discovery pending, a `#general` member unchecked or without a liveness entry, no recipients, or more than 64. A confirm: the nonce is unknown, expired, used or bound to different text. |
| 413 | `Content-Length` over 1 MiB. |
| 415 | `Content-Type` is not a single `application/json` (charset utf-8 allowed). |
| 429 | Rate limit hit or queue full (on a confirm the nonce stays usable). |
| 503 | Chat has not finished starting. |

### Ask for ACK

The composer's "Ask for ACK" sets `ack`. The message is journaled either way.

- **On:** the recipient is told how to ACK and its ACK reads pending until
  `inbox acks` shows it.
- **Off:** the recipient gets no ACK instruction and the row reads `ACK not
  requested`, also after a reload (a channel post keeps the choice in its
  history entry; a direct send in the server's memory). The recipient's own
  journal row still reads `pending`.

### Replies

A reply comes back to the browser peer's socket and lands in the Browser feed
with its sender and name, verified when the sender is a Claude Code session
or a Codex shim (an OpenCode, shell or direct Codex `peers.py send` shows as
an unverified sender):

- **Claude Code:** native `SendMessage` to `web-<user>`.
- **Codex:** the shim routes the thread's final answer. It counts against the
  thread's per-requester [reply budget](budgets.md) at the default cap; raise
  it with `peers.py budget allow`. The details panel shows the replies left.
- **OpenCode:** `peers.py send --to web:<uuid>` (see
  [messaging](messaging.md#sending-to-the-browser-peer)).

A reply is shown only after the server journaled it. The browser peer writes
nothing back on a native sender's connection (Claude Code fails a send that
gets any answer), so native `SendMessage` reports success once the frame is
written, before the journal write. If the journal then refuses it (full or
unavailable), nothing is shown and the sender is told afterwards: a
`peer_message_status` of `refused` and a one-time notice. Only
`peers.py send --to web:` waits for an answer line on its connection.

The feed takes its provenance from the verified sender, never from the body;
an unverified sender shows as such. The body's leading origin line
(`[session-peers from ...]`) naming that verified sender's id, plus any tag and
`reply route` lines directly following it (at most 3 lines in all), are shown
as one muted line above the text; any other text stays in the body. "Verified" means the frame's claimed
reply socket belongs to a live registry record; the sending process is not
authenticated to that record. Any process of the same OS user can claim
another live session's socket, the same trust boundary as the rest of
session-peers (the socket's peer UID).

### The Browser feed

The Browser conversation is an in-memory log in the server: inbound messages
(after the journal write) and direct sends from the moment they are accepted.
Channel posts never enter it; they show in their channel. It holds at most
1000 entries and 4 MiB, oldest evicted first (the newest single entry is
always kept); an entry's text is clipped to
262144 characters for display (the journal keeps the whole message).

- **Reload:** the page gets the newest retained entries since this server
  started, with a note that the conversation starts at server start.
- **Gap note:** a poll or page that fell behind the oldest retained entry or
  an evicted status row (at most 1000 are kept; a send still in flight is
  never evicted), or lost
  entries for want of budget, shows a gap ("Earlier messages expired or were not shown.").
- **Hydration:** on a reload, outbound entries re-read their ACK state from
  the journal. A pruned message reads "expired from the journal". An
  unreadable journal or exhausted budget leaves a still-`pending` ACK
  flagged "ACK unverified" until a later definite fact (acked, expired or a
  transport error) or a later read that reaches the message and finds it still
  pending clears it. A recorded ACK is never lost.
- **Restart:** a new UUID, so a new identity, an empty feed and `restarted`.
  Status rows and queued sends do not survive; journaled ACKs and transport
  errors stay readable from the journal.

### Retention and quota

Browser messages and replies are ordinary [journal](journal.md#retention)
records. A browser peer does not ACK what it receives, so they stay until
pruned by the 14-day age cap, and count against their sender's quota like any
other recorded message, except that an unverified sender's inbox record counts
against the journal's unattributed (`null`) bucket. A delivered
`peers.py send --record --to web:...` message leaves two records, the sender's
own and the inbox's.

### While the server is down

There is no socket. A Claude `SendMessage` to the browser peer fails at the
sender; a Codex shim logs that the session that queued the turn is gone. Those replies are not delivered
to the page. `peers.py send --record --to web:<uuid>` is still journaled, with
a transport error. Without `--record` it is refused.

### Mixed versions

A session-peers copy that predates `--chat` does not know `web:` records and
drops them when it rewrites a journal it shares. Upgrade every copy that
shares the journal before using `--chat`.
