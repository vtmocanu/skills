---
name: skill-maker
description: Creates, updates, lints, and publishes portable agent skills for Claude Code, Codex, and OpenCode, including repo-local .agents/skills sources, Claude compatibility symlinks, cross-agent SessionStart refresh hooks, and npx distribution. Use when (1) writing or editing a skill, (2) linting with agnix, (3) publishing changes, (4) renaming or deleting a skill, or (5) deciding skill layout, scope, frontmatter, discovery, or refresh automation. Triggers include "new skill", "update skill", "repo skill", "skill not loading", "npx skills", "skills update", "Codex skill hook", "lint skill", "agnix", "SKILL.md".
---

# Skills authoring

Author, lint, and publish skills for Claude Code, Codex, and OpenCode. Skills distributed with the [`skills` package manager](https://github.com/vercel-labs/skills) keep their source in a separate git repo; repo-owned local skills can instead live directly in the consuming repo.

**Package-manager policy: rolling `skills@latest`.** Use `npx -y skills@latest` for the interactive workstation commands and SessionStart hooks in this skill and its README examples. This deliberately lets newly published third-party installer code execute unattended at session start in exchange for automatic package-manager fixes and features; skill source URLs and refs remain controlled independently. Pin the package manager instead in CI, privileged automation, or another environment that requires reviewed, reproducible executable code.

## Classify the skill before editing

A skill path can be an authored source or a package-manager-owned installed copy. Edit only an authored source.

| Kind | Where | Edit it? |
|---|---|---|
| **Published source** | the skill's source repo (`<name>/SKILL.md`) | **Yes** |
| **Repo-local source** | `<repo>/.agents/skills/<name>/` for a cross-agent skill; `<repo>/.claude/skills/<name>/` only when intentionally Claude-only | **Yes** |
| **npx-installed copy** | a global or project target directory, or the canonical `.agents/skills` store plus projections | **No** — derived; `npx -y skills@latest add`/`update` overwrites it |

For a published skill, publishing is pull-based, not live editing. **Change the source repo, then `npx -y skills@latest update` re-pulls** and rewrites the installed copy. An edit made directly to the installed copy is discarded on the next update. For a repo-local source, commit the real directory and its compatibility symlink in the consuming repo; no package-manager install step applies.

- **If you own the source repo**: edit `<name>/SKILL.md`, commit, push, then `npx -y skills@latest update`.
- **If you don't** (you only installed it): the installed copy is read-only in practice. To change it, fork the source repo and `npx -y skills@latest add <your-fork>`, or open a PR/issue upstream. You cannot push to a repo you don't own.

To find where an installed skill came from, its source is recorded in the lockfile: `~/.agents/.skill-lock.json` for global installs (or `$XDG_STATE_HOME/skills/.skill-lock.json` if set), and `<project-root>/skills-lock.json` for project installs. If the name is present there, treat the on-disk skill as derived even when it sits under `.agents/skills` and looks like an ordinary directory.

## Repo-local skills for Claude Code, Codex, and OpenCode

For a skill authored by and useful only in one repository, keep the canonical real directory at the repository root under `.agents/skills`. Codex [scans `.agents/skills` from the current directory through the repository root and supports symlinked skill folders](https://learn.chatgpt.com/docs/build-skills), and [OpenCode scans the same project and global paths](https://opencode.ai/docs/skills). Claude Code discovers project skills under `.claude/skills`, so expose the same bytes through one of these two tracked layouts.

### One directory symlink

Prefer this simpler form when every repo skill is cross-agent and the repository never uses project-scope `npx skills add`. Confirm that no `skills-lock.json` exists:

```text
<repo>/
├── .agents/skills/
│   └── <name>/
│       ├── SKILL.md
│       └── scripts/                  # optional
└── .claude/skills -> ../.agents/skills
```

When all existing skills are in a real `.claude/skills` directory and `.agents/skills` does not yet exist, preserve their history while moving the whole tree:

```bash
mkdir -p .agents
git mv .claude/skills .agents/skills
ln -s ../.agents/skills .claude/skills
```

If `.agents/skills` already contains a skill, move the remaining skill directories by name with `git mv`, then replace the empty `.claude/skills` directory with the relative symlink.

This form has two costs. First, every skill becomes visible to Codex and OpenCode, so it cannot keep a Claude-only skill private to Claude. Second, do not use it with project-scope npx installs: the package manager's `.claude/skills/<name>` projection resolves on top of the canonical `.agents/skills/<name>` store instead of remaining a distinct projection. Use the per-skill form below when either cost applies.

The directory form was verified on 2026-09-08 in [vtmocanu/uzi#1204](https://github.com/vtmocanu/uzi/pull/1204): a Claude Code session launched in that checkout discovered the skills through the directory symlink, while Codex read the real `.agents/skills` tree.

### Per-skill symlinks

Use this form when the repository mixes cross-agent and Claude-only skills, or when project-scope npx installs exist. Keep `.claude/skills` itself as a real directory so cross-agent symlinks, Claude-only skills, and npx-managed projections can coexist:

```text
<repo>/
├── .agents/skills/<name>/
│   ├── SKILL.md
│   └── scripts/                  # optional
└── .claude/skills/<name> -> ../../.agents/skills/<name>
```

For a new root-scoped cross-agent skill, create the real folder first, then the Claude projection:

```bash
mkdir -p .agents/skills/<name> .claude/skills
ln -s ../../.agents/skills/<name> .claude/skills/<name>
```

For an existing repo-authored Claude skill, first confirm its name is absent from `skills-lock.json`, then preserve history while moving it:

```bash
mkdir -p .agents/skills
git mv .claude/skills/<name> .agents/skills/<name>
ln -s ../../.agents/skills/<name> .claude/skills/<name>
```

Track both the real directory and the symlink. Do not relocate a lockfile-owned dependency this way; change its upstream source or package selection and let npx regenerate the store and projections. Leave a genuinely Claude-only skill as a real `.claude/skills/<name>` directory.

Under either layout, use `<repo>/.agents/skills`, not `~/.agents/skills`: the tilde path is user-global and would make a repository contract machine-local. A shared path makes one body discoverable; it does not translate harness-specific behavior. Before declaring a skill cross-agent, inspect its frontmatter, tool names, slash commands, lifecycle assumptions, and literal `.claude/skills/...` paths. Keep `name`, `description`, and the body portable; retain host-specific metadata only when that host needs it, and never rely on another host ignoring a field as a security boundary. Reference supporting files through the runtime-provided skill base directory rather than hardcoding either discovery path.

Product caveat: uzi's worker currently enumerates repo skills only from a real `.claude/skills` directory and skips both symlink forms under its ADR-0246 containment guard. A repository with `repo_skills_enabled` therefore loses those skills in worker runs until [vtmocanu/uzi#1205](https://github.com/vtmocanu/uzi/issues/1205) adds `.agents/skills` as a second real-directory root.

### Agent definitions are not portable skill directories

Do not generalize the `.agents/skills` layout to `.agents/agents`. There is no shared agent-definition discovery path or file schema across these runtimes:

- Claude Code reads Markdown agent definitions from `.claude/agents/`.
- Codex reads TOML custom-agent definitions from [`.codex/agents/`](https://learn.chatgpt.com/docs/agent-configuration/subagents); each file requires `name`, `description`, and `developer_instructions`.
- OpenCode reads Markdown agent definitions from [`.opencode/agents/`](https://opencode.ai/docs/agents) with OpenCode-specific frontmatter.

Do not symlink one agent-definition file across those directories. For a cross-agent role catalog, keep neutral source data such as `roles.yaml`, then generate and validate one native projection per runtime. The `agent-team` skill is intentionally Claude Code-native until it has those adapters.

## Layout: folder skills

Author every skill as a **folder with a `SKILL.md` inside** — the `skills` CLI only discovers `<name>/SKILL.md`, never a bare `<name>.md`:

```text
<name>/
├── SKILL.md            # required — frontmatter + body
├── scripts/foo.sh      # optional supporting files
└── references/bar.md   # optional
```

Reference supporting files by the **skill's own base directory**, which the harness provides when the skill loads (a `Base directory for this skill: …` line). For example `<this skill's directory>/scripts/foo.sh`. Do **not** use a bare relative path like `./scripts/foo.sh`: at runtime the working directory is the user's project, not the skill directory, so `./` resolves to the wrong place. Invoke bundled scripts directly (`<dir>/scan.sh`, not `bash <dir>/scan.sh`) — write them executable (`0o755`); direct invocation matches Claude Code's `Bash(/path/to/scan.sh:*)` permission scoping, while `bash …` would need the far broader `Bash(bash:*)`.

Supporting files are the reason to use a folder. A folder containing only `SKILL.md` behaves like a flat prompt — still use the folder form so npx discovers it.

### Nesting under a subpath needs a `.claude-plugin/plugin.json` manifest

A whole-repo install (container `skills/`) discovers catalog nesting up to 3 levels deep (`skills/<cat>/<name>/SKILL.md`, `skills/<cat>/<cat>/<name>/SKILL.md`) with nothing extra. A **subpath** install (`npx -y skills@latest add owner/repo/skills/<sub>`) does not: the CLI walks the subpath dir only **one level deep**, so a skill at `<sub>/<group>/<name>/SKILL.md` is **silently dropped** (no error; the bundle just omits it). This depth cap is independent of the fetch engine — the CLI uses its fast GitHub tree-API only for a hardcoded owner allowlist (`vercel`, `vercel-labs`, `heygen-com`, plus a couple of self-hosted repos) and **clones every other owner** and walks the filesystem, and both paths cap the subpath at one level.

Fix: a **`<sub>/.claude-plugin/plugin.json`** manifest. Manifest-declared skill paths bypass the depth walk (npx: *"searched at their declared depth, not subject to the bounded depth-3 catalog walk"*).

```json
// skills/agent-kit/.claude-plugin/plugin.json
{ "name": "agent-kit", "skills": ["./agent-team", "./prd/prd-create", "./prd/prds-get"] }
```

The CLI resolves each entry under the subpath dir and registers its **parent** directory, then walks that dir one level for `SKILL.md`. So one entry `./prd/<any-skill>` registers the whole `prd/` folder and finds every skill directly inside it — new skills dropped into an already-registered folder **auto-join with no manifest edit**. The gap the manifest does not close on its own: a brand-new nested folder that no entry registers. Guard it in CI with a coverage check that recomputes what the subpath install would find and fails if any on-disk `SKILL.md` is unreachable (see `scripts/check_bundle_coverage.py`).

Verified 2026-08-13 on the `skills/agent-kit` bundle, default-branch install (no `--branch`): nested `prd/*` **without** the manifest resolved to only the one depth-1 skill; **with** the manifest, all eleven installed. Verify layout changes with a real default-branch `add` (e.g. push to a throwaway repo's default branch) — `add --branch <x>` and `add … -l` can take a different code path than the plain default-branch install a SessionStart hook runs, so a green result there is not proof the hook will agree.

## Frontmatter

`name`:
- **Required.** It is the install/display name; the directory name is only a fallback.
- Max 64 chars; lowercase letters, numbers, hyphens only; no XML tags.
- **Reserved substrings `anthropic`, `claude`** — do not use them in `name:`.

`description` — this is where discovery happens, get it right:
- **Single line.** Keep the description to one line for portability (the Anthropic Skills API) and clean auto-invocation.
- **No unquoted `: ` (colon-space).** A colon-space in a plain YAML scalar reads as a mapping indicator: the `skills` CLI parser rejects it (`mapping values are not allowed here`), returns null, and **silently skips the skill** — a `⚠ Skipped` warning, no error, exit 0 — so it never installs. Reword to avoid `: `, or wrap the whole description in double quotes. dot-ai's parser tolerated this, so a skill that installed under dot-ai can vanish under the package manager.
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

Before committing any skill change, resolve `<skill-file>` to the actual authored source: `<name>/SKILL.md` for a published folder source, or `.agents/skills/<name>/SKILL.md` for a repo-local cross-agent source. Then lint it — agnix catches token bloat, weak language, ambiguous instructions, and bad keyword placement:

```bash
agnix --target claude-code <skill-file>
```

For a cross-agent skill, validate both consumers:

```bash
agnix --target claude-code <skill-file>
agnix --target codex <skill-file>
```

`--target` still works and is listed in `agnix --help`, but recent agnix versions print `Field 'target' is deprecated` and steer toward a config-file `tools` array (there is no `--tools` CLI flag); the warning is benign. To silence it in a repo you lint often, add an `.agnix.toml` (`agnix init`) with `tools = ["claude-code"]`, then drop the flag: `agnix <skill-file>`.

Add `--show-fixes` to preview rewrites, or `--fix-safe` for high-confidence ones (always re-read the diff — "fixable" ≠ "correct in context"). **Errors must be fixed before commit; warnings are advisory** — fix the real ones. **Pre-existing warnings count**: when a file is open for a real edit, surface warnings that predate your change and propose fixing them in the same commit, rather than re-committing a file with the same warning count forever.

## Workflow

**First, check whether the source is already wired into a SessionStart hook.** Inspect both `~/.claude/settings.json` and `~/.codex/hooks.json`; a dual-agent setup may point them at a shared wrapper, so inspect that wrapper for `npx -y skills@latest add <source> --skill '*'` too. If it is wired, publishing a new or edited skill to that source is just **commit + push**: the hook's `add --skill '*'` installs new skills and overwrites existing ones on the next session start, so the manual `add` / `update` steps below are redundant for that source. Run them by hand only for immediate use in the **current** session, or for a source with no such hook. Two things the hook never does, so still do them by hand: prune a removed or renamed skill (`npx -y skills@latest remove`), and set any machine-local `skillOverrides` state such as `name-only` in `settings.json`.

### Install a source the first time
`npx -y skills@latest update` only refreshes skills already recorded in the lockfile, so a brand-new skill (or a source never installed on this machine) must be **added** first:

```bash
npx -y skills@latest add <source> -a claude-code codex -g  # canonical store + Claude projection; omit -g for project scope
```

`<source>` accepts GitHub `owner/repo` shorthand or a full URL. For a **private** repo, use the SSH form `git@host:owner/repo.git` so the clone uses your existing git credentials (SSH agent / credential helper).

- **Audit a source without installing**: `npx -y skills@latest add <source> -l` lists every skill the repo offers (name + description). Use it to survey a source, or to spot skills added upstream — `update` never discovers new skills, so this is how you learn one exists.
- **`--skill` is include-only; there is no exclude flag.** To install all-but-some, either pass a positive name list (`--skill a b c`) or install `--skill '*'` then `npx -y skills@latest remove <name> -g -y`. Reason to exclude: a skill whose `name:` duplicates a built-in (see the name-collision caveat under Enable / disable).

### Auto-install new + refresh on session start (hook)
`update` never discovers a skill not yet in the lockfile, so a SessionStart hook that only runs `update` will not pick up a newly-pushed skill. To auto-install new skills **and** refresh existing ones, run `add` (with `--skill '*'`) then `update`, chained:

```bash
npx -y skills@latest add <source> -a claude-code codex --skill '*' -g -y && npx -y skills@latest update -g -p
```

The explicit pair is load-bearing. In `skills` CLI 1.5.23, targeting only the non-universal `claude-code` directory selects copy mode and writes only `.claude/skills`; no canonical `.agents/skills` entry exists for Codex or OpenCode to discover. Targeting `claude-code codex` selects canonical-plus-symlink mode: Codex supplies the universal `.agents/skills` target, Claude gets a symlink, and OpenCode discovers the same canonical store without a redundant third target. This was reproduced in isolated global installs on 2026-09-08. Re-run that probe when changing the rolling package-manager version.

Claude Code and Codex share `~/.agents/.skill-lock.json` and the `~/.agents/skills` store. Their SessionStart hooks—and two Claude sessions starting together—can therefore run this read-modify-write sequence concurrently. This skill ships `scripts/refresh_skills.py`, a macOS/Linux Python-stdlib wrapper that creates `~/.agents`, holds `fcntl.flock` on `~/.agents/.skills-refresh.lock` for the entire source-add/update sequence, and preserves the session working directory so `update -p` refreshes the repository that started the hook. The default source is this public skills catalog; repeat `--source` for another required source or `--best-effort-source` for an optional one.

After installing `skill-maker`, use its actual installed script path directly, or copy it to another stable executable path. Do not configure either hook until that path exists and is executable. Point every agent hook at the same wrapper invocation; this is the serialization boundary.

Use these hook locations and matchers:

| Agent | Hook file | `SessionStart` matcher | Extra step |
|---|---|---|---|
| Claude Code | `~/.claude/settings.json` | `startup` | none |
| Codex | `~/.codex/hooks.json` | `startup\|resume` | review and trust the handler once with `/hooks` |

For Codex, add the wrapper as another `SessionStart` group without reordering existing groups, so their saved trust identities stay stable:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": "~/.agents/skills/skill-maker/scripts/refresh_skills.py",
            "timeout": 300,
            "async": true
          }
        ]
      }
    ]
  }
}
```

Codex user hooks are loaded from `~/.codex/hooks.json`, and a new or changed hook stays skipped until trusted through `/hooks`; see the [official hooks documentation](https://learn.chatgpt.com/docs/hooks). The example is asynchronous and therefore best-effort: the session can begin with the previous skill inventory, Codex cancels an unfinished background hook when the session ends, and the next `startup` or `resume` retries. Remove `async` when refresh completion must gate session startup. When Claude and Codex call the same multi-source wrapper, give both a timeout sized for the whole source list; `300` seconds is the example baseline.

- `add … --skill '*'` installs every skill currently in `<source>`, so new ones land automatically. `--skill '*'` keeps the explicit `-a claude-code codex` scope; `--all` instead fans out to every supported agent.
- **`update` reinstalls to every *detected* agent, and cannot be scoped.** `update` has no `-a` flag, and its internal `add` (run per changed skill) passes none — so it reinstalls each changed skill to **every** agent it detects. Detection is just "the agent's config dir exists" (e.g. `~/.config/crush`, `~/.codex`). Non-universal agents (claude, crush) each get their own copy; universal ones share `~/.agents/skills`. Consequence: even a hook whose every `add` is `-a claude-code` still leaks copies to other agents through the chained `update`. There is no per-`update` agent scope — the only way to keep installs to one agent is to make the others undetectable (remove/rename their config dir).
- Keep `add` and `update` inside one serialized wrapper invocation. Separate async handlers, or matching Claude and Codex handlers without the shared lock, can race on the lockfile.
- For multiple sources, chain the `add`s ahead of one `update`. Join reliable steps with `&&`, but decouple any source that can be unreachable (offline, VPN-gated) with `;` and `|| true` and put it **last** — an `&&` chain aborts on the first failure, so a down source would otherwise block every step after it:

  ```bash
  npx -y skills@latest add <reliable-source> -a claude-code codex --skill '*' -g -y && npx -y skills@latest update -g -p; npx -y skills@latest add <vpn-only-source> -a claude-code codex --skill '*' -g -y || true
  ```

- Removals and renames are still not auto-pruned in a non-TTY hook (see Rename / delete) — drop the old name with `npx -y skills@latest remove <old> -g -y`.

### Edit and publish an existing skill
1. Edit the source `<name>/SKILL.md` (and any supporting files) in its repo.
2. **Lint** with agnix; fix errors, triage warnings.
3. **Commit + push** to the source repo. Stage only the file(s) you touched (`git add <name>/`) — do **not** `git add -A`; the worktree may carry unrelated in-progress edits on other skills.
4. **Publish** by pulling on each machine: `npx -y skills@latest update -g -p` (`-g` global, `-p` current project; together = both). Runs cleanly from a SessionStart hook too.
5. **Verify the canonical installed store and each target projection**, since the source file is not what the model reads:
   ```bash
   grep "<distinctive phrase from your edit>" ~/.agents/skills/<name>/SKILL.md
   grep "<distinctive phrase from your edit>" ~/.claude/skills/<name>/SKILL.md
   ```

## Rename / delete

`npx -y skills@latest update` reconciles only skills already tracked in the lockfile — it does **not** install a newly-named skill, and it **never prunes automatically**. On a deletion a non-interactive run (a hook, or no TTY) prints `Skipping deletion in non-interactive mode` and leaves the skill on disk and in the lockfile; `-y` does not change this, and there is no `--prune` flag. So removal is always an explicit `npx -y skills@latest remove`:

- **Rename**: `git mv <old>/ <new>/`, update the `name:` field, grep the repo for inbound references (`rg "<old>" .`), commit + push. Then on each machine `npx -y skills@latest add <source>` to install the new name and `npx -y skills@latest remove <old> -g -y` to drop the old one.
- **Delete**: `git rm -r <name>/`, grep for references, commit + push. Then `npx -y skills@latest remove <name> -g -y` on each machine (an interactive `npx -y skills@latest update` will also offer to remove it; a non-interactive one will not).

`remove` takes skill **names** (space-separated) or `--all`; there is no `--source <repo>` filter, so name each skill to drop. Flags mirror the others: `-g` global, `-a <agent>` to scope to one agent, `-y` to skip the confirm.

## Install scopes (`skills` CLI)

- **Scope**: when the target set includes a universal agent such as Codex, npx keeps the canonical installed store under `.agents/skills` and creates agent-specific projections such as `.claude/skills`. A single non-universal target can use copy mode and skip the canonical store entirely. Global installs use `$HOME`; project installs use the repository. `add`/`update`/`remove` default to **project** scope; pass `-g` for global. `update -g -p` does both.
- **Lockfiles**: global → `~/.agents/.skill-lock.json`; project → `<project-root>/skills-lock.json`. They record each installed skill's source; the installed copies under `~/.claude/skills/` carry no lockfile.
- **Where files land (shared store + target projections)**: with `-a claude-code codex`, global real files live under `~/.agents/skills/<name>/`, project real files live under `<repo>/.agents/skills/<name>/`, and Claude receives the matching `.claude/skills/<name>` symlink. OpenCode reads the canonical store directly. Other target combinations may choose copy mode instead, so verify the exact paths after changing the target list. **Dotfiles impact**: track the global shared store if desired (for example by symlinking `~/.agents` into a dotfiles repo) and gitignore the machine-local global lockfile, which rewrites on every add/update.
- **Authored repo-local skills are different**: a repo-owned skill absent from `skills-lock.json` uses `<repo>/.agents/skills/<name>` as its editable source and a deliberately tracked `.claude/skills/<name>` symlink. npx does not own or rewrite that pair.

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
