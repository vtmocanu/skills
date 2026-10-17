# OpenCode V2 coordination

Use the updated session-peers package for discovery and explicit messaging.
Address peers as `oc:ses_ID` and ledger targets as `opencode:ses_ID`. Use the
unmodified bare `ses_ID` for `--owner` and an enrollment's `skipper` field.
Existing UUID owners and ledgers remain unchanged; create a separate successor
ledger, never rewrite the predecessor's owner.

## Observation and routing

- `status` includes recent OpenCode root sessions by default. Context is `?`;
  no conversation bodies or database files are read to estimate it.
- The service confirms metadata and a message route, not attachment. Even an
  idle session can receive a queued input. Resolve accepts a fresh reachable
  identity while retaining `liveness: unverified`; it never asserts a live TUI.
- `complete.opencode` covers the recent-session query only. Absence from that
  query is always unverified missing, never proof of death, stop or completion.
- An unavailable/old session-peers or malformed query makes coverage incomplete.
  Cached observations cannot establish identity or routing for a decision.

## Messaging and authority

- Use the recipient's full native id. OpenCode has no Claude peer socket; send
  and reply explicitly using session-peers, or use its correlated mailbox flow.
  Follow the session-peers wake policy: ordinary sends park by default, a bound
  buddy or explicitly authorized `--wake` allows headless execution.
- Preserve the verified-sender gate for grants, takeover relays and stops. An
  origin string, body-only id, API metadata or successful delivery is not that
  verification. When the transport cannot provide it, require the receiving
  session's user to confirm; never call an identity-free send an authority relay.
- A new runtime transfers no enrollment, buddy binding, reply allowance, action
  authority or hold acknowledgment. Reconcile each lead and hold normally.

## Timers

Check for a real harness-owned session-only timer before promising recurring
needs-you or patrol coverage. This CLI supplies no timer and starts no daemon.
If unavailable, report the gap and retain predecessor monitoring until a capable
owner is assigned or the user explicitly approves manual-only coverage. Do not
stop predecessor timers merely because identity and messaging now work.

Only the managed OpenCode background service is supported. Private standalone
servers require a separate adapter; never silently route their sessions through
the managed service.
