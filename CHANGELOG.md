# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- `reflect`: use a skill's `source:` frontmatter to locate its repository before editing.
- `reflect`: detect trigger misfires (fix the `description`, not just the body), add a "do not capture" filter for one-off/context-specific signals, and add a consolidation pass to curb skill bloat.

## [0.0.1] - 2026-06-07

### Added

- Initial public release.
- `reflect` skill: analyze a session and propose, then apply, improvements to the skill that was used.
- Skill frontmatter validator (`scripts/validate_skills.py`), run in CI on every push and pull request.

[0.0.1]: https://github.com/vtmocanu/skills/releases/tag/v0.0.1
