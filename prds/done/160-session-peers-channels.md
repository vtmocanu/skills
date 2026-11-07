# PRD #160: session-peers channels and membership lifecycle

**Issue**: [#160](https://github.com/vtmocanu/skills/issues/160) | **Label**: PRD | **Priority**: Medium
**Umbrella**: [#163](https://github.com/vtmocanu/skills/issues/163) (child 1 of 3)
**Blocked by**: [#157](https://github.com/vtmocanu/skills/issues/157) (critical-message journal and explicit ACK, including its "Interface requirements from the follow-up PRDs" section: multi-recipient messages, per-recipient ACKs, the same-user read-only reader and verified handover). #157 merged in c469e20 (interface: `skills/session-peers/references/journal.md`) and the #158 route check in 5ca6512 (`references/messaging.md`, "Route check"); this PRD was adjusted to both on 2026-11-06.
**Area**: `skills/session-peers/` (`scripts/session_peers/`, `SKILL.md`, `references/`), plus one step in `skills/done/SKILL.md`.
**Status**: **Complete 2026-11-06**, M1 and M2 done (D1-D3 decided by the lander and buddy, 2026-11-06). Implemented on branch `agent/issue-160`; interface in `skills/session-peers/references/channels.md`. Moved to `prds/done/`.

## Problem

Sessions on one machine coordinate in groups but session-peers only knows pairs:

- A coordinating session broadcasts (a merge freeze, a hold) by sending the same text to each session it can name, one by one, and cannot tell who received or acknowledged it. In one week of use (2026-11-02..06) a broadcast to 12 sessions drew no acknowledgement from several, and the sender resent on a timer.
- A lead with two buddies has no shared place where all three see the same thread.
- A session that ends leaves stale registry entries and sockets behind; names then resolve to several sockets and senders fall back to raw socket addresses.
- A successor session has no record of which conversations its predecessor was part of.

## Outcome

Named **channels** with explicit membership. A channel is a member list plus a history. Every message keeps its body in the channel history. A journaled message (`--record`, every `@all`) is also recorded once in the #157 journal (`record`-style entry naming every intended recipient), and its history entry carries that message ID for ACKs. #157 may prune that body under pressure once it is fully ACKed or settled (journal.md, "Retrieval guarantee"), so history never depends on the journal for the body. Delivery reuses the existing per-runtime send routes.

Commands:

- `peers.py channel create NAME [--member ADDR]...`, `channel join NAME`, `channel leave NAME`, `channel remove NAME ADDR` (the channel's owner only: its creator, or for a buddy channel the lead, or its handover successor), `channel list [--json]`, `channel members NAME [--json]`, `channel tail NAME [--since SEQ] [--json]`. ACK state for a journaled message is #157's `peers.py inbox acks MSG_ID` (no separate channel command).
- `peers.py send --to '#NAME' (--message TEXT | --message-file PATH) [--record]` appends the message to the channel history (journaling it first, as one #157 entry naming every intended recipient, when `--record` is given or the text starts with `@all`), then delivers to each member except the sender.
- `peers.py leave` and `peers.py handover --to TYPED_ADDR` (lifecycle, below).

`#general`:

- Derived, not stored: its members are every session the registry lists (live or unverified), minus sessions that ran `peers.py leave`, minus the browser peer (#162). It is exempt from the stored-member cap.
- A plain post to `#general` is **recorded only** (readable with `channel tail`), not delivered.
- A post whose text starts with `@all` is the broadcast: it is delivered to every member except the sender and requests an ACK from each (#157 per-recipient ACKs). The sender is never in the ACK tally.
- `@all` is limited in the fan-out itself (whoever calls it): at most 64 recipients, and at most one `@all` per sender per 60 seconds. Over the cap it is refused before anything is journaled, with an error naming the limit.

Buddy channels:

- `buddy set` creates (or reuses) the owner's buddy channel and adds the bound buddy. Its key is `buddy-<first 12 hex of sha256(owner's typed identity)>` (typed identities cover OpenCode `ses_` IDs, which are not UUIDs); its display name is `buddy · <owner name>`.
- Buddy-channel membership is stored in the channel, independently of the buddy store, which keeps one binding per owner (`buddy.py`). `buddy set` updates that binding as today and also adds the buddy to the channel; binding a different buddy later adds it too and does not remove the earlier one. So a lead with two buddies has one channel with three members while its binding names only the latest.
- `buddy clear` (owner only, as today) clears the binding and removes the currently bound buddy from the channel. An earlier buddy is removed with `channel remove`, or leaves itself with `channel leave`.

Lifecycle:

- `peers.py leave` removes the caller from every stored channel, records `left` in each, and records a leave marker so derived `#general` excludes it while the session stays live. `/done` runs it only when its verdict is that the session can close.
- `peers.py handover --to TYPED_ADDR` requires a #157 grant the caller already recorded with `peers.py inbox handover --to TYPED_ADDR` (read and ACK of journaled messages, `through_seq`). Without that grant it refuses and prints the `inbox handover` command; it never creates a grant itself. The documented sequence is the two commands in that order. It then records `handed over to X` in each channel the caller belongs to, adds the successor, removes the caller, transfers channel ownership (creator role, and the lead role of a buddy channel) to the successor, and renames buddy-channel display names to the successor. It grants nothing else: no approvals and no reply allowance.
- Liveness uses the existing probes. Claude: `dead` from `claude.py`. Codex: the existing held/not-held check. OpenCode: `gone` only when a complete `session.list` page walk omits the session; an incomplete or failed walk leaves it unverified. Gone members are skipped by delivery. Unverified members stay members; delivery to them goes through their route, which may refuse (the Codex route refuses when liveness cannot be checked), and the refusal is the reported outcome.

Delivery outcomes (one shared vocabulary; #161 shows these exact names): `recorded`, `transport accepted`, `queued (thread paused)`, `not woken (OpenCode idle)`, `refused: <reason>`, `skipped: gone`, `in progress`, `unknown` (the result cannot be proven, for example after a crash mid-delivery; never retried), `unverified` (the state could not be checked), `acked`.

- OpenCode: channel sends never wake an idle OpenCode session (the existing rule: only `--wake` or the caller's bound OpenCode buddy wakes one); the outcome is `not woken (OpenCode idle)` and the message stays readable. This includes `@all`.
- Interrupted fan-out: the sender acquires an exclusive `flock` on a per-send lock file (named by message ID) **before** publishing the send's history entry, and holds it until the final outcome entry is appended. A reader (`channel tail`, the #161 read model) that finds a send with no outcome entry tries a non-blocking lock: held means `in progress`; acquired (the sender is gone, whatever its pid became) means `unknown` for every recipient; a lock that cannot be checked means `unverified`. Nothing retries a send automatically. (A pid alone is not used: pids are reused.)
- Pacing: deliveries are serial, with at least 1 second between two deliveries to Claude targets (new code; session-peers has no pacing today). Each delivery carries a per-recipient header line (message ID and recipient), so no two deliveries are byte-identical, which avoids Claude's identical-repeat refusal.

Acceptance examples (each tested at the seam: fake registries, a fake shim queue, fake Claude sockets, a fake OpenCode client):

1. Sessions A (Claude), B (Codex), C (OpenCode, idle). A runs `channel create ops --member codex:<B> --member oc:<C>`, then `send --to '#ops' --message 'freeze main'`. The message lands in B's fake shim queue (`transport accepted`); C's outcome is `not woken (OpenCode idle)`; `channel tail ops` shows one entry with the body.
2. A sends `@all freeze main until the release is tagged` to `#general`. B runs `peers.py ack <msg-id>`. `peers.py inbox acks <msg-id>` shows B acknowledged and C pending; A is not listed. A second `@all` from A within 60 seconds is refused.
3. A channel made with `channel create` holds L and X. L runs `peers.py leave`: the channel shows `L left`, a later send to the channel does not address L, and derived `#general` omits L.
4. L runs `inbox handover --to cc:<M>`, then `handover --to cc:<M>`: every stored channel L was in shows `L handed over to M`, and M is a member and owner: M can `channel remove` a member of the handed-over buddy channel.
5. (M2) Lead L binds Codex buddy X, then Codex buddy Y. `channel list` shows one buddy channel with L, X and Y. L runs `channel remove <buddy channel> codex:<X>`: X leaves the channel, Y stays. L runs `buddy clear`: Y leaves and the binding is cleared.

## Out of scope

- Any web UI (#161, #162).
- Delivering into a running turn (#159).
- Changing native Claude-to-Claude `SendMessage`. A Claude member receives channel messages through the existing route; a Claude session's own native messages reach a channel only when sent through `peers.py` (the #157 coverage limit).
- Cross-machine channels.

## Modules and seams

- **Channel store** (new, `session_peers/channels.py`): create, join, leave, members, list, history append and read. Membership: one private (0600) JSON file per stored channel under the session-peers state directory, written atomically, members as typed identities only. History: the existing topic log (`topics.py`) under the topic `channel:<key>`, each entry carries the sender, kind and the body, plus the #157 message ID for journaled messages; the per-recipient delivery outcomes of one send are appended as ONE outcome entry referencing the message once fan-out finishes (so a 64-recipient broadcast costs two history entries, not 65); ACK state is read from #157, not stored in the history; the existing pruning (7 days, 1000 entries, 16 MiB) and `gap:` reporting apply.
  - Resource bounds: 64 stored members per channel, 256 stored channels, names `[a-z0-9-]{1,64}`, the `@all` limits above, the existing 1 MiB message cap. Each limit fails with an error naming it.
- **Fan-out send** (extends `messaging.py`): journal first when the message is journaled (#157), then history, then serial per-member delivery through the existing routes with the pacing above. One member's failure is recorded and does not stop the rest.
  - Untrusted input: bodies and names are peer-supplied and handled as data with the existing provenance wrapper.
- **Identity resolution**: the existing typed resolver (`identity.py`). An ambiguous name already raises `ResolveAmbiguousKind` and lists candidates.
- **Route check** (extends `routes.py` from #158): today `routes.check` treats `--to '#ops'` as a bare name and refuses it. Add a channel-target branch for the `#` prefix, resolved against the channel store (unknown channel: refused, rc 2). Fan-out then checks and verifies a typed route per member before each delivery with the existing `routes.check`/`verify`.
  - Claude-to-Claude: #158 refuses a Claude sender's `send` to a Claude session (native `SendMessage` instead). Channel fan-out cannot use native `SendMessage`, so it uses an internal channel verb that is exempt from that refusal, available **only after** the channel name and the recipient membership snapshot are validated. The per-recipient header, pacing and the delivery `verify` still apply. A direct `send --to cc:` from a Claude session stays refused.
  - A mixed-runtime environment stays refused, as for journal commands and the route check.
- **Lifecycle hooks**: `buddy.py` maintains buddy channels; `leave` and `handover` are new commands; `skills/done/SKILL.md` gains the conditional `leave` step.

## Testing decisions

Follow `peer_tests/` (`support.py` fixtures; `test_topics.py`, `test_buddy.py`, `test_messaging.py`, `test_routes.py`, `test_journal_cli.py` are the closest models). Every acceptance example above is a test. Also: a Claude sender's channel post reaches a Claude member while the same sender's direct `send --to cc:<member>` is still refused; the channel exemption is not reachable before the channel and membership snapshot validate (unknown channel, non-member sender); after a journaled channel message is fully ACKed and its journal body is pruned, `channel tail` still returns the body from history; `handover` without a prior `inbox handover` grant is refused with the command to run, and after both commands the successor's memberships and ownership moved; an interrupted fan-out (no outcome entry, per-send lock free) reads as `unknown` per recipient and is not retried, a held lock reads as `in progress` even when the sender's pid was reused, a lock check that fails reads as `unverified`, and a reader that sees a new send entry while its sender still holds the lock reads `in progress` (the interleaving where the reader looks between the entry and the delivery); the successor of a handover can manage the handed-over channel; caps and name validation, atomic writes, journal-before-delivery ordering, pacing (an injectable clock, no real sleeps), per-recipient headers differ, gone and unverified handling per runtime including the OpenCode complete-walk rule, the leave marker, and that handover moves no reply allowance.

## Milestones

### M1: channels, `#general` with `@all`, leave and handover

Channel store and commands, fan-out with outcomes and pacing, derived `#general`, `@all` with its limits and ACK tally, `leave`, `handover`, liveness handling, the `/done` step, `SKILL.md` and a new `references/channels.md`.

- Blocked by: #157 merged.
- Acceptance: examples 1 to 4.

### M2: buddy channels

`buddy set` and `buddy clear` maintain the buddy channel, `channel remove`; handover renames its display; docs and tests.

- Blocked by: M1.
- Acceptance: example 5.

## Decision Log

- **Every channel message keeps its body in channel history; journaled ones also carry their #157 message ID.** #157 journals only critical messages and may prune a body under pressure once it is fully ACKed or settled, so the history is the body store for every channel entry. Rejected: journaling every channel message (the journal is for consequential messages); referencing journaled bodies by ID only (superseded 2026-11-06, see below).
- **`#general` is derived from the registry and records only, unless `@all`.** A stored "every session" list would go stale and exceed the member cap; delivering every `#general` post would interrupt every session. Rejected: stored `#general`; delivering plain posts.
- **`@all` limits live in the fan-out, not in a UI**, so a CLI caller is bounded too.
- **No OpenCode wake from channels**, matching the existing wake rule and avoiding headless turns in every idle OpenCode session on a broadcast.
- **Buddy channels keyed by a hash of the owner's typed identity**, because names collide across runtimes and change across successors.
- **Members are typed identities; only runtime-verified absence marks a member gone.** OpenCode has no dead state, so "absent from a complete listing" is its equivalent; anything less stays unverified.
- **Handover reuses #157's verification** rather than defining a second concept.
- **Pacing and per-recipient headers are new code**, because session-peers has none today (only documentation of Claude's burst and repeat refusals).
- **Blocked by #157** because the ACK record, multi-recipient entries and verified handover come from it.
- **History always keeps the body** (2026-11-06, after #157 merged): the journal may prune a fully ACKed or settled body under pressure. Rejected: referencing journaled bodies by ID only.
- **`inbox acks` instead of `channel acks`**: one ACK view. Rejected: a channel-labelled duplicate command.
- **Channel handover reuses a recorded `inbox handover` grant**: one verification concept. Rejected: `handover` creating the grant itself.
- **Channel fan-out is exempt from #158's Claude-to-Claude refusal, internally and only after channel and membership validation**: native `SendMessage` cannot fan out. Rejected: refusing Claude members of a channel; exempting direct `send`.
- **D1, ACK limitation accepted** (lander and buddy, 2026-11-06, on the owner's delegation). The recipient snapshot is preflighted and only members the journal resolves are journaled; each excluded member is reported `refused: <reason>` with `ACK not requested (unverified)` and sits outside the `inbox acks` denominator. If none resolve, the post is refused before anything is recorded or delivered. A member that becomes unreachable after journaling stays pending with its refused outcome. #157's existing-session check is not weakened.
- **D2, `#general` OpenCode coverage is the list view** (lander and buddy, 2026-11-06). Coverage is the top-level sessions `peers.py list` discovers that were updated within `SESSION_PEERS_OPENCODE_RECENT_HOURS` (default 24 h), not every stored OpenCode session. `#general` posts (including `@all` there) print and record that limit, or that OpenCode was not enumerated. `@all` on a stored channel prints no coverage line: its member list is explicit and involves no enumeration.
- **D3, stricter OpenCode gone rule** (lander and buddy, 2026-11-06). A member is gone only when a complete walk omits it and `get_session` returns not found; otherwise it stays unverified and is delivered without waking. This narrows the Outcome's "complete walk omits" rule (a child session is absent from the top-level walk yet exists).
- **The channel store is user-wide** under `~/.codex/session-peers/channels/`, derived from HOME like the journal, so every session of the OS user shares it whatever its `CODEX_HOME`. Ordinary topics stay scoped to `CODEX_HOME`. The store lock is `.channels.lock`, outside the channel key namespace.
- **Torn topic-append recovery**: a final line without its newline is never read and is cut before the next append, for channel history and ordinary topics alike.
- **The `channel:` prefix is reserved in `topic post`** (exit 2) to avoid confusion with channel history; not a security boundary.
- **`--record` on a plain `#general` post is journaled and not delivered.**
- **`@all` allowance**: consumed at reservation, before journaling; one stable per-sender lock is held through reservation, journaling and restore; a refused journal restores it; a future-dated stamp refuses (fail closed). Rejected: a message-id-guarded rollback.
- **`#general` senders must be sessions `peers.py list` discovers** (found in review): a made-up id would otherwise get its own `@all` allowance. A session that left `#general` may still post. Identity remains environment-derived (`journal.caller()`), so this guards against mistakes, not a hostile same-user process.
- **Membership is written before history**, so history never describes a change that did not happen.
- **The owner keeps ownership on leave**; `handover` also moves channels the caller owns but left.
- **No `channel join` on buddy channels.**
- **Leave markers last until the session is verified gone** (never removed by age).
- **Exit codes**: 0 every member accepted, queued, not woken or skipped gone; 1 a member refused or nothing resolvable; 2 argument, name, membership or limit refusal; 75 or 1 journal failure before sending; 76 a `transport_error` update failed.
- **The `@all` cap counts the delivery set** (members except the sender, minus gone members).
- **Handover receipt**: `inbox handover` also writes a receipt under `channels/grants/`, accepted by `handover` for 14 days, because the journal may prune the grant sooner. Writing it only warns on failure.
- **Membership records are validated on read** (size, field types, canonical deduplicated members, roster cap) because any same-user process can write them.
- **A lead that handed its buddy channel over gets no new one**: its derived key belongs to the successor; `buddy set` warns and names the owner. Rejected: a second key scheme.
