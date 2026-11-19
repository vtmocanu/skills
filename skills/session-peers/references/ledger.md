# Local repository ledger

Use `<this skill's directory>/scripts/peers.py ledger`. No ledger path belongs
in a project document. The skill derives it from the current repository.
This is durable handover context, separate from the message journal and from
short-lived landing claim boards. Notes grant no authority.

## Discover and read

```sh
peers.py ledger path --json
peers.py ledger list
peers.py ledger list --mine
peers.py ledger list --all --json
peers.py ledger show ITEM
peers.py ledger log ITEM
```

Use the installed script's full path in place of `peers.py`. `path` prints the
key and directory. Read that ledger's committed `README.md` and optional
`REPO.md` with `git -C DIRECTORY show HEAD:README.md` (and `HEAD:REPO.md`).
`list` defaults to open and blocked items; `--open` is explicit, `--all` adds
done items, and `--mine` selects the caller's unfinished items. No ledger means
an empty successful list without creating files. Other reads require a ledger.
Reads use a pinned committed HEAD, including while a writer has pending files.

Storage is `$XDG_DATA_HOME/session-peers/ledger`, or
`~/.local/share/session-peers/ledger` when unset. XDG_DATA_HOME must be absolute.
The canonical `git rev-parse --path-format=absolute --git-common-dir` identifies
the repository. A final `.git` or `.bare` is replaced by its parent. Replace
every character outside `[A-Za-z0-9._-]`, including `/`, with `-` for the key.
Worktrees and symlinked checkouts share it. A stored full anchor detects
collisions and moved repositories; a mismatch refuses access, naming both paths.

`--repo-key KEY` selects an explicit identity key, never a path. It must contain
only the same ASCII characters, no `..`, and at most 240 bytes. It does not
bypass the anchor check. Use a distinct key for two repositories whose encodings
collide. Pass it consistently to every ledger command for that repository;
automatic buddy reminders discover only the default derived key.

## Initialize and write

```sh
peers.py ledger init
peers.py ledger add ITEM --title 'Owed verification' --body-file NOTE --link URL
peers.py ledger claim ITEM --lease 24h
peers.py ledger update ITEM --status blocked --body-file NOTE
peers.py ledger update ITEM --append-file ADDITION
peers.py ledger release ITEM
peers.py ledger done ITEM
peers.py ledger import-dir DIRECTORY
peers.py ledger relocate --from=OLD_KEY
```

All public reads and mutation results support `--json`. Mutation identity is
detected by the existing session-peers rules; `--as cc:UUID`, `codex:UUID` or
`oc:ses_ID` supplies an explicit identity. `--name NAME` supplies its display
name, never its ownership identity. Git authors use `session-peers NAME` and a
local synthetic email, not the user's Git identity.

- `add` claims the new item for 24 hours. `claim` renews your own lease or
  claims an unowned item, defaulting to 24 hours. Leases accept positive minutes,
  hours or days up to seven days (for example `30m`, `24h`, `7d`). An expired
  lease never authorizes takeover. A verified journal
  handover, or the existing complete-registry/liveness evidence of a departed
  owner, permits a new explicit claim. Live and unverified owners block it.
- `update`, `release` and `done` require the current owner under the write lock.
  A former owner cannot edit after takeover. Replacement and append files are
  mutually exclusive; repeated `--link` replaces the links on update.
- `done` retains the file and releases its claim. Update the outcome and evidence
  before marking it done. To reopen it, claim it then update its status to open.
- Frontmatter values are JSON scalars/objects/arrays on individual lines,
  also valid YAML. The Markdown body is free text. Slugs use the key charset.
- Import reads regular top-level `*.md` files, skipping README.md and REPO.md.
  It preserves each whole original document as the body, plus source path and
  SHA-256 provenance. The first `# Heading` supplies the title, otherwise the
  filename slug is used. Items start open and unclaimed; legacy owner/Grants text
  inside their bodies is context only. One batch produces one commit. Any
  collision or unreadable input refuses the entire batch before writing.
- `init` writes generic rules. It refreshes the committed README only if its
  content still matches the previously installed template. It preserves edited
  rules. Add optional REPO.md with repository-specific rules, commit it locally,
  and keep its private content out of the skill source and public projects.
- After moving the project, run `relocate --from=OLD_KEY` in its new location.
  This explicitly moves the ledger to the currently derived key and commits
  its new anchor. `--repo-key NEW_KEY` can choose another destination. Existing
  destinations refuse; files and history are never overwritten.
  Use the equals form for keys starting with `-`, including derived keys.

## Local-only and recovery rules

Never fetch, push, add a remote or publish a ledger. Every operation refuses a
configured ledger remote. Git runs without inherited Git environment injection
or global/system configuration, with hooks, signing, auto-maintenance and
transports disabled. These are tool guarantees, not protection from another
program running as the same OS user. Ledger directories are private (0700),
files private (0600), and storage inside the project checkout is refused.

Mutations take a bounded exclusive `fcntl.flock`, require a clean tree, stage
only intended files, and commit locally. Bodies use atomic file replacement.
Concurrent cooperating writers serialize. Direct manual edits must be committed
locally before invoking mutations; never edit while a command is writing.

A process crash or ambiguous Git failure can leave uncommitted files. Preserve
them. Back up the ledger, inspect its local `git status` and `git diff` (including
staged changes), reconcile with committed HEAD and the current owner, then
commit the intended contents locally. Never reset or delete notes blindly.
If a failed commit actually landed, the next command re-observes its committed
state. Relocation failures preserve the moved directory and pending metadata;
inspect both old and destination locations before recovery. No power-loss
durability or automatic rollback across multiple files is claimed.

## Session lifecycle

On skill load or buddy bind, discover the cwd ledger, read its committed rules,
and run `ledger list`. Before handover, leave or ending, run `ledger list --mine`
and update or release every outstanding item. Handover messages name the repo
key and item slugs. They never implicitly transfer ledger ownership or grants.

Human `buddy set`, `buddy` status and `handover` output adds a compact reminder
when unfinished items exist. It counts blocked items as open and expired claims
as stale. No items, no ledger, any discovery failure or a one-second whole-check
timeout produces no reminder. Existing JSON output stays unchanged. There is
no SessionStart hook and no change to hook installation.
