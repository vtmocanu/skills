# Channels

A channel is a named member list with a shared history, for coordinating more
than two sessions on one machine. Members are typed identities (`cc:<uuid>`,
`codex:<uuid>`, `oc:<ses_id>`). Delivery reuses the per-runtime send routes;
nothing here is cross-machine.

## Where it lives

One store per OS user at `~/.codex/session-peers/channels/` (directories 0700,
files 0600). Like the journal, it is derived from `HOME` and never depends on
`CODEX_HOME`, so sessions under different homes see the same channels.
Ordinary [topics](topics.md) stay scoped to `CODEX_HOME`.

| Path | Holds |
| --- | --- |
| `<key>.json`, `<key>.lock` | Membership record of one stored channel (atomic replace) and its flock. |
| `.channels.lock` | Store lock, taken by `create` (the leading dot keeps it out of the name space). |
| `history/` | One topic log per channel, topic `channel:<key>`; the files are named by a digest of that topic. |
| `sends/<mid>.lock` | Per-send flock, held while a fan-out is in flight. |
| `left/` | Leave markers of derived `#general`, `<sha256 of typed id>.json`. |
| `allcast/` | `@all` allowance stamp and lock per sender, `<sha256 of typed id>.json`/`.lock`. |
| `grants/` | Handover receipts, `<sha256 from>-<sha256 to>.json`. |

## Commands

Run each as `<this skill's directory>/scripts/peers.py ...`. Identity comes
from the environment, as for the journal commands (an environment naming more
than one runtime is refused with the unset hint). Errors exit 2 for a refusal
(bad name, unknown channel, not the owner, over a cap) and 1 when a write
failed.

| Command | Effect |
| --- | --- |
| `channel create NAME [--member ADDR]...` | Create a channel you own; you are a member. `ADDR` is a typed id or a name (an ambiguous name is refused with candidates). |
| `channel join NAME` / `channel leave NAME` | Add or remove yourself. Idempotent (`already a member of`, `not a member of`). Buddy channels cannot be joined. |
| `channel remove NAME ADDR` | Owner only: the current owner (its creator, the lead of a buddy channel, or a handover successor). Accepts a typed id of a session that no longer exists. |
| `channel list [--json]` | Stored channels with kind, member count, your role (`owner`, `member`, `-`) and owner. |
| `channel members NAME [--json]` | Members with `live`, `unverified` or `gone` ([liveness](#liveness)); the owner is marked. |
| `channel tail NAME [--since SEQ] [--limit N] [--json]` | History after a cursor, ending with `next: --since N`; a cursor behind the oldest entry prints `gap: entries A..B pruned`. Default limit 20. `--json` adds a `view` (delivery state) to each send entry. |
| `leave` | Leave every stored channel you are in, then `#general`. |
| `handover --to TYPED` | Move your channels to a successor ([handover](#leave-and-handover)). |
| `channel join general` / `channel leave general` | Clear or set your `#general` leave marker. |

- Names are `[a-z0-9-]{1,64}`; `NAME` and `#NAME` are equivalent. `general` and
  names starting `buddy-` are reserved: `create` refuses them.
- At most 64 members per channel and 256 stored channels, each refused with an
  error naming the limit.
- The `.json` record is peer-writable, so it is not trusted: one over 256 KiB
  or malformed reads as absent, an invalid or repeated member is dropped, and a
  roster over 64 is refused for `members` and `send`, never truncated.
- Channel history keeps the topic retention (7 days, 1000 entries, 16 MiB, the
  [topics](topics.md) variables) and `gap:` reporting. Every entry records its
  sender; a message entry keeps the body, so history never depends on the
  journal for it.

## Sending

```bash
<this skill's directory>/scripts/peers.py send --to '#NAME' (--message TEXT | --message-file PATH) [--record] [--json]
```

- The sender must be a member of a stored channel (`channel join`) or, for
  `#general`, a [discovered session](#general). An unknown channel is refused
  (rc 2).
- `--wake`, `--allow-reply-wake`, `--in-reply-to` and `--from-*` are refused
  (rc 2): channel sends never wake and carry no reply route override.
- The body goes to history, then each member except the sender gets it in turn.
  Each delivery starts with `[channel #<name> message <mid> from <sender> to
  <recipient>]`, so no two deliveries are byte-identical; a journaled message
  adds the `inbox get`/`ack` pointer line after it. The body is peer-supplied
  data wrapped like any peer message.
- Claude deliveries are at least 1 s apart. A channel send never wakes an idle
  OpenCode session (its outcome is `not woken (OpenCode idle)`), including
  `@all`.
- `--record` journals the message first as one [journal](journal.md) entry
  naming every deliverable recipient, so each can `ack` it. Check with
  `inbox acks MSG_ID`; the sender is not in the tally.
- Before anything is journaled the wrapped size is checked: the Claude wrapper
  limit (measured with the largest Claude or OpenCode framing) applies to every
  post with any recipient, and the Codex argv budget only when a Codex member
  is addressed. An oversize message exits 1 with nothing journaled or sent. The
  1 MiB cap applies as for `send`.
- One member's failure is recorded and does not stop the rest.

### Outcomes

One outcome per recipient, written once to history as a single `channel.outcome`
entry when the fan-out finishes (a 64-recipient post costs two entries):

| Outcome | Meaning |
| --- | --- |
| `transport accepted` | The route accepted it. Proves transport only, never that it was read. |
| `queued (thread paused)` | Codex queued it for a paused thread. |
| `not woken (OpenCode idle)` | Left readable for an idle OpenCode session; nothing woke it. |
| `skipped: gone` | The member was [verified gone](#liveness). |
| `refused: <reason>` | The route or the journal refused that member (for example a Codex thread whose liveness cannot be checked). |
| `recorded` | `--record` on `#general`: journaled, nothing delivered. |

A journaled member the journal cannot address is `refused: <reason>` with the
note `ACK not requested (unverified)`. If no recipient is journalable, exit 1
with nothing recorded or sent.

`channel tail` also shows a delivery state per send, read from history, the
per-send lock and the journal:

| State | Meaning |
| --- | --- |
| `recorded` | Nothing was to be delivered. |
| `complete` | The outcome entry exists; each recipient shows its outcome, or `acked` once its ACK is recorded. |
| `in progress` | The sender still holds the send lock. |
| `unknown` | No outcome entry and the lock is free: the sender is gone mid-delivery. Never retried; check recipients before resending. |
| `unverified` | The lock or history could not be checked. A journaled recipient's ACK also reads `unverified` when the journal cannot be read. |

Exit codes of `send --to '#NAME'`: 0 every recipient accepted; 1 any recipient
refused (`status` `partial` or `failed` in `--json`), or a size, journalability
or history failure; 2 refused flag, name, non-member sender or `@all` limit; 75
journal full and 1 journal unavailable (both before anything was sent); 76 a
journal update after delivery failed, so read the printed outcomes and never
resend blindly (see [the journal](journal.md#exit-codes)).

## Liveness

Gone members are skipped; a member that cannot be proven gone stays a member
and is delivered through its route, whose refusal is the outcome.

- Claude: `gone` when the registry has no record or every record is `dead`
  (`claude.py`); `live` when one is live; otherwise, or when the registry cannot
  be read completely, `unverified`.
- Codex: the existing held/not-held check on the thread's rollout writer lock:
  held is `live`, not held is `gone`; an unresolvable, degraded or unchecked
  thread is `unverified`.
- OpenCode has no dead state. `gone` needs both a complete `session.list` page
  walk that omits the session and a not-found answer for that exact session (a
  child session is absent from the top-level walk yet exists). An incomplete or
  failed walk, or any other error, leaves it `unverified`.

## `#general`

Derived, never stored: no member record, no cap. Its members are the sessions
`peers.py list` discovers (Claude live or unverified, Codex threads live or
unknown, OpenCode top-level sessions updated within
`SESSION_PEERS_OPENCODE_RECENT_HOURS`, default 24) minus sessions that ran
`leave`. Only Claude, Codex and OpenCode sessions are discovered, so any other
kind of peer (such as a browser peer) is excluded by construction.

- A plain post is recorded only: readable with `channel tail general`, not
  delivered.
- `--record` journals it for the members the journal can address (at most 64;
  more is refused before anything is journaled) and delivers nothing; each
  shows outcome `recorded`.
- The sender must itself be a discovered session; a session that has left may
  still post. If OpenCode discovery failed the refusal says so for `oc:`
  senders.
- Every `#general` post prints the coverage statement, and `--json` carries
  `opencode_coverage`:

  ```text
  OpenCode coverage: top-level sessions updated within the last 24 h (as peers.py list)
  note: OpenCode was not enumerated: <reason>
  ```

  The second line replaces the first when the OpenCode listing failed: the
  member list then covers Claude and Codex only. An OpenCode session outside
  the window is not a member, so do not read `#general` as "every session".
  The line is printed for `#general` posts, `@all` there included, and for
  `channel members general`; not for `@all` on a stored channel, whose explicit
  member list involves no enumeration.

## `@all`

A body starting `@all` followed by whitespace or the end (`^@all(\s|$)`), on
`#general` or a stored channel, is a broadcast: it is always journaled
(each recipient is asked to `ack`) and delivered to every member except the
sender.

- At most 64 recipients per post; over that it is refused (rc 2) before
  anything is journaled.
- One `@all` per sender per 60 s across all channels (rc 2, with the seconds to
  wait). The allowance is spent when the post is reserved, before the journal
  write, and restored if the journal refuses it.
- A future-dated stamp (after the clock stepped back, or a planted file)
  refuses until 60 s after the stamp time. To recover sooner, delete
  `~/.codex/session-peers/channels/allcast/<sha256 of your typed id>.json`.
- The limits live in the fan-out, so a CLI caller is bounded too.

```bash
<this skill's directory>/scripts/peers.py send --to '#general' --message '@all freeze main until the release is tagged'
<this skill's directory>/scripts/peers.py inbox acks MSG_ID
```

## Leave and handover

- `peers.py leave` removes the caller from each stored channel it is in,
  appends `left`, then writes the `#general` leave marker (the session stays
  live; derived `#general` omits it). Ownership is kept. A failure on one
  channel is reported, the rest still run, and the exit is 1. A session that
  already left everywhere exits 0. This includes the lead's own buddy channel:
  `buddy set` only adds the buddy and never re-adds a lead that left, and
  `channel join` is refused for a buddy channel, so there is no command to
  rejoin: a lead that left stays out (it still owns the channel, but cannot
  post to it).
- `handover` is two commands in order: `peers.py inbox handover --to TYPED`,
  then `peers.py handover --to TYPED`. The second needs that grant recorded and
  never creates one: otherwise it exits 2 and prints the `inbox handover`
  command to run first. It accepts a live journal grant or a receipt under
  `grants/` younger than 14 days (the journal may prune the grant sooner than
  that; `inbox handover` writes the receipt, and warns only if that fails).
  The successor must be a full typed id, not a name.
- In each stored channel the caller is a member of or owns (including one it
  left), the successor is added, the caller removed, ownership moved and
  `handed over to` appended. The display name of a buddy channel the caller led
  is renamed to the successor. Nothing else moves: no budgets, no buddy
  bindings, no reply allowance, no approvals.
- `gc` removes a leave marker only once its session is verified gone (never by
  age), prunes channel history, unheld send locks (a crashed sender's leftover)
  and receipts older than 14 days. `gc --dry-run` lists markers and receipts
  without removing anything.

## Buddy channels

`buddy set` creates or reuses the lead's buddy channel and adds the bound
buddy; `buddy clear` removes the buddy it clears. The key is
`buddy-<first 12 hex of sha256 of the owner's typed id>`, the display name
`buddy · <owner name>`. Binding a second buddy adds it and keeps the first, so
a lead with two buddies has one channel with three members while the binding
names only the latest. Remove an earlier buddy with `channel remove`, or it
runs `channel leave`.

- Upkeep runs only when the caller is the binding's owner (`buddy set --as
  OTHER` is not). A failure never fails the binding: stderr prints `warning:
  buddy channel not updated: <reason>`.
- A lead that handed its buddy channel over gets no new one; the warning names
  the successor that owns it.
- Nobody joins a buddy channel with `channel join`.

## Claude to Claude

`send --to cc:<uuid>` from a Claude session stays refused (use native
`SendMessage`). A channel delivery is exempt, internally and only after the
channel exists and the sender is a member, so it reaches a Claude member from a
Claude sender. Consequence: adding a Claude session as a member needs no
consent from it, so a two-member channel reaches another Claude session
without `SendMessage`. The header, pacing and delivery verification still
apply.

## Decisions

- **D1:** a member the journal cannot address (its recipient check fails) is
  not delivered to and is reported `refused: <reason>` with `ACK not requested
  (unverified)`, outside the `inbox acks` denominator; the rest are delivered
  with an ACK requested. If none can be addressed, nothing is recorded or sent.
- **D2:** `#general` posts always state what OpenCode discovery covered, so a
  reader cannot mistake it for every session.
- **D3:** an OpenCode member is gone only when a complete walk omits it and a
  lookup of that session says not found; anything less leaves it unverified,
  delivered without waking it. Only runtime-verified absence marks any member
  gone.

## Failure model and limits

- Covers a crash of a `peers.py` process, not power loss or storage loss.
- Membership is written before history, so history never describes a change that
  did not happen; a crash between them leaves the change without its entry.
- A torn final history line (no newline) is never read and is cut before the
  next append.
- A crash while appending the send entry delivers nothing; a journaled message
  stays un-ACKed with no history entry. A crash before the outcome entry reads
  `unknown` and is never retried. The send lock is held from before the send
  entry until the outcome entry, so a reader sees `in progress` only while the
  sender lives; pids are never used.
- Trust: identity comes from environment variables (`journal.caller()`), so a
  process of the same OS user can claim a live peer's id or point discovery at
  directories it controls, and any such process can write the channel files.
  The per-sender `@all` limit and the `#general` sender check guard against
  mistakes, not a hostile same-user process.
- Not covered: delivery into a running turn, cross-machine channels, and native
  Claude `SendMessage` traffic, which reaches a channel only when sent through
  `peers.py`.
