# Critical-message journal and ACK

A private journal holds the bodies of consequential messages (verdicts,
co-signs, handovers) so a recipient can fetch the exact text by id and
acknowledge it. Peers carry only a pointer; the journal carries the body.

## What it stores

One journal per OS user at `~/.codex/session-peers/journal/` (directory 0700,
files 0600). Its location never depends on `CODEX_HOME`. It stores bodies only
for:

- messages sent with `send --record`;
- messages stored with `record`;
- every correlated `ask`/`dispatch` request and its `reply`.

**Coverage limit:** the journal is NOT a record of all session traffic. Casual
messages and native `SendMessage` traffic are absent. Never present it as
complete.

## Commands

Run each as `<this skill's directory>/scripts/peers.py ...`. All take `--json`.

| Command | Effect |
| --- | --- |
| `send --to PEER ... --record` | Journals the body, then sends. The journal id equals the transport message id; `--json` adds `"journal": {"message_id": ..., "recorded": true}`. |
| `record --to PEER [--to PEER ...]` (`--message` or `--message-file`) | Record-only; nothing is sent. At most 64 recipients, each an existing session. |
| `inbox get MSG_ID` | Prints the body, your slots, ACKs, settlement and any transport error. Recipients (and covered successors) only. |
| `inbox journal [--all]` | Lists metadata, no bodies, of messages you may read and have not ACKed; `--all` includes ACKed ones. |
| `inbox acks MSG_ID` | Per-recipient ACK state, settlement and transport error, no body. The sender, any recipient and covered successors may run it. |
| `inbox handover --to TYPED` | Lets a successor act for you (see Handover). |
| `ack MSG_ID [--for TYPED]` | Records your ACK. `--for` names the recipient slot when you cover several by handover. Idempotent. |

`record` prints this pointer, then one `recorded for <typed id> (<name>)` line
per recipient:

```text
critical message <id>: run peers.py inbox get <id>, then peers.py ack <id>
```

With `--json` it prints `status: recorded`, `message_id`, `to` and `pointer`.
Send the pointer to the recipient.

`record`, `inbox get|journal|acks|handover` and `ack` accept no `--as` or
`--from-thread`: identity comes from the environment only.

## Native Claude-to-Claude flow

1. The sender stores the body: `record --to cc:<uuid> --message-file PATH`.
2. The sender sends a native `SendMessage` carrying the pointer text.
3. The recipient runs `inbox get <id>`, acts on the retrieved body, then runs
   `ack <id>`.

Casual native messages stay unrecorded.

## ACK versus settlement

- An ACK is only an explicit `peers.py ack`, bound to the message id, the
  recipient slot (`for`) and the acknowledging identity (`by`).
- Correlated entries also carry a settlement, shown separately from ACKs:
  `replied` is recorded when the verified recipient runs `reply`; `consumed` is
  recorded when the requester's `ask`/`await` takes the reply.
- A transport receipt, queue acceptance, socket write or idle notice is never
  an ACK, a settlement or a verdict.

## Identity and addressing

- Typed identities: `cc:<uuid>`, `codex:<uuid>`, `oc:<ses_id>`.
- Only the intended recipient, or a successor named by a verified handover, may
  read or ACK.
- That check is an addressing rule between sessions, NOT confidentiality
  against processes running as the same OS user: they can read the 0600 files
  or set environment variables.
- The journal's `from` is the detected environment identity, not proof. A
  `send --record` whose identity cannot be detected is recorded with no sender
  and prints `note: sender identity not detected; recorded with no sender`.
- For correlated requests, `from` follows the `--from-thread` rule. When both
  an explicit `--from-thread` and the environment's Codex thread are UUIDs and
  differ, `ask`, `dispatch` and `await` refuse (exit 2); `send --from-thread`
  has no such check. A Claude environment may still name a thread, so there
  `from` is a claim. This is an accepted limit.
- Journal commands refuse an environment that names more than one runtime
  (Claude, Codex, OpenCode). Unset the variables of the others, as the error
  text shows, e.g. `env -u CODEX_THREAD_ID -u CODEX_SESSION_ID peers.py ...`.
- An OpenCode id is verified with the service before anything is read or
  written.

## Handover

`inbox handover --to <full typed id>` is recorded under the predecessor's
detected identity.

- Names, `@name` and `buddy` are refused. The successor must be an existing
  session; a self-handover is refused.
- It covers only messages recorded before the grant (`through_seq`). Re-run it
  to extend.
- Chains are allowed, at most 8 links and 64 relevant edges examined; access
  beyond those limits fails closed.
- Limits: at most 8 grants per grantor and 512 retained in total. Sessions that
  are recipients of retained messages can still use up the global cap (accepted
  limit).
- It grants read and ACK only: no approvals and no reply allowance.
- A predecessor whose environment is gone cannot grant.

## Retention

- **Age cap:** 14 days, fixed. Everything older is pruned at the next write.
  It overrides everything, including a live mailbox: nothing is kept past 14
  days.
- **Size cap:** the default is 16 MiB. `SESSION_PEERS_JOURNAL_MAX_BYTES` can
  only lower it. A 1 MiB reserve above the cap holds events (ACKs,
  settlements, transport errors, handovers).
- **Quotas:** plain recorded messages take at most 50% of the cap; each sender
  at most 25%.
- **Under size pressure** only finished entries are pruned: every recipient
  ACKed, or a settled correlated entry (settlement needs no ACK). A correlated
  entry is pruned only once its request mailbox is gone. Unfinished young
  entries (neither fully ACKed nor settled) are never pruned, including
  expired requests and entries with transport errors: pruning keys only on ACK
  and settlement.
- **Clock skew:** records dated up to 14 days ahead are kept. A clock step-back
  of more than 14 days can drop records at the next rewrite.
- **Retrieval guarantee (#160 channel contract):** a body stays available until
  it is 14 days old while it is un-ACKed and unsettled, or while its
  correlated mailbox is live (never past the 14-day age cap). There is no
  guarantee once it is fully ACKed or settled. A channel history that must keep the body
  longer should keep its own copy.

An entry past the age cap is shown `[expired]` until a write prunes it.

## Exit codes

| Code | When | Meaning |
| --- | --- | --- |
| 75 | Before sending | Journal full. On a sending command nothing was sent. `--json` prints `{"status":"journal_full","detail":...}`. Recipients `ack`, or wait for expiry, then retry. `ack` and `inbox handover` can also return 75 when even the event reserve is exhausted. |
| 1 | Before sending, or after a reported transport failure | Journal unavailable or refused (bad mode, lock held, not a recipient, unknown id): on a sending command nothing was sent. A transport failure whose `transport_error` was recorded also exits 1; delivery is then unconfirmed, as below. |
| 76 | After transport | A journal update after a transport attempt failed (the command prints `journal update failed: ...` on stderr). The transport itself may have succeeded or failed. |

On 76 read the transport result the command printed:

- If it reports success (sent, queued, replied, or the consumed reply body was
  printed), the message went out or the reply was taken. Do NOT resend.
- If it reports a delivery failure (for example `delivery_failed`, "could not
  send/queue", "codex queue failed" or "nothing queued"), the transport reported
  failure, but delivery is unconfirmed: a timed-out queue or API call may still
  have delivered. Check the recipient before resending.

Never treat 76 as a reason to resend blindly. Run `inbox acks MSG_ID` to inspect
the journal, and report the failure. The command never resends or retries.

## Reply single-assignment

- One reply body per request. A different body is refused; the same body is
  idempotent.
- A partial reply file left by a killed process keeps the existing refusal
  ("already has a different reply"). The body is still in the journal.

## Durability and reader

- **Durability:** survives a crash of any `peers.py` process (fsynced appends,
  atomic rewrites). No claim is made about storage loss or power loss.
- **Temp-file recovery:** an interrupted rewrite leaves the old journal intact.
  The next locked write removes stale `journal.*.tmp.*` files, and a torn final
  line is truncated before the next append.
- **Same-OS-user read-only reader:** `journal.read_only_view()` is a
  read-only reader for the same OS user. It is not a session, never ACKs, writes or
  repairs, checks no identity and has no CLI.
