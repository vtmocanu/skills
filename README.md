# skills

A public collection of agent skills for [dot-ai](https://github.com/vfarcic/dot-ai) and Claude Code.

[![test](https://github.com/vtmocanu/skills/actions/workflows/test.yml/badge.svg)](https://github.com/vtmocanu/skills/actions/workflows/test.yml)
[![Release](https://img.shields.io/github/v/release/vtmocanu/skills)](https://github.com/vtmocanu/skills/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Each skill lives at the repository root, either as a single Markdown file (`<name>.md`) or as a folder (`<name>/SKILL.md` plus supporting files; supported since dot-ai v1.21.0), with YAML frontmatter (`name` + `description`). dot-ai serves them to your agent: it fetches this repo and generates the skill files into your agent's skills directory. (Skills live at the root because dot-ai's `?repo=` override reads prompts from the repository root.)

## Quick Start

Generate these skills into Claude Code with the [dot-ai CLI](https://github.com/vfarcic/dot-ai-cli) (v1.21.0+), pointed at a running dot-ai server:

```bash
dot-ai skills generate --agent claude-code --repo https://github.com/vtmocanu/skills
```

`--repo` composes alongside other sources: each invocation tags its skills with `source:` frontmatter and rewrites only its own slice, so running it once per repo (typically one agent hook per source) lets skills from several repos coexist without clobbering each other.

## Skills

| Skill | What it does |
|---|---|
| [agent-permissions](agent-permissions/SKILL.md) | Manage an AI coding agent's permissions via Dippy (Bash/MCP allow/ask/deny + the auto-mode `[ASK]` fallback wrapper, bundled) and settings.json (Read/WebFetch/Skill). |
| [agent-team](agent-team/SKILL.md) | Auto-generate and run a per-repo Claude Code agent team: probe the repo, write `.claude/agents/{role}.md` subagent definitions from a role library, then orchestrate tasks with TeamCreate plus spawned teammates. |
| [done](done/SKILL.md) | End-of-session wrap-up: check git state across the directories touched this session, review for loose ends, and give a plain verdict on whether the session can be closed. |
| [reflect](reflect/SKILL.md) | Analyze the current session and propose improvements to the skill that was used, then edit and commit it. |
| [skills](skills/SKILL.md) | Author, lint, and publish Claude Code skills with the `npx skills` package manager: folder `SKILL.md` layout, frontmatter and description limits, design principles, agnix linting, and the add/update/remove scopes. |
| [upgrade-advisor](upgrade-advisor/SKILL.md) | Evaluate a tool, framework, or dependency upgrade: discover the pinned version, find the latest *installable* one, read the changelog across the whole version delta, and report which breaking changes actually touch this codebase (by grepping usage) plus the features and refactors worth adopting, with a safe / blocked / needs-work verdict and a checklist. |

## Other files

- [`CLAUDE.example.md`](CLAUDE.example.md) — a generic starter for `~/.claude/CLAUDE.md` (Claude Code's global instructions): general AI-collaboration guidance only, no setup specifics. Not a skill; copy what's useful into your own config.
- [`retired/`](retired/) — skills no longer maintained or installed, kept for reference. See [retired/README.md](retired/README.md).

## Contributing

Issues and PRs welcome. See [CONTRIBUTING](.github/CONTRIBUTING.md), the [Code of Conduct](.github/CODE_OF_CONDUCT.md), and the [Security Policy](.github/SECURITY.md).

## License

[MIT](LICENSE) © Vlad Mocanu
