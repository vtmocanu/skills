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
| [agent-team](agent-team/SKILL.md) | Auto-generate and run a per-repo Claude Code agent team: probe the repo, write `.claude/agents/{role}.md` subagent definitions from a role library, then orchestrate tasks with TeamCreate plus spawned teammates. |
| [reflect](reflect.md) | Analyze the current session and propose improvements to the skill that was used, then edit and commit it. |

## Contributing

Issues and PRs welcome. See [CONTRIBUTING](.github/CONTRIBUTING.md), the [Code of Conduct](.github/CODE_OF_CONDUCT.md), and the [Security Policy](.github/SECURITY.md).

## License

[MIT](LICENSE) © Vlad Mocanu
