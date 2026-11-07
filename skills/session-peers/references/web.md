# Web dashboard

`peers.py web` serves a local, read-only browser view of the channels, their
history, who is in them and the ACK state of recorded messages. It reads the
channel store and the [journal](journal.md); it never sends, ACKs, marks,
hands over or changes anything, and it creates or changes no session-peers
file.

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

| Guard | Behaviour |
| --- | --- |
| Token | New on every start. It travels in the URL fragment (never sent to the server or logged), the page keeps it in memory only and clears it from the address bar, and sends it as `X-Peers-Token`. The static shell (`/`, `/app.css`, `/app.js`) loads without it; every `/api/*` route needs it (403 otherwise). A reload or a copied link without the fragment cannot read data. |
| Host | Must be exactly `127.0.0.1:<port>` or `localhost:<port>` (403 otherwise), as a DNS-rebinding guard. |
| Method | GET only (405); a request body is refused (400). |
| Connections | At most 16 at once; extra connections are closed unanswered. The request line and headers must arrive within 2 s, and every connection is cut after 10 s. |
| Headers | Every response carries a `Content-Security-Policy` (`default-src 'none'`, scripts and styles from `'self'` only, no framing, no forms), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` and same-origin resource/opener policies. Errors have an empty `{}` body. No request is logged. |

The token and the loopback bind limit access to one OS user's processes on this
machine; they are not a defence against a hostile process of the same user.

## What the page shows

- **Channels rail.** `#general` first, then stored channels. Each lists its
  members with runtime icon and name and a state: working, idle, unverified or
  gone. A handover chain and a `missing ACKs` (or `ACKs unverified`) badge
  cover the latest 25 entries of the channel; `unverified` appears when the
  read budget ran out before the ACK state could be established.
- **Conversation.** Entries oldest first. A verdict message shows a band
  (`APPROVE`, `REVISE: N`, `BLOCK`) with its pinned SHA. A send lists one row
  per recipient with the [channels](channels.md#outcomes) outcome
  (`transport accepted`, `queued (thread paused)`, `not woken (OpenCode idle)`,
  `skipped: gone`, `refused: <reason>`, `recorded`, or `acked` once the ACK is
  journaled) and the send's own state (`recorded`, `complete`, `in progress`,
  `unknown`, `unverified`). An `@all` post adds an ACK tally and names the
  missing recipients; a recipient's journaled transport error is shown on its
  row. Membership changes (joined, left, removed, handover, created) are
  entries too.
- **Details panel.** Runtime, state and the typed address of a member. `Copy
  address` is active only for a strict typed id (`cc:`, `codex:`, `oc:` form);
  otherwise it is disabled. A Codex member's reply budget is labelled "as last
  saved by the shim" (read from its state file; unknown when the thread's home
  is not known).
- **Coverage banner.** Native Claude-to-Claude messages (`SendMessage`) are not
  in this view unless their sender recorded them; the banner says so for a
  channel with a Claude Code member. On `#general` it also states what the
  post's OpenCode coverage was and any discovery gaps recorded with it.
- **All channels.** Newest entries of many channels merged by time, read in
  batches. It shows "Showing N of M channels" with `Load more`, and an
  "Out of date" note when a new channel or a gap means the loaded set no longer
  covers every channel.
- **Search.** Case-insensitive substring search (1-256 characters) of body,
  actor and member text in each channel's newest 200 entries, in batches with
  `Load more`.
- **Live.** The page polls every 2 s, remembers a light or dark theme in
  `localStorage` (its only stored value; the default follows the system), and
  has a phone layout.

`transport accepted` never means the model read the message; only an ACK
(`inbox acks <id>`) does. A correlated `ask`/`dispatch` request and its
settlement are journaled but are not channel history entries, so the page does
not show them. The static assets live in `scripts/session_peers/web/`.

## Limits and scope

Everything is bounded; a request that hits a bound says so in the page
(`partial`, `unverified`, a gap) instead of showing a silent omission.

| Bound | Value |
| --- | --- |
| Read budget per request | 8 MiB across history, channel records, journal and send state. |
| Per channel | 1 MiB read from the end of its history. A request never returns nothing for want of bytes alone: one complete maximum-size entry can raise one channel's read up to the entry line cap while the request budget holds it. |
| History page | At most 200 entries (`limit`, `before`). |
| All channels / search batch | 32 channels per batch, newest 25 entries each (search: 200), All channels merged to the newest 200. |
| Journal | Read from its end within the remaining request budget. When it cannot reach the start, the page notes that ACK state was read from the newest part of the journal only, and older ACKs show as `unverified`. The 2 s poll reads at most 2 MiB of it. |
| Poll cursor | Opaque, at most 48 KiB. Only `#general` and the first 256 stored channels are tracked; others show "not live-tracked; reload the page to see them". |

- An entry too large to read whole shows "Entry too large to display". History
  pruned by retention or rewritten shows a gap and the latest page.
- **Discovery** (who is live, working or idle) runs on one background thread,
  its result cached 5 s. A pass starts no new member check after 8 s and
  checks at most 64 Codex or OpenCode sessions; members not reached are
  `unverified` and the page notes "discovery stopped early". A check already
  running (`lsof`, an OpenCode API call) can overrun the 8 s. A request waits
  at most 7 s, then serves the last result as stale or an empty pending one.
- **What it touches.** A non-blocking probe of each channel's per-send lock,
  pidfile probes, `ps`, `lsof` and `opencode` subprocesses, and the Codex
  state SQLite opened read-only (opening a WAL database read-only may touch its
  `-wal`/`-shm` sidecars). It does not create the session-peers state
  directories, rewrite a topic log, or take a store or journal lock.
- Crash visibility covers process crashes only, not power loss. Memory worst
  case is about 16 connections times 8 MiB.

## Identifier hygiene

Peer-written names and addresses are shown with control, bidirectional and
invisible characters escaped as `\uXXXX` (`\UXXXXXXXX` beyond the BMP). Message
bodies keep newlines, tabs and emoji sequences, but hidden characters are
escaped: tag characters outside the three RGI subdivision flags (England,
Scotland, Wales), and joiners or variation selectors that do not sit between or
after the characters they belong to.

## Third-party icons

The Codex and Claude marks are inlined from third-party icon sets (the
OpenCode glyph is drawn for this project); see
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).
