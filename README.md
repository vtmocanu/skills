# skills

A public collection of agent skills for [Claude Code](https://claude.com/claude-code) (and other agents), delivered with [vercel's `npx skills`](https://github.com/vercel-labs/skills).

[![test](https://github.com/vtmocanu/skills/actions/workflows/test.yml/badge.svg)](https://github.com/vtmocanu/skills/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/vtmocanu/skills)](https://github.com/vtmocanu/skills/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Each skill is a folder `<name>/SKILL.md` (plus optional supporting files) at the repository root, with YAML frontmatter (`name` + `description`). `npx skills` clones this repo and installs the skills into your agent's skills directory (`~/.claude/skills/<name>/` for Claude Code), where each becomes a `/<name>` slash-command skill. (The older dot-ai-server path still works; see [Legacy: dot-ai](#legacy-dot-ai).)

## Install

Install **all** skills once, globally (every project):

```sh
npx skills add https://github.com/vtmocanu/skills -a claude-code -g
```

They install to `~/.claude/skills/<name>/`; restart Claude Code and the skills (e.g. `/reflect`, `/agent-team`) become available. Drop `-g` to install into the current project only (`.claude/skills/`).

### Specific skills only

Pass `-s` with a comma-separated list to install a subset, or `-l` to list what the repo offers without installing:

```sh
npx skills add https://github.com/vtmocanu/skills -a claude-code -l                    # list, don't install
npx skills add https://github.com/vtmocanu/skills -a claude-code -g -s agent-team,reflect
```

### Auto-update on every session (global hook)

Add a `SessionStart` hook to `~/.claude/settings.json` so the catalog refreshes on each launch:

```json
"hooks": {
  "SessionStart": [
    {
      "matcher": "startup",
      "hooks": [
        {
          "type": "command",
          "command": "npx -y skills@latest update -g -p",
          "async": true,
          "timeout": 120
        }
      ]
    }
  ]
}
```

`update -g -p` refreshes both global and project skills, `@latest` keeps the `skills` CLI current, and `async` keeps it off the startup path. `update` only re-pulls sources already installed via `npx skills add`, so run an `add` above once first. Edit the source repo (the source of truth), never the installed copy under `~/.claude/skills/` (which `update` overwrites).

## Skills

| Skill | What it does |
|---|---|
| [agent-permissions](agent-permissions/SKILL.md) | Manage an AI coding agent's permissions via Dippy (Bash/MCP allow/ask/deny + the auto-mode `[ASK]` fallback wrapper, bundled) and settings.json (Read/WebFetch/Skill). |
| [agent-team](agent-team/SKILL.md) | Auto-generate and run a per-repo Claude Code agent team: probe the repo, write `.claude/agents/{role}.md` subagent definitions from a role library, then orchestrate tasks with TeamCreate plus spawned teammates. |
| [done](done/SKILL.md) | End-of-session wrap-up: check git state across the directories touched this session, review for loose ends, and give a plain verdict on whether the session can be closed. |
| [prd-close](prd-close/SKILL.md) | Close a PRD that is already implemented or no longer needed. `*` |
| [prd-create](prd-create/SKILL.md) | Create documentation-first PRDs that guide development through user-facing content. `*` |
| [prd-done](prd-done/SKILL.md) | Complete a PRD: create branch, push changes, open a PR, merge, and close the issue. `*` |
| [prd-full](prd-full/SKILL.md) | Run a PRD end-to-end autonomously (start, iterate until done, then PR), stopping after PR creation for review. `*` |
| [prd-next](prd-next/SKILL.md) | Analyze a PRD and recommend the single highest-priority task to work on next. `*` |
| [prd-start](prd-start/SKILL.md) | Start working on a PRD implementation. `*` |
| [prd-update-decisions](prd-update-decisions/SKILL.md) | Update a PRD from design decisions and strategic changes made during conversations. `*` |
| [prd-update-progress](prd-update-progress/SKILL.md) | Update PRD progress from git commits and code changes, enhanced by conversation context. `*` |
| [prds-get](prds-get/SKILL.md) | Fetch all open GitHub issues in this project labeled `PRD`. `*` |
| [reflect](reflect/SKILL.md) | Analyze the current session and propose improvements to the skill that was used, then edit and commit it. |
| [skills](skills/SKILL.md) | Author, lint, and publish Claude Code skills with the `npx skills` package manager: folder `SKILL.md` layout, frontmatter and description limits, design principles, agnix linting, and the add/update/remove scopes. |
| [upgrade-advisor](upgrade-advisor/SKILL.md) | Evaluate a tool, framework, or dependency upgrade: discover the pinned version, find the latest *installable* one, read the changelog across the whole version delta, and report which breaking changes actually touch this codebase (by grepping usage) plus the features and refactors worth adopting, with a safe / blocked / needs-work verdict and a checklist. |

## Other files

- [`CLAUDE.example.md`](CLAUDE.example.md) — a generic starter for `~/.claude/CLAUDE.md` (Claude Code's global instructions): general AI-collaboration guidance only, no setup specifics. Not a skill; copy what's useful into your own config.
- [`retired/`](retired/) — skills no longer maintained or installed, kept for reference. See [retired/README.md](retired/README.md).

## Contributing

Issues and PRs welcome. See [CONTRIBUTING](.github/CONTRIBUTING.md), the [Code of Conduct](.github/CODE_OF_CONDUCT.md), and the [Security Policy](.github/SECURITY.md).

## Legacy: dot-ai

Before `npx skills`, these were served by the [dot-ai](https://github.com/vfarcic/dot-ai) generator, which cloned the repo server-side and prefixed every skill as `/dot-ai-<name>`. It still works, but `npx skills` above is the recommended path.

<details>
<summary>dot-ai install</summary>

```sh
dot-ai skills generate --agent claude-code --path ~/.claude/commands --repo https://github.com/vtmocanu/skills
```

`--repo` composes alongside other sources: each invocation tags its skills with `source:` frontmatter and rewrites only its own slice, so skills from several repos coexist without clobbering each other. Do not run both dot-ai and npx for this repo, or you get duplicate skills (`/dot-ai-reflect` from dot-ai and `/reflect` from npx); to switch, remove the `dot-ai skills generate … --repo …` line from your `SessionStart` hook and keep the npx hook above.

</details>

## Credits

`*` The `prd-*` skills are vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) (the `shared-prompts/` directory), created by **Viktor Farcic** and used under the MIT License (Copyright (c) 2025 Viktor Farcic). They are copied largely verbatim, converted to the folder `SKILL.md` layout with the dot-ai `category` frontmatter dropped; each file keeps a provenance line pointing back to its source. Thank you to Viktor for the excellent PRD workflow.

## License

[MIT](LICENSE) © Vlad Mocanu. Vendored `prd-*` skills remain © 2025 Viktor Farcic (MIT); see [Credits](#credits).
