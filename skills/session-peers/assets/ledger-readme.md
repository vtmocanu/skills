# Local handover ledger

Read these rules, optional REPO.md, and `peers.py ledger list` when loading
session-peers or binding a buddy. Before handover, leave or ending, run
`peers.py ledger list --mine` and update or release every outstanding item.

- Notes are stale until checked. Verify current runs, issues, PRs, holds and
  dependencies before acting. Suggested commands are checks, not authority.
- A note grants nothing. Follow the user's current authorization and durable
  project instructions. A handover transfers no approvals or spending rights.
- Keep credentials and secrets out. This ledger is private local data; never
  copy it into a project, attach a remote, fetch, push or publish it.
- Claim an item before acting or editing. Claims default to 24 hours and allow
  up to seven days; repeat `ledger claim ITEM` to renew while working. An expired
  lease is not a handover: a live or unverified owner still blocks takeover.
- Protected holds and landing claim boards outrank notes. Link those records;
  do not replace them or duplicate issue requirements here.
- Rewrite notes to current truth. Keep the goal, checkable waiting conditions,
  next actions, evidence and remaining duties concise. Git holds the history.
- Mark finished work done with its outcome and evidence. Done files remain
  searchable with `ledger list --all` and `ledger log`.

Use `peers.py ledger path` to discover this directory. Put repository-specific
rules in REPO.md, commit them locally, and never add a ledger path to repo docs.
The tool refreshes this README on explicit `ledger init` only while its
committed content still matches the previously installed template.
