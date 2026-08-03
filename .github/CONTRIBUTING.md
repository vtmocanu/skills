# Contributing to skills

Thanks for your interest in contributing! Issues and pull requests are welcome.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Adding or Editing a Skill](#adding-or-editing-a-skill)
- [Product Requirements Documents](#product-requirements-documents)
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

Each skill is a single Markdown file at the **repository root** (dot-ai's `?repo=` override reads prompts from the root):

- **Flat skill**: `<name>.md`
- **Folder skill** (with supporting files): `<name>/SKILL.md`

Every skill starts with YAML frontmatter:

```yaml
---
name: <name>
description: <one line: what it does and when to use it>
---
```

1. Fork the repository and create a branch: `git checkout -b add-<skill>`.
2. Add or edit the skill file at the repository root.
3. Run the validator (below).
4. Open a pull request.

## Product Requirements Documents

Changes large enough to need planning are written up first as a PRD. PRDs live in `prds/` at the repository root, one Markdown file per document, named `<issue-number>-<slug>.md` (for example `prds/13-agent-team-fanout.md`).

- The issue number matches an open GitHub issue labelled `PRD`, and the document links back to that issue on its first line.
- A PRD carries: the problem stated with evidence (cite files and line numbers, quote the text you are calling wrong), the solution, a decision log, milestones as a checklist, measurable success criteria, and risks.
- Milestones are checked off as they land. A milestone the pull request cannot complete stays unchecked, with the reason written next to it. Do not check a box to tidy the list.
- PRDs are working documents. When review disproves something the draft claimed, correct it in place and record the correction rather than deleting the claim; the wrong turn is often the most useful part for the next reader.

`prds/` is also a spec directory in the `agent-team` sense: it matches the `architect` role's `triggers_on` patterns, so a team generated for this repository includes an architect that would not have been selected before.

## Validating

```bash
python3 scripts/validate_skills.py .
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
