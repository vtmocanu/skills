# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-07-05

### Added

- `agent-team`: new `web-ux` role in the role library — a web UX expert that validates web-interface work by driving it in a real browser via the `agent-browser` CLI (navigate, interact, a11y-tree snapshots, screenshots), reviews five lenses (flow integrity, accessibility, visual/token consistency, responsiveness, copy), and proposes scoped refactor improvements as `Enhancement` findings. Read-only, opus, triggered by web-UI repo signals (`web/`, `frontend/`, vite/next/tailwind configs, `*.tsx`/`*.vue`/`*.svelte`). SKILL.md init/tuning/dispatch sections updated to cover it (dispatch in the reviewer/auditor wave with a reachable URL for the running UI).
- `agent-team` (from 2026-07-05 session, previously untagged commit): gotchas for false `teammate_terminated` notices (only protocol `shutdown_approved` proves termination; concurrent-writer guard in respawn prompts), crossed nudge/report message protocol, and the single-worktree sequential writer-token pattern.

## [0.10.7] - 2026-06-16

### Added

- `agent-team`: Gotchas now cover a background teammate's `SendMessage({to: "main"})` report bouncing: the lead receives only the `idle_notification` (a summary preview), not the findings body. Do not act on the summary; SendMessage the (idle, resumable) teammate to re-send its full findings to `main`, and tell teammates in their spawn prompt to fall back to replying directly to the lead if `to: main` bounces. Observed repeatedly 2026-06-16 across multiple background review agents.
- `agent-team`: the cmux "Re-identifying surfaces" guidance now warns that the lead's OWN pane is titled by its current rendered content (e.g. "Teammate shutdown notifications"), not "Claude Code", and a bystander can itself be titled "Claude Code", so the lead is identified authoritatively by `cmux identify`'s `caller.surface_ref`, never by pane title; only the teammate panes are title-correlated. Observed 2026-06-16.

## [0.10.6] - 2026-06-16

### Added

- `agent-team`: SKILL.md "Parallel same-repo waves" now requires verifying a worker branch's ref equals the worker's last reported SHA before merging it (`git rev-parse <branch>`; `git worktree list` must show the branch at that SHA, not `(detached HEAD)`). A follow-up commit made on a detached HEAD leaves the branch ref behind, so `git merge <branch>` silently integrates the stale pre-fix code and drops the follow-up; tests still pass when the dropped delta is additive, so it is invisible without the check. Observed 2026-06-16: a coder's MEDIUM trace-read security fix vanished from a milestone merge because its hardening commit sat on a detached HEAD, caught only later by a docs fact-check against the code. Fix: merge the reported SHA directly, or confirm the ref first, then grep the integration tree for a signature from each follow-up.
- `agent-team`: Gotchas now note that current Claude Code (observed 2.1.178) exposes a single implicit team rather than the `TeamCreate` API — `team_name` is deprecated/ignored and `TeamCreate`/`TeamDelete` are absent; spawn via `Agent({name, subagent_type})` + coordinate via `SendMessage`/`Task*`. The `TeamCreate`-centric Mode 3 prose predates this; a full rewrite is a separate pending pass.

## [0.10.5] - 2026-06-16

### Fixed

- `agent-team`: `scripts/layout-team-panes.sh` now defends the documented "pass surfaces as separate args" footgun. A teammate arg containing whitespace means the caller joined the surfaces into one string ("surface:8 surface:9 ..."); the script now detects that (a surface ref never contains whitespace), re-splits on whitespace to recover the intended refs, and warns so the caller fixes the invocation. Observed 2026-06-16: a space-joined invocation made every per-surface cmux op fail with "Invalid surface handle" and the reshape bail to a confusing LAYOUT-MISS, while `equalize_splits` incidentally evened the geometry — so the layout looked fine but the script reported a miss. Separate args remains the contract; this is a safety net, not a license to join.

## [0.10.4] - 2026-06-16

### Fixed

- `agent-team`: `scripts/layout-team-panes.sh` `verify()` now gates on layout EVENNESS, not just shape, so the idempotent early-return no longer reports "LAYOUT-OK (already canonical)" on a shape-correct-but-skewed layout and skips equalization. It additionally asserts the left/right split is roughly even (lead width 30-62% of span, catching both a squeezed lead and a width-hogging lead) and the teammate strips are roughly equal height ((maxH-minH)/maxH < 0.35). Tolerances are generous so a near-even layout is not needlessly re-reshaped (a reshape itself spawns strays). Motivated 2026-06-16 by a fresh spawn wave that left the lead at ~23% width; the prior shape-only verify caught that particular case via the lead-width-vs-span boundary check, but a shape-correct right column with unequal strips (e.g. 80/20) would have falsely passed.

## [0.10.3] - 2026-06-15

### Added

- `agent-team`: SKILL.md Step 3.5 now documents how to re-identify teammate surfaces after a RECYCLE (a teammate shut down + respawned). The before/after `pane.list` diff is unreliable across a recycle because the terminated teammate frees a surface and cmux often respawns a stray shell into the emptied pane, so "new surfaces" no longer equals "the new teammate" (observed 2026-06-15 recycling the coder at a milestone boundary). The reliable disambiguator: correlate tmux pane titles (`tmux list-panes -a -F '#{pane_left},#{pane_top}  #{pane_id}  #{pane_title}'` — live agent panes are titled by teammate name) with the cmux `pane.list` `pixel_frame` (x,y) ordering to map each live agent to its surface, then pass lead + the live-teammate surfaces to the layout script.

## [0.10.2] - 2026-06-15

### Fixed

- `agent-team`: SKILL.md Step 3.5 no longer tells the team-lead to run a bare `cmux` to find the lead/teammate surfaces. Under the claude-teams launcher the cmux CLI is off PATH, so `cmux identify` / `cmux rpc pane.list` error out and make the lead wrongly conclude "not under cmux" and skip the layout entirely (observed 2026-06-15). The lead now resolves `$CMUX` via the app-bundle fallback (`/Applications/cmux.app/Contents/Resources/bin/cmux`, the same resolution the bundled `layout-team-panes.sh` already uses) and detects launcher presence from the `$TMUX` socket name (`*cmux-claude-teams*`), never from a `cmux identify` exit code. The script itself was already correct; this aligns the prose the lead executes with it.

## [0.10.1] - 2026-06-15

### Fixed

- `agent-team`: `scripts/layout-team-panes.sh` no longer reports a spurious LAYOUT-MISS when the cmux window has a global chrome/sidebar x-offset. `verify()` now measures pane geometry relative to the layout origin (`ox` = minimum pane x) and span (`cw - ox`) instead of assuming the lead pane starts at absolute x≈0. An offset layout (observed at 216px) was perfectly canonical yet exited 3; the relative check passes it. Backward-compatible (offset 0 reduces to the prior absolute checks) and does not weaken genuine-miss detection (those still fail on pane count / op errors).

## [0.10.0] - 2026-06-14

### Added

- `agent-team`: bundled `scripts/layout-team-panes.sh`, which normalizes the cmux pane layout after a spawn wave (team-lead on the left half, teammate panes as equal right-column strips, bystanders stacked in the left column). It is idempotent (a no-op when the layout is already canonical), self-verifying (pane count + lead-left + teammates-right geometry), cleans up the stray shells cmux respawns into emptied panes (via `close-surface`), polls the eventually-consistent pane tree to confirm each structural op landed before the next, and is a clean no-op outside the cmux launcher. On failure it exits 3 (LAYOUT-MISS) and saves a `pane.list` snapshot under `~/.claude/cmux-layout-misses/`.

### Changed

- `agent-team`: SKILL.md Step 3.5 now calls the bundled layout script instead of walking the manual move-surface/split-off recipe inline, and documents a required self-improving loop: a LAYOUT-MISS (or a discovered bug or a better cmux primitive) goes through `/dot-ai-reflect agent-team` to fold the fix into the script itself, using the captured snapshots as input.

## [0.9.0] - 2026-06-13

### Added

- `agent-team`: the documenter role now self-verifies after a large doc change (a migration or relocation) before reporting done — content fidelity (diff the pre-change source against the new corpus), link integrity, inbound-reference fixes (other docs, CLAUDE.md/CONTRIBUTING.md), and accuracy-vs-source — and points docs at any local-dev setup a reader needs so a relocated instruction never dead-ends.
- `agent-team`: SKILL.md Step 4 documents a fidelity-first review pass for documentation migrations (five lenses: fidelity, link integrity, accuracy, structure, newcomer-UX), with agent count scaled to the change size.

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
