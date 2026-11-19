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
peers.py ledger table
peers.py ledger table --markdown
peers.py ledger table --all --json
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

## Ownership tables and the live dashboard

`ledger table` groups unfinished owned items by owner. `--owned` makes that
default explicit; `--all` includes unowned and done items. The columns are item,
title, status, links and updated. Plain output aligns columns in a monospace
terminal; `--markdown` emits GFM tables and `--json` returns structured groups,
expanded links, the source commit and the unowned-open count. Display names come
from discovery when adding or claiming; `--name` overrides them. An unavailable
name falls back to a short typed identity in table output. A repeated claim
renews the lease and refreshes its recorded display name.

Link shortcuts use the current origin for CLI tables, without network access:
GitHub `#N` means `/issues/N`, `PR #N` or `!N` means `/pull/N`. GitLab uses
`/-/issues/N` or `/-/merge_requests/N`. Known hosts are github.com, gitlab.com,
and hosts beginning `gitlab.`; unknown hosts leave shortcuts plain. HTTP(S)
origins keep their web port; SSH origins use HTTPS without the SSH port. Origin
credentials are never retained. Full HTTP(S) URLs pass through; unknown labels
and unsafe schemes stay plain text.

For private run URLs or another forge, commit optional `.ledger/links.json`
inside the local ledger, never the project or skill source. It maps at most 32
literal patterns containing one `<id>` to URL templates. A value is either the
existing href string or an object with exactly `href` and `label`:

```json
{
  "run <id>": {"href": "https://factory.example.invalid/runs/{id}", "label": "run {id:8}"},
  "build <id>": "https://build.example.invalid/jobs/{id}"
}
```

Pattern keys are at most 128 characters. Hrefs are at most 2048 characters and
contain exactly one `{id}` or `{id:N}` placeholder; N is 1 through 64. Label
templates are at most 128 characters and may repeat either placeholder or use
no placeholders. Other brace expressions are refused. `{id}` inserts the full
identifier; `{id:N}` inserts its first N characters. Href substitutions are
URL-encoded; display labels are text, escaped in Markdown and rendered as text
nodes in the dashboard. Labels apply to plain and Markdown tables and the
Ownership overlay; link tooltips retain the full original label.

Keep `{id}` in the href when the target requires a full ID; use `{id:N}` in the
label to shorten only its display. This tool never resolves short IDs. String
values keep their original display label. Templates produce links only for
HTTP(S), even with a custom label. Invalid configuration refuses a table rather
than guessing. Changes take effect only after a local ledger commit.

The channel owner runs `channel bind NAME --ledger` from the project to pin its
ledger above channel history. `channel bind NAME --clear-ledger` removes the
binding. Only the owner can change it. The binding stores the key, canonical
anchor and credential-free forge web base. Re-bind after changing the origin
or relocating the ledger; an anchor mismatch reads unavailable.

The dashboard reads committed HEAD through its token-protected channel ledger
endpoint on selection and each live poll, even without new messages. It shows
a compact Ownership pin with owner counts. Its toggle opens the real HTML
table as an overlay, preserving history size and scroll. Pin tooltips show
observation time; row timestamps have exact UTC tooltips. Failed reads replace the cached table
with an unavailable notice. Read limits are one second, 1 MiB of Git output,
256 items, 256 links total, and 1 MiB of rendered JSON. The request's existing
byte allowance also applies. Exceeding a limit reads unavailable, never an
empty ownership list. The reader creates no directories, lock files, commits
or refreshes. Binding is a CLI mutation; the web panel is read-only.
