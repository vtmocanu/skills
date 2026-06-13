# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.0] - 2026-06-13

### Changed

- `agent-team`: the documenter role now carries the terse-README + `docs/` house style (README as a launchpad, reference detail in a `docs/` folder), proposes a README-to-`docs/` migration when a repo diverges (gated on user confirmation, never silent), and maintains an `ARCHITECTURE.md` for repos with non-trivial architecture (skipping small/simple repos where the README conveys the shape).
- `agent-team`: run-mode keeps the `.claude/agent-team-tasks/` artifacts on the work branch (never the default branch), and the team-lead never deletes another session's team state.

## [0.7.1] - 2026-06-13

### Fixed

- `scripts/validate_skills.py` now skips `*.example.md` files (e.g. `CLAUDE.example.md`), which are docs, not skills. The v0.7.0 push failed the `test` workflow because the validator treated the frontmatter-less example as an invalid skill.

## [0.7.0] - 2026-06-13

### Added

- `CLAUDE.example.md` at the repo root: a generic starter for a global `~/.claude/CLAUDE.md`, extracted and genericized from a personal config. Contains only general AI-collaboration guidance (parallel tool calls, truth-over-agreement, confirm-before-changing, no-corner-cutting, best-practice-first, the Documentation Update Trigger and Conflicting Instructions patterns). No hosts, paths, repo names, or tool-specific setup. It is a plain doc, not a skill (no frontmatter, so `dot-ai skills generate` ignores it).

## [0.6.0] - 2026-06-13

### Added

- `agent-permissions`: bundled `config.example` — a sanitized starter Dippy config (generic safe-defaults: read-only allows, write/destructive `ask`/`deny`, secret-write `deny-redirect` guards, the never-allow-interpreters rule, and the auto-mode `[ASK]` convention). Contains no host/path/integration specifics; copy to `~/.dippy/config` and adapt. Referenced from the skill's Dippy Config Format section.

## [0.5.0] - 2026-06-13

### Added

- `agent-permissions` folder skill, migrated from a private skills repo and genericized: manage an AI coding agent's permissions via [Dippy](https://github.com/ldayton/Dippy) (Bash/MCP `allow`/`ask`/`deny` rules, file redirects) and `settings.json` (Read/WebFetch/Skill). Bundles `dippy-with-auto-fallback.sh`, the PreToolUse wrapper that implements the auto-mode `[ASK]` escalation convention (in `auto` permission mode only `ask` rules whose reason contains `[ASK]` prompt the human; everything else falls through to the agent's own classifier). Local paths in the body are examples — adapt them to your own setup; hook `command` strings must be absolute (Claude Code does not expand `~`).

## [0.4.0] - 2026-06-13

### Added

- cmux skill suite (8 folder skills), migrated from a private skills repo and genericized: `cmux` (topology/routing control), `cmux-browser` (browser automation, with `templates/`), `cmux-customization`, `cmux-diagnostics` (with a bundled `cmux-diagnostics` script), `cmux-keyboard-shortcuts`, `cmux-markdown`, `cmux-settings` (with a bundled `cmux-settings` script), and `cmux-workspace`. All track upstream `manaflow-ai/cmux` (`skills/cmux*`); re-sync from upstream rather than authoring from scratch.

## [0.3.2] - 2026-06-13

### Fixed

- `agent-team`: cmux pane-layout detection no longer silently skips. The `claude-teams` launcher puts only shim dirs on PATH (a `tmux`/`claude` shim), so `command -v cmux` returns false even while running under cmux, which skipped the entire Step 3.5 layout fix and left the team-lead squeezed in a full-width stack. Detect the launcher via its `$TMUX` socket and resolve the cmux CLI with an app-bundle fallback (`/Applications/cmux.app/Contents/Resources/bin/cmux`), then use the resolved `"$CMUX"` path through the verify and rebuild steps.

## [0.3.1] - 2026-06-12

### Changed

- `agent-team`: `spec-keeper` keeps `specs/human.md` terse (short, skimmable bullets, one line per requirement) so humans can read and confirm it at a glance; detail and rationale go to `specs/ai.md`.

## [0.3.0] - 2026-06-12

### Added

- `agent-team`: new `fact-checker` role for adversarial claim verification. Opt-in and read-only: extracts checkable claims from docs, reports, diffs, and teammate outputs, verifies each against the most authoritative source (code over prose, command output for behavior claims, primary sources for external facts), and reports per-claim verdicts (VERIFIED / REFUTED / UNVERIFIABLE) with evidence. Run-mode dispatches it in the reviewer/auditor wave; refuted claims are blocking.

## [0.2.0] - 2026-06-11

### Added

- `agent-team`: new `spec-keeper` role for rebuild-from-specs tracking. Maintains `specs/human.md` (user-stated requirements, the binding contract; edits gated on user confirmation via the lead) and `specs/ai.md` (AI design decisions, auto-applied). The lead passes a user-vs-AI provenance breakdown on dispatch; spec sync runs after review and audit.

### Changed

- `agent-team`: run-mode hardening from live sessions: pin reviews to commit SHAs, require commit-and-report-SHA on post-done dispatches, forward standby pre-flags to the coder mid-implementation, releaser default-branch drift reconciliation, worktree cleanup and lint-cache gotchas, stall-nudge guidance, and task-list-loss resilience.

## [0.1.0] - 2026-06-10

### Added

- `agent-team` skill (folder skill: `SKILL.md` plus `roles.yaml`): auto-generate and run a per-repo Claude Code agent team, migrated from a private skills repo and genericized.

### Changed

- `reflect`: use a skill's `source:` frontmatter to locate its repository before editing.
- `reflect`: detect trigger misfires (fix the `description`, not just the body), add a "do not capture" filter for one-off/context-specific signals, and add a consolidation pass to curb skill bloat.
- README: skills may be folders (`<name>/SKILL.md` plus supporting files), supported since dot-ai v1.21.0.

## [0.0.1] - 2026-06-07

### Added

- Initial public release.
- `reflect` skill: analyze a session and propose, then apply, improvements to the skill that was used.
- Skill frontmatter validator (`scripts/validate_skills.py`), run in CI on every push and pull request.

[0.3.1]: https://github.com/vtmocanu/skills/releases/tag/v0.3.1
[0.3.0]: https://github.com/vtmocanu/skills/releases/tag/v0.3.0
[0.2.0]: https://github.com/vtmocanu/skills/releases/tag/v0.2.0
[0.1.0]: https://github.com/vtmocanu/skills/releases/tag/v0.1.0
[0.0.1]: https://github.com/vtmocanu/skills/releases/tag/v0.0.1
