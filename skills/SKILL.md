---
name: skills
description: Creates, updates, lints, and publishes Claude Code skills, and keeps them in sync with the npx skills package manager. Use when (1) writing a new skill, (2) editing an existing one, (3) linting a skill with agnix, (4) publishing a change so machines pick it up, (5) renaming or deleting a skill, or (6) deciding flat-vs-folder layout or frontmatter/description shape. Triggers include "new skill", "add a skill", "update skill", "skill not loading", "npx skills", "skills update", "lint skill", "agnix", "SKILL.md".
---

# Skills authoring

Author, lint, and publish Claude Code skills. Skills are distributed and refreshed with the `npx skills` package manager (github.com/vercel-labs/skills): each source repo holds the skill, and `npx skills add`/`update` installs and refreshes the installed copy on a machine.

## Source of truth: edit the source repo, never the installed copy

A skill exists in two places. Edit only the source.

| | Where | Edit it? |
|---|---|---|
| **Source** | the skill's git repo (`<name>/SKILL.md`) | **Yes** |
| **Installed copy** | `~/.claude/skills/<name>/` (global) or a project's `.claude/skills/<name>/` | **No** — derived; `npx skills update` overwrites it |

Publishing is pull-based, not live editing. **Change the source repo, then `npx skills update` re-pulls** and rewrites the installed copy. An edit made directly to the installed copy is discarded on the next update.

- **If you own the source repo**: edit `<name>/SKILL.md`, commit, push, then `npx skills update`.
- **If you don't** (you only installed it): the installed copy is read-only in practice. To change it, fork the source repo and `npx skills add <your-fork>`, or open a PR/issue upstream. You cannot push to a repo you don't own.

To find where an installed skill came from, its source is recorded in the lockfile: `~/.agents/.skill-lock.json` for global installs (or `$XDG_STATE_HOME/skills/.skill-lock.json` if set), and `<project-root>/skills-lock.json` for project installs. The installed copies under `~/.claude/skills/` have no lockfile of their own.

## Layout: folder skills

Author every skill as a **folder with a `SKILL.md` inside** — `npx skills` only discovers `<name>/SKILL.md`, never a bare `<name>.md`:

```text
<name>/
├── SKILL.md            # required — frontmatter + body
├── scripts/foo.sh      # optional supporting files
└── references/bar.md   # optional
```

Reference supporting files by the **skill's own base directory**, which the harness provides when the skill loads (a `Base directory for this skill: …` line). For example `<this skill's directory>/scripts/foo.sh`. Do **not** use a bare relative path like `./scripts/foo.sh`: at runtime the working directory is the user's project, not the skill directory, so `./` resolves to the wrong place. Invoke bundled scripts directly (`<dir>/scan.sh`, not `bash <dir>/scan.sh`) — write them executable (`0o755`); direct invocation matches Claude Code's `Bash(/path/to/scan.sh:*)` permission scoping, while `bash …` would need the far broader `Bash(bash:*)`.

Supporting files are the reason to use a folder. A folder containing only `SKILL.md` behaves like a flat prompt — still use the folder form so npx discovers it.

## Frontmatter

`name`:
- **Required.** It is the install/display name; the directory name is only a fallback.
- Max 64 chars; lowercase letters, numbers, hyphens only; no XML tags.
- **Reserved substrings `anthropic`, `claude`** — do not use them in `name:`.

`description` — this is where discovery happens, get it right:
- **Single line.** Keep the description to one line for portability (the Anthropic Skills API) and clean auto-invocation.
- **No unquoted `: ` (colon-space).** A colon-space in a plain YAML scalar reads as a mapping indicator: the `npx skills` parser rejects it (`mapping values are not allowed here`), returns null, and **silently skips the skill** — a `⚠ Skipped` warning, no error, exit 0 — so it never installs. Reword to avoid `: `, or wrap the whole description in double quotes. dot-ai's parser tolerated this, so a skill that installed under dot-ai can vanish under npx.
- **Third person** ("Generates X", "Manages Y"). It is injected into the system prompt; first/second person breaks auto-invocation.
- **Put all "when to use" info here**, not in the body: `Use when (1)… (2)…` and `Triggers include "…"`.
- **Lean slightly pushy** — models under-trigger skills. Frame triggers to pull the model in, not "use if relevant".
- ~500–700 chars is the style target; **1024 is the hard cap** at the Anthropic Skills API (npx itself does not enforce it). It loads in every session, so do not pad.

**Always-on cost / name-only.** The `description` loads in every session whether or not the skill fires, so a niche or over-matching skill can carry a real per-session cost. Claude Code lets you make a skill fire on explicit invocation only via `skillOverrides` in `settings.json` (`"<skill>": "name-only"`) — the description stops loading, the name stays invokable. Use it for single-purpose skills or ones whose triggers over-match.

## Design principles

Adapted from Anthropic's upstream [`skill-creator`](https://github.com/anthropics/skills/tree/main/skills/skill-creator) (Apache 2.0).

- **Concise.** Only add what the model does not already know. Imperative ("Run agnix before committing"), no hedging ("you should", "we recommend").
- **Audience is the model at runtime**, not humans. Skip onboarding prose, rationale dumps, changelogs, install guides.
- **Discover, don't hardcode.** For mutable values (versions, IPs, namespaces, secret paths) give the discovery command, not the value — "how to find X" stays correct, "X is 1.2.3" rots. Label any concrete value as an example and tell the model to re-query.
- **Match specificity to fragility.** Text instructions where judgement varies; parameterized scripts where a pattern exists; specific scripts when sequence matters.
- **Provide a default, not a menu.** Pick one recommended tool/approach; offer alternatives only for clearly different cases.
- **Consistent terminology.** One term per concept throughout a skill.

### Body budget and progressive loading

| Level | What | Loaded | Soft budget |
|-------|------|--------|-------------|
| 1. Metadata | frontmatter (`name` + `description`) | always, every session | ~100 words |
| 2. Body | `SKILL.md` after frontmatter | when the skill triggers | < 500 lines / < 5K tokens |
| 3. Resources | `scripts/`, `references/`, `assets/` | as needed | unlimited |

When the body passes ~500 lines, split detail into `references/*.md` (one level deep). Information lives in `SKILL.md` **or** a reference, never both — duplication rots.

### When to bundle a script

Prefer a bundled script over inline snippets when the recipe has any of: repeated multi-step orchestration (the same `curl … | jq …` chain three+ times), non-trivial data transforms, error handling that matters (retry on 401, partial-failure rollback), or a natural CLI surface (`list`/`get`/`add` verbs). Keep inline when it is a single command, the exact wording matters for the user to read, or the skill is small.

Script conventions: stdlib only where possible (no runtime `pip install`); one executable per concern (no swiss-army `helpers.sh`); direct invocation; forward-slash paths even on Windows.

## Naming

Gerund (`processing-pdfs`), noun phrase (`pdf-processing`), or action verb (`process-pdfs`) are all fine. Avoid vague names (`helper`, `utils`, `tools`) and over-generic ones (`data`, `documents`).

## Content pitfalls

- **Avoid time-sensitive content.** Do not write "if before August 2025 do X" — it rots. Version-pin instead ("since ripgrep 14.0"). Fence deprecated paths under an "Old patterns" / `<details>` section.
- **Solve, don't punt** (script skills). Handle `FileNotFoundError` / `PermissionError` in the script rather than failing and asking the model to recover.
- **Weak language in critical blocks.** Replace `should` with `MUST` / imperatives inside a directive; exempt quoted user-speech trigger examples (fence them so a linter reads them as data). State negatives with a positive alternative ("Use a regular clone instead", not just "Don't clone shallow").

### Never put a bare `$1` / `$2` in a code block

When a skill is invoked **with arguments** (`/<skill> some args`), the runner substitutes argument tokens into `$N` **everywhere in the rendered body, including fenced code blocks** — the files on disk are untouched, so it is invisible unless you compare what ran against the source. The model then runs a silently corrupted command (observed: `awk '{print $1"/"$2}'` arrived as `awk '{print foo,"/"bar}'`). Avoid `$N` in code blocks — prefer `cut -f1`, `jq -r '.[0]'`, or a named regex capture. Where `$N` is unavoidable, put a warning beside the snippet that the `$1` is literal. Do not assume `$$1` / `\$1` escapes survive without checking.

## Linting with agnix

Before committing any skill change, lint it — agnix catches token bloat, weak language, ambiguous instructions, and bad keyword placement:

```bash
agnix --target claude-code <name>/SKILL.md
```

Add `--show-fixes` to preview rewrites, or `--fix-safe` for high-confidence ones (always re-read the diff — "fixable" ≠ "correct in context"). **Errors must be fixed before commit; warnings are advisory** — fix the real ones. **Pre-existing warnings count**: when a file is open for a real edit, surface warnings that predate your change and propose fixing them in the same commit, rather than re-committing a file with the same warning count forever.

## Workflow

### Install a source the first time
`npx skills update` only refreshes skills already recorded in the lockfile, so a brand-new skill (or a source never installed on this machine) must be **added** first:

```bash
npx skills add <source> -a claude-code -g        # -g = global (~/.claude/skills); omit for project scope
```

`<source>` accepts GitHub `owner/repo` shorthand or a full URL. For a **private** repo, use the SSH form `git@host:owner/repo.git` so the clone uses your existing git credentials (SSH agent / credential helper).

### Edit and publish an existing skill
1. Edit the source `<name>/SKILL.md` (and any supporting files) in its repo.
2. **Lint** with agnix; fix errors, triage warnings.
3. **Commit + push** to the source repo. Stage only the file(s) you touched (`git add <name>/`) — do **not** `git add -A`; the worktree may carry unrelated in-progress edits on other skills.
4. **Publish** by pulling on each machine: `npx skills update -g -p` (`-g` global, `-p` current project; together = both). Runs cleanly from a SessionStart hook too.
5. **Verify the installed copy**, since that is what the model reads — the source file is not:
   ```bash
   grep "<distinctive phrase from your edit>" ~/.claude/skills/<name>/SKILL.md
   ```

## Rename / delete

`npx skills update` reconciles only skills already tracked in the lockfile — it does **not** install a newly-named skill, and in non-interactive mode (a hook, `-y`, or no TTY) it **skips** deletions. So propagate these explicitly:

- **Rename**: `git mv <old>/ <new>/`, update the `name:` field, grep the repo for inbound references (`rg "<old>" .`), commit + push. Then on each machine `npx skills add <source>` to install the new name and `npx skills remove <old>` to drop the old one.
- **Delete**: `git rm -r <name>/`, grep for references, commit + push. Then `npx skills remove <name>` on each machine (an interactive `npx skills update` will also offer to remove it; a non-interactive one will not).

## Install scopes (npx skills)

- **Scope**: global skills live in `~/.claude/skills/`; project skills in that repo's `.claude/skills/`. `add`/`update`/`remove` default to **project** scope; pass `-g` for global. `update -g -p` does both.
- **Lockfiles**: global → `~/.agents/.skill-lock.json`; project → `<project-root>/skills-lock.json`. They record each installed skill's source; the installed copies under `~/.claude/skills/` carry no lockfile.
