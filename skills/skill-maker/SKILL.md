---
name: skill-maker
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

`--target` still works and is listed in `agnix --help`, but recent agnix versions print `Field 'target' is deprecated` and steer toward a config-file `tools` array (there is no `--tools` CLI flag); the warning is benign. To silence it in a repo you lint often, add an `.agnix.toml` (`agnix init`) with `tools = ["claude-code"]`, then drop the flag: `agnix <name>/SKILL.md`.

Add `--show-fixes` to preview rewrites, or `--fix-safe` for high-confidence ones (always re-read the diff — "fixable" ≠ "correct in context"). **Errors must be fixed before commit; warnings are advisory** — fix the real ones. **Pre-existing warnings count**: when a file is open for a real edit, surface warnings that predate your change and propose fixing them in the same commit, rather than re-committing a file with the same warning count forever.

## Workflow

### Install a source the first time
`npx skills update` only refreshes skills already recorded in the lockfile, so a brand-new skill (or a source never installed on this machine) must be **added** first:

```bash
npx skills add <source> -a claude-code -g        # -g = global (~/.claude/skills); omit for project scope
```

`<source>` accepts GitHub `owner/repo` shorthand or a full URL. For a **private** repo, use the SSH form `git@host:owner/repo.git` so the clone uses your existing git credentials (SSH agent / credential helper).

- **Audit a source without installing**: `npx skills add <source> -l` lists every skill the repo offers (name + description). Use it to survey a source, or to spot skills added upstream — `update` never discovers new skills, so this is how you learn one exists.
- **`--skill` is include-only; there is no exclude flag.** To install all-but-some, either pass a positive name list (`--skill a b c`) or install `--skill '*'` then `npx skills remove <name> -g -y`. Reason to exclude: a skill whose `name:` duplicates a built-in (see the name-collision caveat under Enable / disable).

### Auto-install new + refresh on session start (hook)
`update` never discovers a skill not yet in the lockfile, so a SessionStart hook that only runs `update` will not pick up a newly-pushed skill. To auto-install new skills **and** refresh existing ones, run `add` (with `--skill '*'`) then `update`, chained:

```bash
npx -y skills@latest add <source> -a claude-code --skill '*' -g -y && npx -y skills@latest update -g -p
```

- `add … --skill '*'` installs every skill currently in `<source>`, so new ones land automatically. `--skill '*'` keeps the `-a claude-code` agent scope; `--all` instead fans out to every detected agent.
- **`update` reinstalls to every *detected* agent, and cannot be scoped.** `update` has no `-a` flag, and its internal `add` (run per changed skill) passes none — so it reinstalls each changed skill to **every** agent it detects. Detection is just "the agent's config dir exists" (e.g. `~/.config/crush`, `~/.codex`). Non-universal agents (claude, crush) each get their own copy; universal ones share `~/.agents/skills`. Consequence: even a hook whose every `add` is `-a claude-code` still leaks copies to other agents through the chained `update`. There is no per-`update` agent scope — the only way to keep installs to one agent is to make the others undetectable (remove/rename their config dir).
- Keep it a **single shell command**, not two async hooks: `add` and `update` both write the lockfile, so two concurrent hooks race and corrupt it.
- For multiple sources, chain the `add`s ahead of one `update`. Join reliable steps with `&&`, but decouple any source that can be unreachable (offline, VPN-gated) with `;` and `|| true` and put it **last** — an `&&` chain aborts on the first failure, so a down source would otherwise block every step after it:

  ```bash
  npx -y skills@latest add <reliable-source> -a claude-code --skill '*' -g -y && npx -y skills@latest update -g -p; npx -y skills@latest add <vpn-only-source> -a claude-code --skill '*' -g -y || true
  ```

- Removals and renames are still not auto-pruned in a non-TTY hook (see Rename / delete) — drop the old name with `npx skills remove <old> -g -y`.

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

`npx skills update` reconciles only skills already tracked in the lockfile — it does **not** install a newly-named skill, and it **never prunes automatically**. On a deletion a non-interactive run (a hook, or no TTY) prints `Skipping deletion in non-interactive mode` and leaves the skill on disk and in the lockfile; `-y` does not change this, and there is no `--prune` flag. So removal is always an explicit `npx skills remove`:

- **Rename**: `git mv <old>/ <new>/`, update the `name:` field, grep the repo for inbound references (`rg "<old>" .`), commit + push. Then on each machine `npx skills add <source>` to install the new name and `npx skills remove <old> -g -y` to drop the old one.
- **Delete**: `git rm -r <name>/`, grep for references, commit + push. Then `npx skills remove <name> -g -y` on each machine (an interactive `npx skills update` will also offer to remove it; a non-interactive one will not).

`remove` takes skill **names** (space-separated) or `--all`; there is no `--source <repo>` filter, so name each skill to drop. Flags mirror the others: `-g` global, `-a <agent>` to scope to one agent, `-y` to skip the confirm.

## Install scopes (npx skills)

- **Scope**: global skills live in `~/.claude/skills/`; project skills in that repo's `.claude/skills/`. `add`/`update`/`remove` default to **project** scope; pass `-g` for global. `update -g -p` does both.
- **Lockfiles**: global → `~/.agents/.skill-lock.json`; project → `<project-root>/skills-lock.json`. They record each installed skill's source; the installed copies under `~/.claude/skills/` carry no lockfile.
- **Where files land (shared store + unstable per-agent copies)**: `npx` keeps each skill's real files once under the shared **`~/.agents/skills/<name>/`** and also installs a per-agent copy under each detected agent (e.g. `~/.claude/skills/<name>/`). The per-agent form is **unstable**: on each `update`, the skill being re-pulled is written as a **symlink** into the shared store (`~/.claude/skills/<name>` → `../../../../../../../.agents/skills/<name>`), while every other skill is reinstalled as a **real-dir copy**, so real-dir vs symlink reshuffles across the per-agent copies on every run; there is no stable committed form for them. **Dotfiles impact**: track the shared store `~/.agents/skills` as the source of truth (e.g. symlink `~/.agents` into the repo) and gitignore the machine-local `~/.agents/.skill-lock.json` (it rewrites on every add/update). Do **not** pin the per-agent `~/.claude/skills/<name>` copies to symlinks; npx flips them back to real dirs, so leave them as npx's native real dirs or gitignore them.

## Enable / disable an installed skill

npx has no enable/disable — a skill is installed (active) or removed. To keep a skill on disk but **turn it off**, use Claude Code's `skillOverrides` in `~/.claude/settings.json`. It is a four-state control, keyed by skill **name** (not source), the richer analogue of a plugin's boolean `enabledPlugins`:

| State | Listed to the model | In `/` menu | Auto-trigger | Manual `/name` |
|-------|--------------------|-------------|--------------|----------------|
| `on` (default) | name + description | yes | yes | yes |
| `name-only` | name only (saves context) | yes | yes | yes |
| `user-invocable-only` | hidden | yes | no | yes |
| `off` | hidden | hidden | no | no |

Cycle states live in the `/skills` menu (Space to cycle, Enter to save) or edit `skillOverrides` directly. `skillOverrides` persists in settings.json, so the enable/disable state is version-controllable.

- Prefer `off` over `permissions.deny: ["Skill(<name>)"]` to disable: deny only blocks at call time and **still surfaces** the skill in context; `off` removes it entirely (no context cost, no auto-trigger).
- **Name-collision caveat**: because overrides key by name, never install a skill whose `name:` duplicates a built-in (e.g. `claude-api`) — a single `"claude-api": "off"` would target the built-in too. Exclude such a skill at install (see the include-only note under Install a source).
