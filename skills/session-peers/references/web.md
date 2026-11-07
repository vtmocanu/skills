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

- The server listens on loopback only and accepts GET only; it is read-only.
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

- **Channels rail.** `#general` first, then stored channels. Each lists its
  members with runtime icon and name and a state: working, idle, unverified or
  gone. A handover chain (buddy channels) and a `missing ACKs` (or `ACKs
  unverified`) badge cover the latest 25 entries of the channel; `unverified`
  appears when the read budget ran out before the ACK state could be
  established.
- **Conversation.** Entries oldest first. A verdict message shows a band
  (`APPROVE`, `REVISE: N`, `BLOCK`) with its pinned SHA. A send lists one row
  per recipient with the [channels](channels.md#outcomes) outcome
  (`transport accepted`, `queued (thread paused)`, `not woken (OpenCode idle)`,
  `skipped: gone`, `refused: <reason>`, `recorded`, or `acked` once the ACK is
  journaled) and the send's own state (`recorded`, `complete`, `in progress`,
  `unknown`, `unverified`). An `@all` post adds an ACK tally and names the
  missing recipients (refused and gone recipients, whose ACK was never
  requested, are outside it); a recipient's journaled transport error is shown on its
  row. Membership changes (joined, left, removed, handover, created) are
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
- **What it touches.** A non-blocking probe of each channel's per-send lock,
  pidfile probes, `ps`, `lsof` and `opencode` subprocesses, and the Codex
  state SQLite opened read-only (opening a WAL database read-only may touch its
  `-wal`/`-shm` sidecars). It does not create the session-peers state
  directories, rewrite a topic log, or take a store or journal lock.

Peer-written names, addresses and message bodies are shown with control,
bidirectional and hidden characters escaped (`\uXXXX`, or `\UXXXXXXXX` beyond
U+FFFF); bodies keep newlines, tabs and real emoji sequences.
