# skills

A public collection of agent skills for [Claude Code](https://claude.com/claude-code), [Codex](https://learn.chatgpt.com/docs/build-skills), and [OpenCode](https://opencode.ai/docs/skills), delivered with [Vercel's `skills` CLI](https://github.com/vercel-labs/skills).

[![test](https://github.com/vtmocanu/skills/actions/workflows/test.yml/badge.svg)](https://github.com/vtmocanu/skills/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/vtmocanu/skills)](https://github.com/vtmocanu/skills/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Each skill is a folder `skills/<name>/SKILL.md` with YAML frontmatter (`name` + `description`). Target Claude Code plus Codex when installing the full catalog: the rolling `skills@latest` CLI writes the canonical store under `~/.agents/skills/<name>/`, which Codex and OpenCode read directly, and symlinks `~/.claude/skills/<name>/` for Claude Code. Restart the target agent if a newly installed skill does not appear.

## Two options, pick one

## 🧰 Just agent-kit

The Claude Code-native agent team plus the full PRD lifecycle (11 skills).

```sh
npx -y skills@latest add vtmocanu/skills/skills/agent-kit -a claude-code -g -y
```

For automatic updates, first install the refresh helper with consumers of the skill store closed:

```sh
npx -y skills@latest add vtmocanu/skills --skill skill-maker -a claude-code codex -g -y
```

**Claude Code auto-update hook, macOS/Linux only** (requires Python 3 with `fcntl`; add to `~/.claude/settings.json`):

```json
"hooks": {
  "SessionStart": [
    {
      "matcher": "startup",
      "hooks": [
        {
          "type": "command",
          "command": "~/.agents/skills/skill-maker/scripts/refresh_skills.py --source vtmocanu/skills/skills/agent-kit",
          "async": true,
          "timeout": 300
        }
      ]
    }
  ]
}
```

| Skill | What it does |
|---|---|
| [agent-team](skills/agent-kit/agent-team/SKILL.md) | Auto-generate and run a per-repo Claude Code agent team: probe the repo, write `.claude/agents/{role}.md` subagent definitions from a role library, then orchestrate tasks with spawned teammates. |
| [prd-create](skills/agent-kit/prd/prd-create/SKILL.md) | Create documentation-first PRDs that guide development through user-facing content. |
| [prd-start](skills/agent-kit/prd/prd-start/SKILL.md) | Start working on a PRD implementation. |
| [prd-next](skills/agent-kit/prd/prd-next/SKILL.md) | Analyze a PRD and recommend the single highest-priority task to work on next. |
| [prd-update-progress](skills/agent-kit/prd/prd-update-progress/SKILL.md) | Update PRD progress from git commits and code changes, enhanced by conversation context. |
| [prd-update-decisions](skills/agent-kit/prd/prd-update-decisions/SKILL.md) | Update a PRD from design decisions and strategic changes made during conversations. |
| [prd-done](skills/agent-kit/prd/prd-done/SKILL.md) | Complete a PRD: create branch, push changes, open a PR, merge, and close the issue. |
| [prd-full](skills/agent-kit/prd/prd-full/SKILL.md) | Run a PRD end-to-end autonomously (start, iterate until done, then PR), stopping after PR creation for review. |
| [prd-close](skills/agent-kit/prd/prd-close/SKILL.md) | Close a PRD that is already implemented or no longer needed. |
| [prd-worktree](skills/agent-kit/prd/prd-worktree/SKILL.md) | Create a git worktree for PRD work with a descriptive branch name (bundles a `create.sh`). |
| [prds-get](skills/agent-kit/prd/prds-get/SKILL.md) | Fetch all open GitHub issues in this project labeled `PRD`. |

Anything later added under `skills/agent-kit/` joins the bundle automatically.

## 📦 All skills

The whole catalog (20 skills), agent-kit included.

```sh
npx -y skills@latest add https://github.com/vtmocanu/skills -a claude-code codex -g -y
```

**Claude Code auto-update hook, macOS/Linux only** (requires Python 3 with `fcntl`; add to `~/.claude/settings.json`):

```json
"hooks": {
  "SessionStart": [
    {
      "matcher": "startup",
      "hooks": [
        {
          "type": "command",
          "command": "~/.agents/skills/skill-maker/scripts/refresh_skills.py",
          "async": true,
          "timeout": 300
        }
      ]
    }
  ]
}
```

| Skill | What it does |
|---|---|
| [cicd-expert](skills/cicd-expert/SKILL.md) | The CI/CD expert. Generate a repo's pipelines through an interactive analyze-then-confirm conversation, or review, harden, debug, and speed up existing CI (supply-chain security, caching, path filters, job DAG, Renovate tool pins). |
| [claude-permissions](skills/claude-permissions/SKILL.md) | Manage Claude Code permissions via Dippy (Bash/MCP allow/ask/deny plus the auto-mode `[ASK]` fallback wrapper, bundled) and settings.json (Read/WebFetch/Skill). |
| [done](skills/done/SKILL.md) | End-of-session wrap-up: check git state across the directories touched this session, review for loose ends, and give a plain verdict on whether the session can be closed. |
| [generate-dockerfile](skills/generate-dockerfile/SKILL.md) | Generate a production-ready, secure, multi-stage Dockerfile and `.dockerignore` for the project. |
| [session-peers](skills/session-peers/SKILL.md) | Make Claude Code sessions and Codex CLI threads on one machine message each other. Async `send` supports handoffs; correlated `ask`/`reply` returns multi-round peer work to the current Codex turn without a stale queued reply; `wait` observes peer state. Codex threads can auto-attach as real peers and aliases follow renames. Auto-attachment requires running `~/.agents/skills/session-peers/scripts/peers.py install-hook --auto-attach` once, then trusting the entry through Codex `/hooks`; see [setup](skills/session-peers/SKILL.md#automatic-attachment-recommended). |
| [reflect](skills/reflect/SKILL.md) | Analyze the current session and propose improvements to the whole authored skill package, including instructions, scripts, tests, references, templates, assets, and hooks, then test and publish the approved changes. |
| [skill-maker](skills/skill-maker/SKILL.md) | Author, lint, and publish portable Claude Code, Codex, and OpenCode skills: canonical `.agents/skills` repo layout, Claude compatibility symlinks, cross-agent refresh hooks, agnix linting, and npx distribution scopes. |
| [token-audit](skills/token-audit/SKILL.md) | Audit a Claude Code setup for token waste (report only, change nothing): measure in-scope CLAUDE.md sizes and @imports, MCP servers/tool counts and whether tool deferral is active, any proxy that silently disables it, model/effort and mid-session switches, output-reducing hooks, per-agent model inheritance, cron/loop intervals vs the measured cache TTL, and the newest session log's cache-read/creation/input/output token split; emit one severity-ranked table plus the single highest-leverage fix. |
| [upgrade-advisor](skills/upgrade-advisor/SKILL.md) | Evaluate a tool, framework, or dependency upgrade: discover the pinned version, find the latest *installable* one, read the changelog across the whole delta, and report which breaking changes actually touch this codebase (by grepping usage), with a safe / blocked / needs-work verdict. |

Plus the 11 [agent-kit](skills/agent-kit/) skills from the table above.

## Notes for both paths

- **Refresh safety:** Vercel installs into temporary projects; the wrapper publishes complete files with atomic replacement. Existing paths stay readable, unchanged files are untouched, and failed staging leaves the live installation intact. Retired supporting files remain available to already-loaded skills. This is atomic per file, not a frozen version of the whole skill. See [refresh safety](skills/skill-maker/references/refresh-safety.md) for the verified race, metadata compatibility, and cleanup limits.
- The wrapper uses `add … --skill '*'` in staging to discover new catalog skills. It also refreshes other tracked global skills and current-project dependencies by their recorded names and refs. It serializes cooperating refresh jobs and leaves repo-authored skills outside `skills-lock.json` untouched. Automatic refresh is optional; for manual use with agents running, invoke the same wrapper. Run direct modifying `npx skills` commands only with consumers of that store closed.
- The full-catalog installer targets `claude-code codex` explicitly. Codex supplies the universal `.agents/skills` destination, Claude receives a symlink to it, and OpenCode reads that same universal destination without another projection. A Claude-only target makes the CLI use copy mode and does not populate `.agents/skills`.
- **Codex auto-update:** point the Codex `SessionStart` handler at the same installed refresher, using `~/.codex/hooks.json` with matcher `startup|resume`, then review and trust it through `/hooks`. For agent-kit-only installs, use the same `--source` argument as the Claude hook. Use the installed script or a thin machine-local trampoline that executes it; a detached copy of its implementation cannot receive updates. The hooks are asynchronous and best-effort: a session may begin with an older inventory, an early session end may cancel refresh, and the next start retries. The [skill-maker hook section](skills/skill-maker/SKILL.md#auto-install-new--refresh-on-session-start-hook) has the JSON contract and multi-source options.
- Renames and removals are **not** auto-pruned in a non-TTY hook; drop an old name with `npx -y skills@latest remove <old> -g -y`.
- Drop `-g` to install into the current project only (`.agents/skills` store plus agent-specific projections such as `.claude/skills`). Add `-l` to list without installing, or `-s a b` (space-separated) to pick a subset.
- Edit the source repo, never an npx-installed `.agents/skills` store or `.claude/skills` projection, which `add`/`update` overwrite.
- Agent definitions are different from skills. There is no portable `.agents/agents` directory; Claude Code, Codex, and OpenCode require separate native agent-definition files. See [`skill-maker`](skills/skill-maker/SKILL.md#agent-definitions-are-not-portable-skill-directories).

## Other files

- [`CLAUDE.example.md`](CLAUDE.example.md): a generic starter for `~/.claude/CLAUDE.md` (Claude Code's global instructions), general AI-collaboration guidance only, no setup specifics. Not a skill; copy what's useful into your own config.
- [`retired/`](retired/): skills no longer maintained or installed, kept for reference. See [retired/README.md](retired/README.md).

## Contributing

Issues and PRs welcome. See [CONTRIBUTING](.github/CONTRIBUTING.md), the [Code of Conduct](.github/CODE_OF_CONDUCT.md), and the [Security Policy](.github/SECURITY.md).

## Legacy: dot-ai

Before the `skills` CLI, these were served by the [dot-ai](https://github.com/vfarcic/dot-ai) generator, which cloned the repo server-side and prefixed every skill as `/dot-ai-<name>`. It still works, but the rolling CLI above is the recommended path.

<details>
<summary>dot-ai install</summary>

```sh
dot-ai skills generate --agent claude-code --path ~/.claude/commands --repo https://github.com/vtmocanu/skills
```

`--repo` composes alongside other sources: each invocation tags its skills with `source:` frontmatter and rewrites only its own slice, so skills from several repos coexist without clobbering each other. Do not run both dot-ai and npx for this repo, or you get duplicate skills (`/dot-ai-reflect` from dot-ai and `/reflect` from npx); to switch, remove the `dot-ai skills generate … --repo …` line from your `SessionStart` hook and keep the npx hook above.

</details>

## Credits

The `prd-*`, `cicd-expert`, and `generate-dockerfile` skills draw on work vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai), created by **Viktor Farcic** and used under the MIT License (Copyright (c) 2025 Viktor Farcic). Most come from its `shared-prompts/` directory; `prd-worktree` comes from `.claude/skills/dot-ai-worktree-prd/` (renamed from `worktree-prd`, with its bundled `create.sh`). The `prd-*` and `generate-dockerfile` skills are copied largely verbatim, converted to the folder `SKILL.md` layout with the dot-ai `category` frontmatter dropped; `cicd-expert` keeps Viktor's interactive generator (formerly the `generate-cicd` skill) and substantially expands it with a security and speed reference set. Each keeps a provenance line pointing back to its source. Thank you to Viktor for the excellent PRD workflow and project generators.

The `agent-team` skill's initial design was based on Viktor's [dot-agent-deck](https://github.com/vfarcic/dot-agent-deck).

## License

[MIT](LICENSE) © Vlad Mocanu. The `prd-*` and `generate-dockerfile` skills, and the interactive generator inside `cicd-expert`, remain © 2025 Viktor Farcic (MIT); see [Credits](#credits).
