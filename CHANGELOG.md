# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.2.0]: https://github.com/vtmocanu/skills/releases/tag/v0.2.0
[0.1.0]: https://github.com/vtmocanu/skills/releases/tag/v0.1.0
[0.0.1]: https://github.com/vtmocanu/skills/releases/tag/v0.0.1
