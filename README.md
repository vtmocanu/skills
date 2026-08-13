# skills

A public collection of agent skills for [Claude Code](https://claude.com/claude-code) (and other agents), delivered with [vercel's `npx skills`](https://github.com/vercel-labs/skills).

[![test](https://github.com/vtmocanu/skills/actions/workflows/test.yml/badge.svg)](https://github.com/vtmocanu/skills/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/vtmocanu/skills)](https://github.com/vtmocanu/skills/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Each skill is a folder `skills/<name>/SKILL.md` with YAML frontmatter (`name` + `description`). `npx skills` installs them to `~/.claude/skills/<name>/`, where each becomes a `/<name>` slash-command skill. Restart Claude Code after installing.

## Two options, pick one

## 🧰 Just agent-kit

The agent team plus the full PRD lifecycle (11 skills).

```sh
npx skills add vtmocanu/skills/skills/agent-kit -a claude-code -g -y
```

**Auto-update hook** (add to `~/.claude/settings.json`):

```json
"hooks": {
  "SessionStart": [
    {
      "matcher": "startup",
      "hooks": [
        {
          "type": "command",
          "command": "npx -y skills@latest add vtmocanu/skills/skills/agent-kit -a claude-code --skill '*' -g -y && npx -y skills@latest update -g -p",
          "async": true,
          "timeout": 180
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

The whole catalog (18 skills), agent-kit included.

```sh
npx skills add https://github.com/vtmocanu/skills -a claude-code -g -y
```

**Auto-update hook** (add to `~/.claude/settings.json`):

```json
"hooks": {
  "SessionStart": [
    {
      "matcher": "startup",
      "hooks": [
        {
          "type": "command",
          "command": "npx -y skills@latest add https://github.com/vtmocanu/skills -a claude-code --skill '*' -g -y && npx -y skills@latest update -g -p",
          "async": true,
          "timeout": 180
        }
      ]
    }
  ]
}
```

| Skill | What it does |
|---|---|
| [claude-permissions](skills/claude-permissions/SKILL.md) | Manage Claude Code permissions via Dippy (Bash/MCP allow/ask/deny plus the auto-mode `[ASK]` fallback wrapper, bundled) and settings.json (Read/WebFetch/Skill). |
| [done](skills/done/SKILL.md) | End-of-session wrap-up: check git state across the directories touched this session, review for loose ends, and give a plain verdict on whether the session can be closed. |
| [generate-cicd](skills/generate-cicd/SKILL.md) | Generate CI/CD workflows through an interactive conversation that analyzes the repo structure and your preferences. |
| [generate-dockerfile](skills/generate-dockerfile/SKILL.md) | Generate a production-ready, secure, multi-stage Dockerfile and `.dockerignore` for the project. |
| [reflect](skills/reflect/SKILL.md) | Analyze the current session and propose improvements to the skill that was used, then edit and commit it. |
| [skill-maker](skills/skill-maker/SKILL.md) | Author, lint, and publish Claude Code skills with the `npx skills` package manager: folder `SKILL.md` layout, frontmatter and description limits, design principles, agnix linting, and the add/update/remove scopes. |
| [upgrade-advisor](skills/upgrade-advisor/SKILL.md) | Evaluate a tool, framework, or dependency upgrade: discover the pinned version, find the latest *installable* one, read the changelog across the whole delta, and report which breaking changes actually touch this codebase (by grepping usage), with a safe / blocked / needs-work verdict. |

Plus the 11 [agent-kit](skills/agent-kit/) skills from the table above.

## Notes for both paths

- The hook chains `add … --skill '*'` with `update` because `update` alone never discovers a **new** skill, it only refreshes ones already in the lockfile; the `add` step picks up anything the source has added. `&&` (not two hooks) keeps the two lockfile writers from racing.
- Renames and removals are **not** auto-pruned in a non-TTY hook; drop an old name with `npx skills remove <old> -g -y`.
- Drop `-g` to install into the current project only (`.claude/skills/`). Add `-l` to list without installing, or `-s a b` (space-separated) to pick a subset.
- Edit the source repo, never the installed copy under `~/.claude/skills/`, which `update` overwrites.

## Other files

- [`CLAUDE.example.md`](CLAUDE.example.md): a generic starter for `~/.claude/CLAUDE.md` (Claude Code's global instructions), general AI-collaboration guidance only, no setup specifics. Not a skill; copy what's useful into your own config.
- [`retired/`](retired/): skills no longer maintained or installed, kept for reference. See [retired/README.md](retired/README.md).

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

The `prd-*`, `generate-cicd`, and `generate-dockerfile` skills are vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai), created by **Viktor Farcic** and used under the MIT License (Copyright (c) 2025 Viktor Farcic). Most come from its `shared-prompts/` directory; `prd-worktree` comes from `.claude/skills/dot-ai-worktree-prd/` (renamed from `worktree-prd`, with its bundled `create.sh`). They are copied largely verbatim, converted to the folder `SKILL.md` layout with the dot-ai `category` frontmatter dropped; each keeps a provenance line pointing back to its source. Thank you to Viktor for the excellent PRD workflow and project generators.

The `agent-team` skill's initial design was based on Viktor's [dot-agent-deck](https://github.com/vfarcic/dot-agent-deck).

## License

[MIT](LICENSE) © Vlad Mocanu. Vendored `prd-*`, `generate-cicd`, and `generate-dockerfile` skills remain © 2025 Viktor Farcic (MIT); see [Credits](#credits).
