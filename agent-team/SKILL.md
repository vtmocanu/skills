---
name: agent-team
description: Auto-generate and run a per-repo Claude Code agent team. Probes the current repo (build/CI/env manifests, agent launchers, slash commands, spec dirs) and writes `.claude/agents/{role}.md` subagent definitions for the relevant roles from a library (coder, reviewer, auditor, tester, documenter, release, researcher, spec-keeper, fact-checker) plus a `.claude/agent-team.md` workflow doc. Use when (1) `/agent-team init` to create the team for the current repo, (2) `/agent-team update` to refresh after project shape changes, (3) `/agent-team {task}` to run a task with the team (TeamCreate plus spawn plus drive orchestrator flow plus stop at user gates). Triggers include "/agent-team", "spin up a team", "auto-create agents", "agent team for this repo", "team-on-task".
---

## Document Location

This document lives in [github.com/vtmocanu/skills](https://github.com/vtmocanu/skills) at `agent-team/SKILL.md`.

> **Note**: This is the source of truth. Generated copies in your agent's skills directory (e.g. Claude Code's `~/.claude/commands/`) are derived from this file via `dot-ai skills generate --repo https://github.com/vtmocanu/skills`. Edit here and regenerate; never edit generated copies.

## What this skill does

Builds and operates a Claude Code [agent team](https://code.claude.com/docs/en/agent-teams) tailored to the current repo. Inspired by Viktor Farcic's [`dot-agent-deck`](https://github.com/vfarcic/dot-agent-deck) (a TUI that both displays and defines multi-agent teams across Claude Code and OpenCode); this skill is the native-APIs alternative: it probes the current repo for signals, picks roles from a library, and writes Claude Code native `.claude/agents/*.md` subagent definitions plus a `.claude/agent-team.md` workflow doc, so teammates are spawned by name via the Agent tool's `subagent_type` parameter.

The skill has three modes selected by the first argument:

| Mode | Trigger | What it does |
|------|---------|--------------|
| **init** | `/agent-team init`, or no args + no `.claude/agents/` present | Probe the repo, pick roles, write `.claude/agents/<role>.md` + `.claude/agent-team.md` |
| **update** | `/agent-team update` | Re-probe, diff against existing `.claude/agents/`, apply targeted changes |
| **run** | `/agent-team <task description>` | Read team manifest, TeamCreate, spawn teammates, drive the workflow, STOP at user gates |

`.claude/agents/` is a Claude Code project-scoped subagent directory. `.claude/agent-team.md` is a workflow manifest this skill writes for its own use; not loaded automatically by Claude Code but read by the skill on `run`.

## Autonomy: spin up teams as you see fit

When this skill is loaded, the team-lead has standing authority to spawn agents and create teams without asking the user to authorize each one. Decide which roles to spawn, how many, whether to run them in the foreground or background, and when to recycle or shut them down, based on the task shape, not on per-call user confirmation. The user does NOT have to say "create coder + reviewer" or "spin up the team for this"; if the work fits the team's shape, just do it.

Scope of standing authority:

- **Spawning teammates** from the existing `.claude/agents/` roster for the active task.
- **TeamCreate / TeamDelete** at task boundaries.
- **Background vs foreground** mode per Agent call.
- **Recycling** a teammate at a clean task boundary (Mode 3 Step 5 + the "Context recycling" section below).
- **Dispatching slash commands** the orchestrator may invoke between delegations (per the workflow manifest).

Still requires explicit user confirmation (do NOT auto-act):

- **Shared-system writes** the team would perform on the user's behalf: release tag pushes, force-pushes, PR merges, sending external messages. Present a verification summary and STOP, per Mode 3 Step 4.
- **PRD-task transitions** to a NEW scope (a "pick the next task" command or request). Analyze and propose; spawn only after acceptance.
- **Role-file hotfixes** (Mode 3 Step 6.A) unless pre-authorized in durable instructions.
- **Editing the role library** at `./roles.yaml` or running `/agent-team update`.

If `.claude/agents/` is missing for the current repo, propose `/agent-team init` before spawning; do not auto-init without surfacing the proposed roster.

## Required pre-checks

Before any mode, confirm:

1. `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1` is set (per the [docs](https://code.claude.com/docs/en/agent-teams), agent teams are experimental and disabled by default). Check `settings.json` or env. If missing, tell the user how to enable it and stop.
2. The current working directory is a git repo root (look for `.git`). If not, ask the user to confirm the target directory.
3. Claude Code `v2.1.32` or later: `claude --version`.

## Mode 1: init

Use when `.claude/agents/` does not exist, or `/agent-team init` is invoked explicitly.

### Step 1: discover the repo

Probe for these signals. Use the Glob and Read tools; do NOT run `find /`. All probing scoped to the current repo.

- **Build/package manifests**: `package.json`, `go.mod`, `pyproject.toml`, `Cargo.toml`, `pom.xml`, `Gemfile`, `build.gradle`, `Makefile`, `kcl.mod`, `Chart.yaml`.
- **Task runners**: `Taskfile.yml`, `justfile`, `Makefile`, `package.json#scripts`.
- **Reproducible-env manifests**: `devbox.json`, `flake.nix`, `shell.nix`, `.nvmrc`, `pyproject.toml` (poetry), `environment.yml`, `.tool-versions`, `.envrc`. The first hit drives `init_command` references in the team workflow doc.
- **Agent launchers**: scripts/aliases that launch a Claude/opencode/etc. session. Look in `scripts/`, `Taskfile.yml`, `Makefile`, `package.json#scripts`, devbox script blocks. Record the FULL invocation form (`devbox run agent-big`, `task agent`, `make agent`), not the bare script name.
- **Project slash commands** the orchestrator can invoke between delegations: `.claude/commands/`, `.claude/skills/`. List them; the lead will reference them in the workflow doc.
- **Spec directories**: `prds/`, `specs/`, `rfcs/`, `proposals/`, `docs/adr/`, `docs/design/`.
- **CI configs**: `.github/workflows/`, `.forgejo/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`.
- **Release signals**: `docs/releasing.md`, `RELEASING.md`, `.goreleaser.*`, `CHANGELOG.md`, `semantic-release.json`, `.releaserc*`, a release workflow in CI.
- **CLAUDE.md and CONTRIBUTING.md**: read both (top-level and any nested). They contain authoring rules workers must follow.

Cite the actual files/directories you found in the proposal you present to the user. Do not pad with generic boilerplate.

### Step 2: pick roles

Load the role library at `./roles.yaml` (relative to this SKILL.md). For each role:

- **coder, reviewer, auditor**: always include. These are the default trio.
- **tester**: include if any of the role's `triggers_on` patterns match a path in the repo (`tests/`, `test/`, `*_test.go`, `pytest.ini`, etc.).
- **documenter**: include if a non-trivial `docs/` dir exists OR README is large (>500 lines) OR a docs site config is present (`mkdocs.yml`, `book.toml`, `docusaurus.config.*`).
- **release**: include if any release signal from §discover step matches.
- **researcher**: opt-in only; include if the user requests it or if the repo has a substantial spec directory (≥3 documents) suggesting research-heavy work.
- **spec-keeper**: include if a `specs/` directory exists OR the user asks for spec tracking. Maintains `specs/human.md` (user-stated requirements; edits gated on user confirmation) and `specs/ai.md` (AI design decisions; auto-applied), aiming for rebuild-from-specs sufficiency.
- **fact-checker**: opt-in only; include if the user requests it or the work is claim-heavy (docs sites, READMEs/CHANGELOGs with version/URL/API facts, reports whose statements must hold). Adversarially verifies claims against code, command output, and primary sources; read-only.

If a borderline call needs the user, ask via AskUserQuestion before writing files.

### Step 3: tune prompt bodies

For each picked role, take the `prompt_body` from `roles.yaml` and append project-specific details discovered in step 1:

- **coder**: name the actual test/lint command (e.g. `task test`, `cargo test`, `pytest`, `devbox run -- task kcl:test`). Reference CONTRIBUTING.md / CLAUDE.md by path. Reference the spec directory if found.
- **reviewer**: cite the authoring-rules file (CONTRIBUTING.md / CLAUDE.md) by exact path and quote one or two of its load-bearing rules if obvious.
- **auditor**: note if the repo is public (security implications differ); name the secret scanner if found in CI (`gitleaks`, `trufflehog`).
- **tester**: name the test framework discovered (jest, pytest, go test, cargo test).
- **documenter**: name the doc site generator (mkdocs, hugo, docusaurus) or "plain markdown" if none. The documenter also owns the repo's `CHANGELOG.md` and keeps it TERSE: one concise line per change under an `[Unreleased]` section (Keep a Changelog style), not paragraphs. Every feature/fix/behaviour change the team ships gets a one-line entry there; the releaser later folds `[Unreleased]` into the cut version (see Mode 3 Step 3). If the repo has no CHANGELOG yet, the documenter creates one. The documenter also carries the README/docs house style (terse README as a launchpad, reference detail in a `docs/` folder); when the repo's README diverges (a large monolithic README, or no `docs/` — the same signal as the >500-line-README include trigger in Step 2), it PROPOSES a migration and asks the user before restructuring, never doing it silently. For repos with non-trivial architecture (multiple components/services, cross-cutting data flows, trust boundaries) it also keeps an `ARCHITECTURE.md` at the root, creating one when it helps a new reader and skipping it for small/simple repos where the README conveys the shape.
- **release**: name the release flow doc by path (`docs/releasing.md`, etc.) and the release command (`semantic-release`, `goreleaser`, manual tag-push).
- **researcher**: name the spec directory.
- **spec-keeper**: name the spec directory path if it differs from `specs/`; note any existing spec files to adopt instead of creating fresh ones.
- **fact-checker**: name the claim-bearing surfaces in this repo (docs dir, README, CHANGELOG, published specs) and any authoritative sources to check against (the code itself, CI status, official upstream docs).

Keep additions tight: 1-3 extra sentences per role. The role library text already covers the generic shape.

### Step 4: present a proposal

Show the user a numbered proposal:

```
Proposed team for <repo-name>:

1. coder (sonnet) - implements features, fixes bugs. Will run `<test-command>`
   before reporting done. Will follow rules in <CONTRIBUTING.md path>.
2. reviewer (sonnet) - reviews against <CONTRIBUTING.md rules>. Read-only.
3. auditor (sonnet) - security audit. Notes: <public/private repo>, secret
   scanner: <name>. Read-only.
4. tester (sonnet) - <test framework discovered>. (included because <reason>)
   [omit if not picked]
5. release (sonnet) - runs <release-doc path>. (included because <release signal>)
   [omit if not picked]
... etc

Files I would write:
  .claude/agents/coder.md
  .claude/agents/reviewer.md
  .claude/agents/auditor.md
  ... (one per role)
  .claude/agent-team.md (workflow manifest)

Tell me what to drop or change, otherwise I'll write the whole thing.
```

Wait for user confirmation. Use AskUserQuestion only if a specific binary decision needs resolving (e.g., "include researcher?"); otherwise present numbered text and let the user reply free-form.

### Step 5: write the files

For each picked role, write `.claude/agents/<name>.md`:

```markdown
---
name: <role-name>
description: <role's description from roles.yaml, lightly tuned if needed>
tools: <comma-separated allowlist from roles.yaml, or omit field entirely if empty>
model: <model from roles.yaml, or omit>
---

<tuned prompt_body from step 3>
```

**Tool allowlist format**: Claude Code subagent frontmatter accepts `tools:` as a comma-separated list. If `roles.yaml` says `tools: []` (empty array, meaning inherit all), OMIT the `tools` line entirely from the frontmatter. Do not write `tools: []`.

**Team-coordination tools (REQUIRED on any non-empty allowlist)**: every role with a `tools:` line MUST include `SendMessage, TaskUpdate, TaskList, TaskGet`. Without these, the spawned agent renders reports in its pane but cannot transmit them to team-lead, claim/complete tasks, or respond to `shutdown_request`. The role library at `roles.yaml` already includes them on every non-empty allowlist; verify they survived any prompt-body or schema-tuning edits you make in Step 3.

**Frontmatter MUST be single-line for `description`** (multi-line YAML block scalars break Claude Code's parser).

**Model MUST be auto-mode-capable (never `haiku`)**: auto-mode (the bias-to-act permission mode that lets teammates complete shared-system writes like a release push without a manual confirmation round-trip) is gated by the Claude Code docs to **Sonnet 4.6, Opus 4.6, or Opus 4.7 only**. Haiku and Sonnet 4.5 are explicitly unsupported, so a teammate on those models cannot participate in auto-mode and falls back to ask-for-everything, stalling autonomous flow. Use `model: sonnet` (the alias resolves to Sonnet 4.6) as the floor; bump the reasoning-heavy roles (coder, reviewer, auditor, tester, researcher, spec-keeper, **and fact-checker**) to `opus`. The tester is opus because testing in most repos here is adversarial/scenario validation (crafting fixtures to break a guard, reasoning about evasion vectors, probing runtime contracts), which is reasoning-heavy; keep tester on `sonnet` only when the repo's testing is a purely mechanical unit-test-suite run (jest/pytest/go test where the work is execution, not reasoning). documenter and release stay `sonnet`. Do NOT pin `haiku` for any role, including release. Note (per the docs): a subagent's frontmatter `permissionMode` is ignored; its actions are classified under the parent session's rules, but only if the subagent's model is itself auto-mode-capable, which is why the model choice (not a mode flag) is the lever here.

Write `.claude/agent-team.md` as the workflow manifest:

```markdown
# Agent team workflow for <repo-name>

Generated <date> by the `agent-team` skill.

## Team roster

| Role | Subagent type | Model | Tools |
|------|---------------|-------|-------|
| coder | coder | sonnet | (inherit) |
| reviewer | reviewer | sonnet | Bash, Read, Grep, Glob, WebFetch |
| ... | | | |

## Orchestrator workflow

You (the team lead) NEVER do implementation, review, or audit work yourself.
You coordinate the team via TeamCreate + Agent (with team_name + name +
subagent_type) + SendMessage + TaskUpdate.

Default flow for a typical task:
1. Spawn coder with the full task context. The coder runs `<test-command>`
   before reporting done.
2. After coder reports done, spawn reviewer + auditor IN PARALLEL with
   coder's diff + report.
3. Resolve any blocking findings (route them back to coder via SendMessage).
4. <if release role exists> Before delegating to release, summarize what to
   verify end-to-end and STOP for user confirmation.
5. <if release role exists> On user OK, spawn release.

## Context handoff (CRITICAL)

Every teammate cold-starts with no memory of prior conversation or other
teammates' outputs. Whatever you write in the spawn `prompt:` is the entire
context they have, plus the body of `.claude/agents/<role>.md`.

Therefore every spawn prompt MUST include:
- File paths the teammate should read (the spec, the files being modified,
  CLAUDE.md/CONTRIBUTING.md when authoring rules matter)
- A summary of any prior teammate's findings when chaining workers
- The exact error message when retrying after a failure
- If context is long, write it to `.claude/agent-team-tasks/<slug>.md` and
  reference that path in the prompt instead of pasting inline

## Project signals

- Test command: <discovered>
- Lint command: <discovered>
- Release flow: <discovered path>
- Spec dir: <discovered path>
- Authoring rules: <CLAUDE.md / CONTRIBUTING.md paths>
- CI: <forgejo / github / gitlab / etc.>
- Slash commands the orchestrator may invoke between delegations:
  <list of /commands the lead can run itself>
```

Cite specific values, not "discovered".

After writing, suggest the user commit `.claude/agents/` and `.claude/agent-team.md` to the repo so the team definition is reproducible.

## Mode 2: update

Use when `/agent-team update` is invoked, or when an existing team feels out of date.

1. Read existing `.claude/agents/*.md` files and `.claude/agent-team.md`.
2. Re-run discovery (step 1 of init).
3. Diff: roles in library that match repo signals but missing from `.claude/agents/`, OR roles present but whose `triggers_on` no longer match (project no longer has tests, release flow removed, etc.).
4. Present the diff to the user as a numbered proposal (additions / removals / prompt-body updates).
5. On confirmation, apply targeted edits. Preserve any manual customizations the user made (e.g. extra paragraphs in a `prompt_body`) by merging rather than overwriting: read existing content, identify which parts came from the library and which are manual, only touch the library parts.

## Mode 3: run

Use when `/agent-team <task description>` is invoked with a non-keyword first argument.

### Step 1: precheck

- Confirm `.claude/agents/` exists. If not, suggest running `/agent-team init` first.
- Confirm `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
- Read `.claude/agent-team.md` for the workflow + project signals.
- **Put the agent-team artifacts on the work branch, NEVER the default branch.** Decide the feature branch (and, in a bare-clone-with-worktrees repo, which worktree) BEFORE writing any `.claude/agent-team-tasks/` brief or spawning a teammate, then operate from there:
  - **Single shared worktree:** if the session opened on the default branch (`main`/`master`), `git checkout -b <feature>` in THIS worktree first, so the briefs and every commit land on `<feature>`.
  - **Dedicated work worktree (bare-clone layout / parallel waves):** create the feature worktree (per the `git-worktrees` skill) and write the `.claude/agent-team-tasks/` briefs INTO that worktree — do not write them in a default-branch worktree the agents will not be in.

  Always name the exact branch in every spawn prompt, and ensure the lead itself is on/in the work branch before it authors briefs. Stranding these artifacts on the default branch breaks context handoff (the brief is invisible from the work branch/worktree) and pollutes the default branch. Observed 2026-06-13: a session started on `main` while its agents worked on another branch left the `.claude/agent-team-tasks/` ("agents dir") stranded on `main`, invisible from the work branch.

### Step 2: TeamCreate + plan tasks

Call `TeamCreate({team_name: "<repo-name>-<short-slug>", agent_type: "orchestrator", description: "<task summary>"})`.

Create team-level tasks via TaskCreate:
- Task #1: implementation (owner: coder)
- Task #2: review (owner: reviewer, blockedBy: #1)
- Task #3: audit (owner: auditor, blockedBy: #1)
- Task: spec sync (owner: spec-keeper, blockedBy: #2 + #3) - only if spec-keeper role exists
- Task #4: release (owner: release, blockedBy: #2 + #3, user-gated) - only if release role exists

### Step 3: spawn teammates

Spawn each teammate in parallel via Agent calls in a single message:

```
Agent({
  team_name: "<team>",
  name: "coder",
  subagent_type: "coder",
  prompt: "<task-specific cold-start prompt>"
})
```

For `coder`, the prompt includes the full task description plus paths to read.

For `reviewer` and `auditor` (which depend on coder's output), the prompt is "stand by, prime your context by reading <PRD/CLAUDE.md/CONTRIBUTING.md>, then go idle. Wait for the team lead to send the diff/files to review/audit."

For `release`, do NOT spawn at start; spawn only after user confirms post-audit. When you DO spawn it, the spawn prompt MUST instruct the releaser to include the changelog in the release: before tagging, fold the pending `CHANGELOG.md` entries into the version being cut (rename an `[Unreleased]` section to the version with its date, or otherwise assemble the version's notes); after the release publishes, confirm the release page / GitHub Release body carries that changelog section, not just an empty or auto-generated-commits-only body. A release that ships without its changelog is incomplete.

For `spec-keeper`, do NOT spawn at start; spawn after blocking review/audit findings are resolved. Its spawn prompt MUST carry a provenance breakdown of the change: which requirements and decisions came from the user (verbatim where possible) and which were AI choices made along the way. The team lead is the only one who has seen the full conversation, so assembling this breakdown is lead work; without it the spec-keeper cannot file decisions into `specs/human.md` vs `specs/ai.md` correctly.

**Parallel same-repo waves (multiple implementers editing one repo at once):**

- Do NOT rely on the Agent tool's `isolation: "worktree"` parameter for teammates: combined with `team_name` it has been observed to silently not isolate (one teammate switched the lead worktree's branch mid-run, 2026-06-10). Instead, put explicit worktree setup in each spawn prompt: `git worktree add <repo-parent>/<branch> <branch>`, then "cd there and do ALL work in that worktree; NEVER touch the lead's worktree or switch its branch".
- When N workers' branches will merge into one integration branch, instruct every worker NOT to bump VERSION/CHANGELOG (or any other shared file all milestones would touch); the lead does one consolidated bump after merging. Otherwise the merge hits N-way conflicts on those files.
- If a wave is aborted before commits land, leftover empty branches are fine: tell the respawned worker to reuse the existing branch instead of deleting and recreating it (branch deletion may be denied by the permission classifier as destructive).

### Step 3.5: cmux pane layout (keep the team-lead un-squeezed)

When the session runs inside **cmux** (the `cmux claude-teams` launcher installs a tmux shim that turns each teammate spawn into a `cmux new-split`, so every teammate becomes its own pane), spawn splits land wherever the shim puts them, not where the user wants them.

**Target layout (user-validated):** the team-lead pane on the LEFT half; ALL teammate panes equal HORIZONTAL strips on the RIGHT half. Non-agent bystander panes (extra shells, old sessions) stacked in the LEFT column above/below the lead are FINE — leave them there; do NOT consolidate them into lead-pane tabs (an unwanted disruption). NOT ok: a bystander in the right teammate stack, a teammate in the left column, or the lead squeezed into a full-width stack.

After EVERY spawn wave (the initial Step 3 spawns, the post-coder reviewer/auditor/tester wave, and any respawn in Step 6 or the recycle flow), run the bundled helper:

```bash
# Detect cmux by the $TMUX socket name, NOT by running `cmux` — the claude-teams shim keeps the
# cmux CLI OFF PATH, so a bare `cmux identify` errors and makes you wrongly conclude "not under cmux".
case "${TMUX:-}" in *cmux-claude-teams*) ;; *) echo "not under cmux claude-teams — skip layout"; esac
# Resolve the CLI the same way the script does (PATH, else the app-bundle path):
CMUX=$(command -v cmux || echo /Applications/cmux.app/Contents/Resources/bin/cmux)
LEAD=$("$CMUX" identify --json | jq -r .caller.surface_ref)
./scripts/layout-team-panes.sh "$LEAD" surface:NN surface:MM ...   # one arg per teammate
```

- **cmux presence:** decide it from `$TMUX` containing `cmux-claude-teams` (above), never from whether `cmux identify` exits 0 — off PATH it fails even when you ARE under cmux. The correct app-bundle path is `/Applications/cmux.app/Contents/Resources/bin/cmux` (note: `Resources/bin`, NOT `Resources/app/bin`).
- **Lead surface:** `"$CMUX" identify --json | jq -r .caller.surface_ref` (resolve `$CMUX` first, above).
- **Teammate surfaces:** the surfaces that APPEARED with this wave. Snapshot `"$CMUX" rpc pane.list` `selected_surface_ref`s BEFORE spawning, diff after; the new ones are the teammates. Pass them as SEPARATE args — NEVER a single space-joined string (that collapses them into one bogus surface ref and the reshape silently fails).
- Any surface that is neither the lead nor a listed teammate is treated as a bystander and stacked in the left column.

The script is idempotent (a no-op when the layout is already canonical — re-running after a wave that didn't skew costs nothing, and reshaping a good layout is itself what spawns stray shells, so it verifies first), self-verifies the result (exact pane count + lead-left + teammates-right geometry), cleans up the stray shells cmux respawns into emptied panes (via `close-surface`), and is a clean no-op when not under the cmux launcher. It resolves the cmux CLI itself (the claude-teams shim keeps cmux off PATH — it falls back to the app-bundle path, NOT `command -v cmux`).

Exit codes: `0` laid out OK / already canonical / no-op outside cmux; `2` usage; `3` **LAYOUT-MISS** — it could not reach the canonical shape and saved a `pane.list` snapshot under `~/.claude/cmux-layout-misses/`. On a MISS, fall back to inspecting `"$CMUX" rpc pane.list` pixel frames by hand (resolve `$CMUX` as above; the script source documents the manual recipe), and feed the snapshot into the reflect loop below.

**Self-improving (REQUIRED).** When the script reports LAYOUT-MISS, OR you hit a bug in it, OR you discover a better cmux primitive/approach, run `/dot-ai-reflect agent-team` so the fix is folded into the SCRIPT itself (`scripts/layout-team-panes.sh`), not just into this prose. The captured `~/.claude/cmux-layout-misses/*.json` snapshots are the concrete edge-case input for that pass. This is how the layout helper accretes handled cmux tree shapes over time, instead of the lead re-deriving the reshape by hand each session.

Notes:
- `cmux rpc workspace.equalize_splits` only evens SIZES at each split level; it canNOT fix a wrong tree SHAPE — that is why the script rebuilds the shape (collapse to tabs → `split-off right` to anchor the right half → stack down) rather than just equalizing. `cmux tree` does NOT show split orientation; verify with `pane.list` pixel frames.
- The cmux pane tree is eventually-consistent: a structural op is not reflected in the next `pane.list` immediately, so the script polls to confirm each op landed before the next (fire-and-continue races and derails the reshape). This is also why doing it by hand is error-prone — prefer the script.

### Step 4: drive the flow

- Wait for coder's completion message via the automatic mailbox notifications (do NOT poll).
- On coder done: SendMessage to reviewer and auditor with the diff summary, file paths changed, and the coder's report. They claim their tasks and report findings.
- **Pin review scope to explicit commit SHAs** in the review dispatch, and if the worker adds commits after the review was dispatched (a follow-up fix, a crossed-in-flight reconciliation), immediately SendMessage the reviewer the new tip and require explicit confirmation that ALL commits were covered. Observed failure mode: a reviewer's report cited only the first of two commits on the branch; the second commit was verified only after a direct "did you cover SHA X?" follow-up.
- **No amends after review dispatch — put this rule in the coder's INITIAL spawn prompt.** Workers may amend freely BEFORE a SHA is dispatched for review; once dispatched, fixes must land as follow-up commits, never amends. An amend orphans the pinned SHA (it stops being an ancestor of HEAD), invalidates in-flight review scope, and forces a re-confirmation round-trip with every validator. Observed twice in one run (2026-06-12): both amends crossed the reviewer's/auditor's reports in flight, and each validator had to re-diff and re-confirm coverage of the new tip; after the rule was sent mid-run, all later fixes stacked cleanly. Stating it at spawn time costs one sentence; correcting it mid-run costs a round per validator per amend.
- On reviewer + auditor both done: synthesize findings for the user. If any blocking finding, route back to coder via SendMessage.
- **Trust but verify teammate-reported gate results against artifacts** when the result matters beyond the report: a teammate's "task build green" can be stale-cache luck or a partial run. Cheap checks: binary timestamp/version after a build claim, `git log` tip after a commit claim. (Observed: a release agent reported the build gate green while the installed binary was left months stale.)
- A teammate going idle WITHOUT reporting, right after being asked for a small fix, may be stalled mid-fix rather than working: check `git -C <their-worktree> status` — idle + uncommitted edits = stalled; send a targeted "finish the loop: run the gate, commit, report the SHA" nudge. Prevent it at dispatch time too: any message that sends NEW requirements to a worker that already reported done MUST explicitly end with "run the gate, commit, and report the new tip SHA" (observed 2026-06-10: a coder applied forwarded security hardening but went idle with it uncommitted — the "release-ready" branch tip lacked the fix, and only the auditor's `git status` check caught it).
- **Standby reviewers/auditors often surface baseline pre-flags while priming** (they read the target code before the coder finishes). Forward actionable pre-flags to the coder MID-implementation instead of holding them for the review round — requirements are cheaper upstream than as blocking findings (observed: an auditor's pre-flags became the spec for a hardening commit, avoiding a full fix-and-re-review cycle).
- **If the tester role exists and the change alters behavior, dispatch it by default** on the scenario surface (the real runtime path: live conversation, real container/deploy entrypoint, the spec's stated success criteria). Coder-authored unit/security suites are NOT a substitute for end-to-end scenario proof — they test components, not the user-visible criterion. Observed (2026-06-12): a PRD's headline success criterion ("answers question X end-to-end via the real docker path") sat unproven through four reviewed milestones because the test matrix looked comprehensive; the user caught the missing tester, whose E2E run then produced the only direct evidence of the criterion (plus an adversarial injection probe and live doc-example verification the suites couldn't provide).
- If the fact-checker role exists: dispatch it in the same wave as reviewer/auditor, scoped to the change's claim-bearing artifacts (docs, README, CHANGELOG, report prose), with explicit pointers to which claims matter. Treat REFUTED claims as blocking findings; UNVERIFIABLE ones go to the user with what would be needed to verify.
- **Documentation migrations get a fidelity-first review pass.** When the documenter does a large doc change (a README→`docs/` migration, a relocation), scope the review to five lenses: content **fidelity** (diff the pre-change source against the new corpus — nothing dropped or altered), **link integrity** (relative links + anchors + images resolve), **accuracy** vs the source (env vars, script names, paths), **structure**/house-style (terse README, one concern per `docs/` file, no duplication/contradiction), and **newcomer-UX** (can a new reader get running and find things following only the docs). Dispatch reviewer + fact-checker scoped accordingly; the documenter should already have self-checked fidelity/links/inbound-refs before handing off (per its role). Scale the agent count to the change size — a few lenses for a small edit, one agent per lens for a full migration.
- If the spec-keeper role exists: once blocking findings are resolved, dispatch it with the change summary plus the user-vs-AI provenance breakdown (see Step 3). It applies `specs/ai.md` updates directly and sends proposed `specs/human.md` edits back; relay those to the user for confirmation before telling the spec-keeper to apply them. The task is not complete until specs are in sync — the bar is that the code could be rebuilt from `specs/` alone, with `specs/human.md` treated as the binding contract.
- Before release (if applicable): present an end-to-end verification summary and STOP. Ask the user to confirm before spawning the release teammate.
- When you spawn release, put the changelog requirement in its prompt (see Step 3): the releaser must ensure the version's `CHANGELOG.md` entries are folded into the cut and that the published release page carries them.
- **Expect default-branch drift before release**: long team sessions race bot pushes (Renovate dep PRs auto-merging to main, CI config bumps). The releaser must fetch and check divergence before tagging; reconcile with a plain `git merge origin/<default>` (NEVER force-push — destroys the bot commits; avoid rebasing already-reviewed merge commits — rewrites the audited SHAs), re-run the build gate on the merged tip, then tag that tip.

### Step 5: cleanup

When the task is complete and the user is done, do NOT send `shutdown_request` blindly. Verify each teammate is at a clean stop boundary first.

#### Pre-shutdown checks (mandatory before SendMessage shutdown_request)

1. **Task-ownership check**: Run TaskList. If the teammate owns any task in `in_progress` status, they may be mid-work. Investigate before shutting down.

2. **Pane-activity check**: Capture the teammate's tmux pane and look for the running indicator:

```bash
PANE_ID=$(jq -r '.members[] | select(.name=="<teammate>") | .tmuxPaneId' ~/.claude/teams/<team>/config.json)
tmux capture-pane -p -t "$PANE_ID" 2>/dev/null | tail -30
```

Interpret the output:
- **`✽ Nucleating…` or `✻ Cooked for Ns`**: actively processing. Likely reading your last inbox message or executing tool calls. Look at the scrollback above to identify what they're doing.
- **Idle `❯` prompt with no spinner**: ready for shutdown.
- **Mid-shell-command output without a returned prompt**: a Bash invocation is in flight; wait for it to finish.

3. **Scrollback inspection**: capture with `-S -50` for more context. Look for recent edits, uncommitted local state, in-flight git pull/rebase, or tool calls without rendered results.

#### If the teammate is mid-work

Do NOT send `shutdown_request` directly. SendMessage a plain question instead: "we're stopping today; what's your cleanest stopping point from where you are?" Let the teammate propose the stop boundary. Valid responses include "finish current sub-step then stop", "current state is clean, send shutdown when ready", or "rollback needed first". Give them the agency to choose; their work product is what's at risk.

**Anti-pattern**: send `shutdown_request` without checking, then send a corrective "wait, are you OK?" message later. This burns inbox slots, confuses the teammate, and forces them to reconcile a conflicting protocol signal with the actual state.

#### After verification

Send `{type: "shutdown_request", reason: "<reason>"}` via SendMessage. Wait for `shutdown_response approve: true`. After all teammates confirm, call `TeamDelete`. Do NOT call TeamDelete with active teammates; it will fail.

**Stale `isActive` after a GRACEFUL shutdown** (observed 2026-06-12): even with `shutdown_approved` received and the teammate terminated, its config entry can stay `isActive: true`, making TeamDelete fail with "Cannot cleanup team with N active member(s)". Verify the pane is actually dead (`tmux list-panes -a -F '#{pane_id}' | grep -x '<paneId>'` — paneId from the config), then flip the flag and retry:

```bash
jq '.members |= map(if .name == "<name>" then .isActive = false else . end)' \
  ~/.claude/teams/<team>/config.json > /tmp/tc.json && mv /tmp/tc.json ~/.claude/teams/<team>/config.json
```

#### Worktree cleanup after TeamDelete

TeamDelete does NOT remove the worktrees your spawn prompts told workers to create (the explicit `git worktree add` pattern from Step 3) — only worktrees the Agent tool itself created via `isolation`. After the wave's branches are merged, the lead must `git worktree remove <path>` each milestone worktree and delete the merged branches. Two follow-on gotchas: (1) removing a worktree can poison the shared golangci-lint cache — later `task lint` runs in surviving worktrees fail on phantom issues whose paths point into the removed worktree; fix with `golangci-lint cache clean`. (2) A single follow-up role task after TeamDelete (e.g. release) does not need a new team — spawn it standalone via `Agent(subagent_type: <role>)`.

#### Pane-capture caveat

`tmux capture-pane` shows scrollback, NOT only what the teammate is doing *right now*. A recent investigation in the scrollback might have already concluded; the only signal of *current* activity is the running indicator in the statusline. If you see Bash output for an action in the scrollback, assume that action is **already done** unless the statusline shows active processing AND the prompt hasn't returned yet.

### Step 6: run-mode hotfixes (role-file edits + slot collisions)

Run-mode normally assumes `.claude/agents/*.md` is stable. Two situations break that assumption and require team-lead action mid-run:

**A. Role-file hotfix when a teammate reports a structural defect.**

If a spawned teammate reports a structural problem with its own role file (missing tool in the allowlist, broken frontmatter, contradictory instructions), the team-lead MAY patch the role file in-place rather than aborting the run. Procedure:

1. Confirm the defect by reading the affected role file. A teammate's self-report is necessary but not sufficient; verify before editing.
2. Propose the fix to the user via `AskUserQuestion` with the minimal change as the recommended option and 1-2 alternatives (e.g., "workaround via files", "remove the `tools:` line entirely"). Skip the question only if the user has pre-authorized hotfixes in durable instructions.
3. Apply the patch (`Edit` on `.claude/agents/<role>.md`). Keep the change minimal: add the missing tool, fix the broken YAML, narrow the contradictory instruction. Do NOT rewrite the role wholesale; that's `/agent-team update` work.
4. Respawn the affected teammate (see slot-collision handling below). The live teammate's tool set AND model are frozen at spawn time; the patched file only takes effect on the next spawn. This also covers mid-run model changes: editing `model:` in a role file does nothing for live teammates; recycle them (graceful shutdown, then respawn), and pass the Agent tool's `model` parameter explicitly on the respawn to override regardless of role-file state.
5. After the run completes, decide whether the hotfix should be backported into the role library at `./roles.yaml` and propagated via `/agent-team update`. Flag this to the user; the team-lead does NOT edit the role library directly.

Out of scope for hotfixes: adding new roles, removing roles, changing a role's responsibilities, changing `model:`. Those are `/agent-team init`/`update` work.

**B. Slot-collision handling when respawning a teammate.**

Whether a respawn collides depends on how the previous holder ended. A graceful shutdown (shutdown_request, then `shutdown_response approve: true`, then termination) FREES the name slot: respawning with the same `name:` and `team_name:` reuses it with no suffix (verified 2026-06-10). Killing a teammate's pane without the protocol (`tmux kill-pane`, crash) leaves a stale entry in the team config (`~/.claude/teams/<team>/config.json`) with `isActive: false` that does NOT free the slot; a subsequent `Agent` spawn with the same name produces a `-N` suffix (e.g., `reviewer` becomes `reviewer-2` on first respawn, `reviewer-3` on the next). Two implications when the suffix happens:

- The live agent's NAME (for `SendMessage` targeting and `TaskUpdate` ownership) is the suffixed form. Address them as `reviewer-2`, not `reviewer`. The skill text + brief prompts must use the live name.
- The stale `reviewer` entry with `isActive: false` is harmless but clutters the roster. Optional cleanup: edit the config to remove the stale members before respawning, OR accept the suffix and move on. Do NOT call `TeamDelete` to "reset" the team mid-run; that nukes the task list and any other live members.

If the pane is alive but the agent is wedged (responded to `shutdown_request` with plain text instead of the protocol response, often because the tool allowlist excludes `SendMessage`), kill the pane directly with `tmux kill-pane -t <pane-id>`. Then respawn (accepting the `-N` suffix) with the patched role file.

## Context recycling for long-running teammates

Each teammate is a full Claude Code session with its own context window (1M for Opus 4.7, smaller for Sonnet/Haiku). Over a multi-track session, the heaviest worker (usually `coder`) accumulates context fast and starts paying prompt-cache penalties. The lead should actively recycle teammates at clean task boundaries.

### Monitoring teammate context

Each teammate runs in a tmux pane recorded at `~/.claude/teams/<team>/config.json#members[].tmuxPaneId`. Capture the live statusline:

```bash
PANE_ID=$(jq -r '.members[] | select(.name=="coder") | .tmuxPaneId' ~/.claude/teams/<team>/config.json)
tmux capture-pane -p -t "$PANE_ID" 2>/dev/null | grep -E '% of [0-9]+k'
```

Output: `▰▰▰▱▱▱▱ 51% of 1000k`: the live context budget. Run this check before EVERY new-task dispatch (it gates the >30% threshold below).

**cmux caveat** (observed 2026-06-12): under the cmux shim, `tmux capture-pane` on a teammate pane can return EMPTY (no error, no output), and no cmux text-capture RPC exists (`surface.get_text` is not a method) — so the % budget check silently fails. Fall back to judgment: count task boundaries since spawn (the heavy worker typically crosses 30% within a milestone or two), keep spawn prompts self-contained so a recycle costs little, and don't block dispatch on an unobtainable number.

### When to recycle (heuristic)

**Mandatory pre-dispatch check.** Before handing a teammate a NEW task (a fresh scope, not a fix or iteration on the task it just did), check its context budget (see Monitoring above). If it is **>30%**, recycle before dispatching: have it write a checkpoint, then either clear its context or shut it down and respawn a fresh one. Both reduce to the Recycle procedure below; a teammate has no in-place `/clear` the lead can trigger remotely, so checkpoint + shutdown + respawn-fresh IS the clear. A new task rarely needs the prior task's working memory; carrying 30%+ of now-irrelevant context degrades output quality and compounds prompt-cache cost. The heaviest worker (usually `coder`) crosses 30% within a milestone or two, so expect to recycle it at most new-task boundaries.

**Force-recycle regardless of coupling** when context >85%: even on a same-track continuation, externalize via checkpoint and respawn. Cache pressure and quality drop too far past this floor to keep the session alive.

**Do NOT recycle (keep the teammate, ignore the 30% line) when:**
- **Mid-task**: never recycle while a task is `in_progress`.
- **Fixes / iterations on the SAME task** the teammate just finished: routing reviewer or auditor findings back to the coder for the same diff, debugging the same code, iterating the same change. Here the carried context IS the point; recycling would force costly re-discovery. (This is the "not fixes for an old one" carve-out.)
- **Last task before shutdown**: no payback for the recycle cost.

### Recycle procedure

1. **Teammate writes a checkpoint**. Send a SendMessage asking the teammate to write `.claude/agent-team-tasks/<slug>-checkpoint.md` covering: files changed (full paths + commit SHAs), open questions, anything the next session of this role would need to know. The checkpoint is the only carrier of historical context across the recycle boundary.

2. **Shutdown**. Send `SendMessage({to: "<teammate>", message: {type: "shutdown_request", reason: "Recycle for context budget"}})`. Wait for `shutdown_response` with `approve: true`.

3. **Respawn**. Call Agent with the same `team_name`, `name`, `subagent_type`, and a cold-start prompt that references the checkpoint:

   ```
   Agent({
     team_name: "<team>",
     name: "<role>",
     subagent_type: "<role>",
     prompt: "Cold-start. Read .claude/agent-team-tasks/<slug>-checkpoint.md for prior context. Then: <new task>."
   })
   ```

The respawned teammate starts fresh; the checkpoint is the bridge.

### Anti-patterns

- **Recycle at NEW-task boundaries, not within a task or for same-task fixes.** Each recycle costs a cold-start prompt + checkpoint-write effort + risk of losing context that wasn't externalized. The >30% threshold gates *new-task* dispatch; it does not license recycling mid-task or per-fix.
- **Recycling without a checkpoint** orphans whatever the teammate knew that wasn't in the file system.
- **Recycling mid-task** loses working state and forces re-discovery.
- **Recycling reviewer/auditor between dispatches** is usually wasteful: they're typically dispatched once per task they're reviewing, finish their report, and idle. Their context stays low. Coder is the one to watch.

### Lead's own context

The lead's context grows from teammate wrap-up reports too. The lead cannot self-recycle without losing the team. If lead context >70%, consider asking teammates to write more terse reports referencing files rather than pasting content inline, and prune lead-side conversation by summarizing into a CLAUDE.md or memory file before context fills.

## PRD-task transitions: fresh team per task

The within-task recycling above is for one teammate at a sub-task boundary. PRD-task transitions are coarser: when team-lead is asked to start a new PRD/spec task (a "next task" or "start task" command, or any natural-language pickup of a new scope), the default is **recycle the entire team**, not reuse it. PRD-task boundaries are the cleanest recycle point in the workflow; primed context from the prior task is rarely load-bearing for the next task and carries scope-contamination risk.

Sequence:

1. **Analyze without dispatching.** Read the PRD, identify the next task, present the recommendation. The team stays dormant: no spawns, no SendMessage, no role-file edits.
2. **Wait for user acceptance.** Do NOT spawn anything until the user confirms the chosen task. Skipping this step risks priming teammates on a scope the user will redirect.
3. **On acceptance, gracefully shutdown the existing team.** Follow the Mode 3 Step 5 cleanup procedure: pre-shutdown checks (task ownership + pane activity), `SendMessage shutdown_request` + wait for `shutdown_response approve:true`, `TeamDelete`. Wedged teammates (no `SendMessage` in their allowlist) get `tmux kill-pane`. Mid-work teammates mean the prior task wasn't actually done; resolve before transitioning.
4. **TeamCreate + spawn fresh.** A new `team_name` (or reusing the same one if no stale member slots remain) avoids the `-N` suffix collision documented in Step 6 above.
5. **Run the task** per Mode 3 normal flow.

Exception: if the user explicitly wants the existing team kept alive across the transition (the next task is a tight follow-up on the same diff, primed context is directly reusable, no scope drift), skip the shutdown. But this is the exception; the default is recycle.

## Role library reference

The full role library is in `./roles.yaml`. Read it during init/update to see the canonical role descriptions, default tool allowlists, and `triggers_on` patterns. The library may evolve over time; the skill always reads it fresh.

## Gotchas

- **Team config is global**, not per-repo. Multiple repos can each have their own `.claude/agents/`, but only one team can be active at a time across the whole Claude Code instance. If a team is already running for a different repo, ask the user to clean it up first.
- **Never delete another session's team state.** `~/.claude/teams/` (and `~/.claude/tasks/`) is shared across ALL Claude Code sessions on the host. A team dir you did NOT create in THIS session may belong to a different, still-running session — do not `rm -rf` it, and do not `TeamDelete` it, to "clear a slate" before spawning. Detecting "orphaned" from your own session is unreliable: `cmux pane.list` / `cmux rpc` are scoped to the CALLER's workspace, so a session running in a different cmux window/workspace has live teammate panes you cannot enumerate from here, and `tmux list-panes -a` under the cmux shim can return empty. Safe to remove unprompted: a team dir with NO `config.json` (empty junk). NOT proven dead by your workspace failing to see its panes: a populated `config.json` with `isActive: true` members — treat it as possibly-live and ASK the user (it is likely their other session) before touching it. Observed 2026-06-13: a populated PRD team dir was deleted as "orphaned" on workspace-scoped pane evidence; it actually belonged to another live session, disrupting it.
- **Single-line description in `.claude/agents/<role>.md`**: multi-line YAML (`>-`, `|`) breaks Claude Code's parser.
- **`tools: []` field**: omit entirely if inheriting. Do not write an empty array.
- **Team-coordination tools on every non-empty `tools:` allowlist**: include `SendMessage, TaskUpdate, TaskList, TaskGet`. The role library enforces this; check survives any prompt-body or schema-tuning edits. A teammate spawned without these cannot report findings, claim/complete tasks, or respond to `shutdown_request`, even though its `prompt_body` says "Report via SendMessage". Symptom on first surface: teammate renders the report in its pane and goes idle; lead has to scrape and apply the hotfix in Step 6.A.
- **`subagent_type` matches the agent's name**, not its filename. The `name:` frontmatter field is authoritative.
- **Never pin `model: haiku`** (or Sonnet 4.5) for any role. Auto-mode is docs-gated to Sonnet 4.6 / Opus 4.6 / Opus 4.7; a haiku teammate cannot do auto-mode and stalls on every shared-system write (e.g. a release tag push needs a manual confirmation round-trip). Default documenter/release to `sonnet`; bump the reasoning-heavy roles (coder, reviewer, auditor, tester, researcher, spec-keeper, fact-checker) to `opus`. Tester is included because adversarial/scenario validation (the common testing shape here) is reasoning-heavy; keep it on `sonnet` only for a purely mechanical unit-test-suite repo. See the Step 5 model note for the full rationale.
- **Idle is normal**: teammates go idle after every turn. Do not interpret idle as "done" or "stuck". Only act when a teammate sends a message or completes a task.
- **Tasks vs SendMessage**: use TaskUpdate to mark progress (shared task list); use SendMessage for human-readable communication. Do not send structured JSON status payloads via SendMessage.
- **The shared task list can vanish mid-run** (observed: lead's `TaskUpdate` returned "Task not found" and a teammate saw its task entry disappear, mid-session, with the team still healthy). Treat git state + SendMessage reports as the source of truth; the task list is a coordination convenience. Teammates should report findings via SendMessage directly when their task entry is missing instead of stalling, and the lead should not block any flow step on task-list bookkeeping succeeding.
- **Stale duplicate message re-deliveries** (observed ~5x in one session, 2026-06-12): teammates can receive a re-delivered copy of an earlier dispatch AFTER completing it — sometimes minutes later, sometimes after the task entry has vanished from the store. Correct teammate behavior: recognize it (HEAD unchanged, work already reported), take NO action, and reply that the prior verdict stands. Correct lead behavior: confirm "stale duplicate, no re-dispatch — your verdict stands" and never treat the re-delivery as new scope or respawn-worthy. A side effect to watch: a re-delivered pre-flag list can wake the coder into an UNREQUESTED extra work round — if uncommitted WIP appears with no dispatch behind it, check pane activity, then require the standard finish-the-loop (gate, commit, report tip SHA) rather than aborting it.
- **Teammate-environment-only failures**: a teammate may hit build/tool failures specific to its sandboxed session (observed: bare `go build` failing with buildvcs "exit status 128" in every worktree because the go toolchain's git subprocess was blocked — while the same command succeeded in the lead's shell). Before accepting workaround changes to shared build files, reproduce the failure in the lead's shell; if it doesn't reproduce, it's the teammate's environment — have them use a local env workaround (e.g. `GOFLAGS=-buildvcs=false`) and keep it out of the tree.

## Examples

### Init for a fresh repo

```
User: /agent-team init
Claude: [probes repo, finds Cargo.toml + tests/ + docs/ + .github/workflows/release.yml]
       Proposed team for myproject:
       1. coder (sonnet) - will run `cargo test` before reporting done.
       2. reviewer (sonnet) - read-only.
       3. auditor (sonnet) - public repo on github; no secret scanner in CI (consider gitleaks).
       4. tester (sonnet) - cargo test framework discovered in tests/.
       5. release (sonnet) - .github/workflows/release.yml present; uses cargo-release.

       Files I would write: .claude/agents/{coder,reviewer,auditor,tester,release}.md + .claude/agent-team.md.

       Tell me what to drop, otherwise I'll write the whole thing.
User: ok
Claude: [writes 6 files, confirms]
```

### Run a task

```
User: /agent-team implement the new --json output flag
Claude: [reads .claude/agent-team.md, TeamCreate, spawns coder with the task,
        spawns reviewer + auditor in standby mode, waits]
        Team spawned. Coder is working on the --json flag. Reviewer + auditor
        primed and standing by.
[... mailbox notifications drive the flow ...]
        Coder done; reviewer + auditor dispatched on the diff.
        Reviewer: 1 non-blocking finding (consider adding a test). Auditor: clean.
        Summary for end-to-end verification: <points>. Want me to proceed with release?
User: yes
Claude: [spawns release teammate, drives tag/push, reports]
```

### Update after project change

```
User: /agent-team update
Claude: [re-probes; finds new docs/ site (mkdocs.yml just added) and no longer-present .github/workflows/release.yml]
        Proposed updates:
        + Add documenter role (mkdocs.yml + docs/ now present)
        - Remove release role (release workflow gone)
        ~ Update auditor's prompt_body (removed CI secret-scanner reference)

        Apply?
User: yes, but keep the release role (I'll add a release flow back next week)
Claude: [adds documenter, updates auditor, leaves release in place]
```

## Next steps

After this skill writes `.claude/agents/` for a repo, the team is reusable by any Claude Code session: `/agent-team <task>` or natural-language ("spin up the team to ..."). The lead reads `.claude/agent-team.md` to know the workflow.

Consider also installing the [SessionStart hook](https://code.claude.com/docs/en/agent-teams) to enable team auto-cleanup between sessions if a team gets orphaned.
