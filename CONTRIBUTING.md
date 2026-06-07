# Contributing to skills

Thanks for your interest in contributing! Issues and pull requests are welcome.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Adding or Editing a Skill](#adding-or-editing-a-skill)
- [Validating](#validating)
- [Pull Request Process](#pull-request-process)
- [Skill Authoring Standards](#skill-authoring-standards)

## Code of Conduct

This project adheres to a [Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold it.

## Ways to Contribute

- **Report bugs** in a skill (a step that misfires, an outdated instruction).
- **Suggest or add skills** that are broadly useful and self-contained.
- **Improve the wording** of existing skills.
- **Review pull requests.**

## Adding or Editing a Skill

Each skill is a single Markdown file:

- **Flat skill**: `skills/<name>.md`
- **Folder skill** (with supporting files): `skills/<name>/SKILL.md`

Every skill starts with YAML frontmatter:

```yaml
---
name: <name>
description: <one line: what it does and when to use it>
---
```

1. Fork the repository and create a branch: `git checkout -b add-<skill>`.
2. Add or edit the skill file under `skills/`.
3. Run the validator (below).
4. Open a pull request.

## Validating

```bash
python3 scripts/validate_skills.py skills/
```

CI runs the same check on every push and pull request.

## Pull Request Process

Include in your pull request description: **what** changed, **why**, and any related issue. Keep pull requests focused on a single concern. Automated checks must pass; a maintainer will review and merge.

## Skill Authoring Standards

- `name`: lowercase letters, digits, and hyphens; 64 characters or fewer. It must match the filename for flat skills. Do not use the reserved substrings `anthropic` or `claude`.
- `description`: a single line (no multi-line YAML scalars), 1024 characters or fewer, written in the third person, with explicit "Use when ..." triggers so agents auto-invoke correctly.
- Keep the body concise and imperative. Put the detail an agent needs in the body; keep the always-loaded description tight.
- No private hosts, internal paths, secrets, or tokens. These skills are public.

---

Thank you for contributing!
