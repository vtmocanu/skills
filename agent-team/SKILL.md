---
name: agent-team
description: Auto-generate and run a per-repo Claude Code agent team. Probes the current repo (build/CI/env manifests, agent launchers, slash commands, spec dirs) and writes `.claude/agents/{role}.md` subagent definitions for the relevant roles from a library (coder, reviewer, auditor, tester, documenter, release, architect, researcher, spec-keeper, fact-checker, web-ux) plus a `.claude/agent-team.md` workflow doc. Use when (1) `/agent-team init` to create the team for the current repo, (2) `/agent-team update` to refresh after project shape changes, (3) `/agent-team {task}` to run a task with the team (spawn teammates plus drive orchestrator flow plus stop at user gates), (4) `/agent-team reflect` to review the session's agents and propose refactors plus new roles. Roles carry a frontmatter `version:` for staleness detection. Triggers include "/agent-team", "spin up a team", "auto-create agents", "agent team for this repo", "team-on-task", "reflect on the agents".
---

## Document Location

This document lives in [github.com/vtmocanu/skills](https://github.com/vtmocanu/skills) at `agent-team/SKILL.md`.

> **Note**: This is the source of truth. Generated copies in your agent's skills directory (e.g. Claude Code's `~/.claude/commands/`) are derived from this file via `dot-ai skills generate --repo https://github.com/vtmocanu/skills`. Edit here and regenerate; never edit generated copies.

## What this skill does

Builds and operates a Claude Code [agent team](https://code.claude.com/docs/en/agent-teams) tailored to the current repo. Inspired by Viktor Farcic's [`dot-agent-deck`](https://github.com/vfarcic/dot-agent-deck) (a TUI that both displays and defines multi-agent teams across Claude Code and OpenCode); this skill is the native-APIs alternative: it probes the current repo for signals, picks roles from a library, and writes Claude Code native `.claude/agents/*.md` subagent definitions plus a `.claude/agent-team.md` workflow doc, so teammates are spawned by name via the Agent tool's `subagent_type` parameter.

The skill has four modes selected by the first argument:

| Mode | Trigger | What it does |
|------|---------|--------------|
| **init** | `/agent-team init`, or no args + no `.claude/agents/` present | Probe the repo, pick roles, write `.claude/agents/<role>.md` + `.claude/agent-team.md` |
| **update** | `/agent-team update` | Re-probe, diff against existing `.claude/agents/` (roles + `version:` staleness), apply targeted changes |
| **run** | `/agent-team <task description>` | Read team manifest, spawn teammates, drive the workflow, STOP at user gates |
| **reflect** | `/agent-team reflect` | Spawn a reviewer over this session's agents; propose refactors, new roles, and version bumps |

`.claude/agents/` is a Claude Code project-scoped subagent directory. `.claude/agent-team.md` is a workflow manifest this skill writes for its own use; not loaded automatically by Claude Code but read by the skill on `run`.

## Version staleness check (on load)

Every generated `.claude/agents/<role>.md` carries a frontmatter `version:` copied from that role's `version:` in `./roles.yaml`. Whenever this skill loads in a repo that already has `.claude/agents/`, do a quick staleness pass before other work: for each agent file, read its `version:` and compare to the current role `version:` in `roles.yaml`. Surface the result in one line, e.g.:

> 3 of 6 agents are behind the library — coder (v1→v2), tester (v1→v3), documenter (missing version → treat as v0). Run `/agent-team update` to refresh.

A file with no `version:` field predates versioning; treat it as v0 (stale). This pass only INFORMS — it never auto-edits. The actual merge happens in `update` mode, which preserves each file's `## For this repo` tail. Skip the pass silently when every agent is current.

## Autonomy: spin up teams as you see fit

When this skill is loaded, the team-lead has standing authority to spawn agents and create teams without asking the user to authorize each one. Decide which roles to spawn, how many, whether to run them in the foreground or background, and when to recycle or shut them down, based on the task shape, not on per-call user confirmation. The user does NOT have to say "create coder + reviewer" or "spin up the team for this"; if the work fits the team's shape, just do it.

Scope of standing authority:

- **Spawning teammates** from the existing `.claude/agents/` roster for the active task.
- **Retiring teammates** at task boundaries (graceful shutdown; under the implicit-team API there is no team to create or delete).
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
- **Quality-gate configs**: the checks the repo can run beyond its tests. Probe for each config file, then resolve it to the command that actually runs it — prefer a task-runner target or CI job over the raw binary, since that is what contributors and CI use.

  | Slot | Config signals | Typical command |
  |---|---|---|
  | format | `.editorconfig`, `.prettierrc*`, `rustfmt.toml`, `.clang-format`, gofmt (implicit for Go) | `task fmt-check`, `prettier --check .`, `gofmt -l .`, `cargo fmt --check` |
  | lint | `.golangci.y*ml`, `eslint.config.*`, `.eslintrc*`, `biome.json`, `.oxlintrc*`, `ruff.toml`, `.flake8`, `.rubocop.yml`, `clippy.toml` | `task lint`, `golangci-lint run`, `npm run lint`, `ruff check`, `cargo clippy` |
  | typecheck | `tsconfig.json`, `mypy.ini`, `pyrightconfig.json` | `tsc --noEmit`, `mypy .` |
  | test | test dirs/manifests from the rows above | `task test`, `go test ./...`, `pytest`, `npm test` |
  | dead code | `knip.json`, `.ts-prunerc`, `deadcode`/`unused`/`unparam` in a golangci config, `vulture` config | `knip`, `deadcode -test ./...`, `vulture .` |
  | coverage | `codecov.yml`, `.coveragerc`, a `-coverprofile`/`--coverage` flag anywhere in CI or task targets | `task test-coverage`, `go test -coverprofile=…`, `vitest --coverage` |
  | security scan | `.gitleaks.toml`, `.semgrep.yml`, `.trivyignore`, gosec/bandit/govulncheck/`npm audit`/`cargo audit` invocations in CI | `gitleaks detect`, `govulncheck ./...`, `npm audit` |
  | pre-commit | `.pre-commit-config.yaml`, `lefthook.y*ml`, `.husky/` | `pre-commit run -a`, `lefthook run pre-commit` |

  **Mine the CI config for these, not just the repo root.** A repo can lint in CI with no config file at the root, and a repo can carry a `.golangci.yml` that nothing ever invokes. The CI job definitions are the evidence of what actually runs.

  **Record a slot with no check as the literal `none (gap)`, never omit the line.** An omitted slot reads as "not investigated"; `none (gap)` is what lets the tester and auditor say "this repo has no linter" instead of silently skipping it. In a monorepo, record slots per component (`lint (api)`, `lint (web)`) — one flat gate that forces a four-toolchain run for a one-line change is a gate that stops being run.

  **If the repo has no task runner and two or more slots resolve to multi-command recipes, PROPOSE writing one — and ask before creating it.** Without a runner, each slot's raw recipe gets copied into every role tail, the workflow doc, and CLAUDE.md, and then drifts independently in each. A task runner collapses that to one name per slot. Same proposal when a runner exists but does not cover every populated slot (a repo with `npm test` whose lint lives only in a CI job): offer to add the missing targets. Never create or restructure a build file silently — this is the same consent gate the documenter uses before restructuring a README.

  When you do write one, write it **repo-local and self-contained**: every target defined inline in the repo's own file, no imports of shared or remote task libraries. Some projects do use shared libraries; do not introduce one unless the user asks. Targets invoke the tools directly (`golangci-lint run`, `shellcheck …`) and stay indifferent to what puts them on `PATH`, so the same target works in a local dev shell and in CI. Where the repo's CI already runs those commands, note that it can call the same targets — one definition, two callers.
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
- **architect**: include if the repo has a design surface (`docs/adr/`, `docs/design/`, `rfcs/`, `proposals/`, `prds/`, `ARCHITECTURE.md`) OR the user requests it OR the repo is multi-component enough that up-front design pays (several services/packages, cross-cutting data flows). Designs implementation approaches before coding, reviews changes for architectural fit, and contributes to PRD writing/review; writes design docs/ADRs only, never source.
- **researcher**: opt-in only; include if the user requests it or if the repo has a substantial spec directory (≥3 documents) suggesting research-heavy work.
- **spec-keeper**: include if a `specs/` directory exists OR the user asks for spec tracking. Maintains `specs/human.md` (user-stated requirements; edits gated on user confirmation) and `specs/ai.md` (AI design decisions; auto-applied), aiming for rebuild-from-specs sufficiency.
- **fact-checker**: opt-in only; include if the user requests it or the work is claim-heavy (docs sites, READMEs/CHANGELOGs with version/URL/API facts, reports whose statements must hold). Adversarially verifies claims against code, command output, and primary sources; read-only.
- **web-ux**: include if any of the role's `triggers_on` patterns match (a web UI surface: `web/`, `frontend/`, vite/next/tailwind configs, `*.tsx`/`*.vue`/`*.svelte`). Validates web-interface work by driving it in a real browser via the `agent-browser` CLI (must be on PATH — note in the proposal if missing) and proposes UX refactor improvements; read-only. Dispatch it whenever the team's change touches a web interface.

If a borderline call needs the user, ask via AskUserQuestion before writing files.

### Step 3: tune prompt bodies

For each picked role, the `prompt_body` from `roles.yaml` is the GENERIC body, copied verbatim. Project-specific details discovered in step 1 do NOT get spliced into that body — they go into a separate `## For this repo` tail section appended to the generated file (see Step 5). Draft that tail per role from the discoveries below:

- **coder**: name the actual test/lint command (e.g. `task test`, `cargo test`, `pytest`, `devbox run -- task kcl:test`). Reference CONTRIBUTING.md / CLAUDE.md by path. Reference the spec directory if found.
- **reviewer**: cite the authoring-rules file (CONTRIBUTING.md / CLAUDE.md) by exact path and quote one or two of its load-bearing rules if obvious. Name the dead-code command from the gate slots if the repo has one, so the deletion lens in the generic body has something to run.
- **auditor**: note if the repo is public (security implications differ); give the security-scan slot verbatim — the command if one exists, or `none (gap)` — since the generic body now tells the auditor to run it rather than merely name it.
- **tester**: **paste the gate-slot table for this repo**, one line per slot, each with the exact command including its working directory (`cd api && go test ./...`) or the literal `none (gap)`. A framework name alone is not enough: the generic body tells the tester to run every populated slot, so a tail that says "vitest" gives it nothing to invoke. This is the one role whose tail may exceed the 1-3 sentence cap below — a monorepo with four toolchains needs four sets of slots, and truncating them is what leaves checks unrun. Also record here any command whose runtime exceeds the generic 5-minute live-wait bound, with its real bound (e.g. "`./e2e/run-e2e.sh` takes ~30min; let it finish").
- **documenter**: name the doc site generator (mkdocs, hugo, docusaurus) or "plain markdown" if none. The documenter also owns the repo's `CHANGELOG.md` and keeps it TERSE: one concise line per change under an `[Unreleased]` section (Keep a Changelog style), not paragraphs. Every feature/fix/behaviour change the team ships gets a one-line entry there; the releaser later folds `[Unreleased]` into the cut version (see Mode 3 Step 3). If the repo has no CHANGELOG yet, the documenter creates one. The documenter also carries the README/docs house style (terse README as a launchpad, reference detail in a `docs/` folder); when the repo's README diverges (a large monolithic README, or no `docs/` — the same signal as the >500-line-README include trigger in Step 2), it PROPOSES a migration and asks the user before restructuring, never doing it silently. For repos with non-trivial architecture (multiple components/services, cross-cutting data flows, trust boundaries) it also keeps an `ARCHITECTURE.md` at the root, creating one when it helps a new reader and skipping it for small/simple repos where the README conveys the shape.
- **release**: name the release flow doc by path (`docs/releasing.md`, etc.) and the release command (`semantic-release`, `goreleaser`, manual tag-push).
- **architect**: name the design-doc/ADR directory and its numbering/format convention if one exists (or note there is none, so the role proposes before creating one); list the repo's major components so designs map onto them.
- **researcher**: name the spec directory.
- **spec-keeper**: name the spec directory path if it differs from `specs/`; note any existing spec files to adopt instead of creating fresh ones.
- **fact-checker**: name the claim-bearing surfaces in this repo (docs dir, README, CHANGELOG, published specs) and any authoritative sources to check against (the code itself, CI status, official upstream docs).
- **web-ux**: name how to reach a running instance of the UI (dev-server command, compose service + port, demo/mock build) and the design-token/style-system files if the repo has them; note any repo-specific UX contract (design system, a11y bar, target browsers).

Keep the tail tight: 1-3 sentences per role, the tester's gate-slot table excepted. The generic body already covers the shape; the `## For this repo` tail only carries what is specific to THIS repo (exact commands, file paths, framework names, how to reach the app). Keeping repo-specifics in the tail — never spliced into the generic body — is what lets `update`/uzi-sync replace the versioned generic body later without clobbering local tuning.

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
version: <role's version integer from roles.yaml, e.g. 1>
description: <role's description from roles.yaml, lightly tuned if needed>
tools: <comma-separated allowlist from roles.yaml, or omit field entirely if empty>
model: <model from roles.yaml, or omit>
---

<GENERIC prompt_body from roles.yaml, copied verbatim>

## For this repo

<the repo-specific tail drafted in Step 3 — exact commands, paths, framework
names, how to reach the app. OMIT this whole heading if the role has no
repo-specifics; the file is then pure generic body at that version.>
```

**`version:` frontmatter** stamps which `roles.yaml` version this file was generated from. Claude Code tolerates the custom key (verified on 2.1.215: an agent with `version:` in frontmatter loads and spawns normally) — it is not in the documented key set but is silently ignored by the loader. Copy the integer straight from the role's `version:` in `roles.yaml`. This is the field `update` mode diffs to find stale agents. (Uzi's builtin parser is stricter and rejects unknown keys — versioning uzi builtins needs the separate Go change tracked in the uzi issue; it does not affect these Claude Code files.)

**`## For this repo` tail** holds ALL repo-specific tuning (Step 3), kept out of the generic body so `update`/sync can replace the generic body by version without touching local edits. Everything above the tail must be the verbatim `roles.yaml` body for that version.

**Tool allowlist format**: Claude Code subagent frontmatter accepts `tools:` as a comma-separated list. If `roles.yaml` says `tools: []` (empty array, meaning inherit all), OMIT the `tools` line entirely from the frontmatter. Do not write `tools: []`.

**Team-coordination tools (REQUIRED on any non-empty allowlist)**: every role with a `tools:` line MUST include `SendMessage, TaskUpdate, TaskList, TaskGet`. Without these, the spawned agent produces its report but cannot transmit it to team-lead, claim/complete tasks, or respond to `shutdown_request`. The role library at `roles.yaml` already includes them on every non-empty allowlist; verify they survived any prompt-body or schema-tuning edits you make in Step 3.

**Frontmatter MUST be single-line for `description`** (multi-line YAML block scalars break Claude Code's parser).

**Model tiers (define in frontmatter, ENFORCE via the Agent `model` param)**: the role file's `model:` is where the roster DEFINES each role's tier, but it is NOT reliably honored at spawn — observed 2026-07-05: a `model: sonnet` documenter spawned without an explicit override ran on the parent session's model instead. So the lead MUST read the role file's `model:` and pass it explicitly via the Agent tool's `model` parameter on EVERY teammate spawn and respawn; treat frontmatter-only as declared intent, never as enforcement.

Tier guidance (alias names, deliberately not version-pinned — model families rotate; the Claude 5 family shipped after this guidance was first written): reasoning-heavy roles (coder, reviewer, auditor, tester, architect, researcher, spec-keeper, fact-checker, web-ux) get the strong tier (`opus`); mechanical roles (documenter, release) get the mid tier (`sonnet`); never pin the smallest tier (`haiku`) for any role. Rationale for the haiku ban: auto-mode (the bias-to-act permission mode that lets teammates complete shared-system writes like a release push without a manual confirmation round-trip) is gated to specific models — the smallest tier has been excluded; **verify the current auto-mode-capable list in the Claude Code docs** rather than trusting a version list here. The tester is strong-tier because testing in most repos here is adversarial/scenario validation (crafting fixtures to break a guard, reasoning about evasion vectors, probing runtime contracts); keep tester on `sonnet` only when the repo's testing is a purely mechanical unit-test-suite run. Note (per the docs): a subagent's frontmatter `permissionMode` is ignored; its actions are classified under the parent session's rules, but only if the subagent's model is itself auto-mode-capable, which is why the model choice (not a mode flag) is the lever here.

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
You coordinate the team via Agent (name + subagent_type) to spawn teammates,
SendMessage to communicate, and the Task* tools to track work. The session
has ONE implicit team; there is nothing to create or delete.

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

## Re-derive the claim at the moment you assert it (CRITICAL)

**Re-derive a claim from the code at the moment you assert it, however sure you
are.** Having verified something once is not knowing it: you verified a *past*
state and you assert in the present. This applies to every role, including the
lead.

**A comment is an assertion, so it deserves the same mutation as a test.**
Freeze the field, drop the line, move the path, and watch the assertion fail.
If nothing fails, the comment describes a mechanism that is not there.

Corollaries, each earned:
- **The code is usually right; the story is what rots.** Every instance below had
  correct logic and a wrong description. Nobody was careless: each claim was true
  when written and stopped being true, or was never re-derived from the code.
- **Presence is not efficacy.** "The attribute is there" and "it reaches anyone"
  are different claims. Two validators can both be right and appear to conflict
  because they asked different questions: find the two questions before picking a
  winner.
- **The experiment that justifies a choice usually also bounds it.** Record both
  halves, not the flattering one.
- **It hides in the artifacts with no gate.** Comments get read in review, tests
  get run, commit messages get diffed. A "still open" list, a checkpoint, a
  handoff note is prose nobody executes, and it decides where the next person
  spends their time. Re-derive those too.

Validated 2026-07-16 on a PRD where **nine claims fell over**, each believed by
someone competent and each disproved in seconds once someone ran it: a PRD
decision asserting a quota was atomic (measured: 8 of 8 concurrent provisions
passed a quota of 2); a design claiming one test caught a misplaced lock (it
stays green, because a misplaced lock still blocks); a design claiming only a
browser could prove a UI gate escapable (a page-level test does it); three code
comments naming mechanisms the code did not have; a test-count baseline carried
from memory; a handoff note that outlived the fix that killed it and was
reported open twice; and a browser pass that "verified" a `title` reaching no
screen-reader user. The coder that made four of them diagnosed the root: *"I
trusted any claim I had personally verified once, and stopped re-checking it,
because having checked it felt like knowing it."*

**Lead's share of this:** relay findings as claims to check, not facts to apply.
When you forward a teammate's finding, say what was measured and what was
inferred. Twice in that run the lead propagated a validator's inherited
attribution as verified, and once told a reviewer a rule ("focus is proven by
identity, never text") that was half right: identity fails too, when the selector
drifts. Verify a load-bearing claim yourself before acting on it, and say plainly
when you did not.

## Quality gates

One line per slot, in this order, each with the exact command or the literal
`none (gap)`. Per component in a monorepo. This block is the tester's and the
reviewer's source of truth; a slot omitted here is a slot nobody runs.

- format: <command | none (gap)>
- lint: <command | none (gap)>
- typecheck: <command | none (gap)>
- test: <command | none (gap)>
- dead code: <command | none (gap)>
- coverage: <command | none (gap)>
- security scan: <command | none (gap)>
- pre-commit: <command | none (gap)>
- long-running: <any gate command exceeding the tester's 5-minute default wait, with its real bound>

## Project signals

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

1. Read existing `.claude/agents/*.md` files (each file's frontmatter `version:` and its `## For this repo` tail) and `.claude/agent-team.md`.
2. Re-run discovery (step 1 of init).
3. Diff along three axes:
   - **Roster**: roles in the library that match repo signals but are missing from `.claude/agents/`, OR roles present whose `triggers_on` no longer match (tests removed, release flow gone, etc.).
   - **Version staleness**: for each present role, compare its file `version:` to that role's current `version:` in `roles.yaml`. A lower or missing number means the generic body drifted behind the library — read both bodies so you can summarize WHAT changed, not just the number.
   - **Tuning drift**: repo facts in a `## For this repo` tail that no longer hold (renamed test command, moved spec dir). Gate slots drift the same way and are worth re-deriving: a repo can gain a linter, a dead-code check, or a task runner after the team was generated, and a tail still naming the old raw recipe sends the tester at a command that no longer matches CI.
4. Present the diff to the user as a numbered proposal: additions / removals / version bumps (one line of "what changed" each) / tail fixes.
5. On confirmation, apply targeted edits:
   - **Version bump**: replace the generic body (everything ABOVE the `## For this repo` heading) with the current `roles.yaml` body, update the frontmatter `version:`, and leave the tail untouched. This is exactly why Step 3 keeps repo-specifics in the tail — the generic part is replaceable wholesale.
   - **Legacy files with no tail split** (older generated agents, or inline hand-tuning): the boundary is not mechanical, so do NOT blind-overwrite. Read the file, separate library-origin paragraphs from manual ones, replace only the library parts, and migrate the manual repo-specifics into a new `## For this repo` tail so the NEXT update is clean. When unsure which is which, quote the paragraph and ask.
   - **Roster add/remove**: write or delete the role file as in init Step 5.

**Downstream vendored copies (only when you EDIT `roles.yaml` itself, NOT on a normal repo `update`).** If a downstream app vendors these role bodies as its own built-in templates (shipped-in-binary defaults, seeded into a DB, etc.), a change to a role's generic body/description/tools/model in `roles.yaml` — or a new role — leaves that copy behind. When you make such a `roles.yaml` edit, propose re-syncing the downstream copy: apply the same generic-body change there, preserve each copy's own `## For this repo` tail and any built-ins it owns that have no `roles.yaml` equivalent, and mirror new roles across. Note that a downstream parser may be stricter than Claude Code's loader and reject the `version:` frontmatter key; such a copy carries the new body content but needs its own change before it can store the version stamp. This is a proposal gated like any `roles.yaml` edit; never auto-commit into another repo. (Concrete downstream targets and their tracking issues are kept out of this public file; check the maintainer's notes.)

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

### Step 2: plan tasks

The session already has one implicit team — there is nothing to create. Go straight to planning the task list.

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
  name: "coder",
  subagent_type: "coder",
  model: "<tier from the role file>",   // pass explicitly — frontmatter model is not honored at spawn
  prompt: "<task-specific cold-start prompt>"
})
```

For `coder`, the prompt includes the full task description plus paths to read.

For `reviewer` and `auditor` (which depend on coder's output), the prompt is "stand by, prime your context by reading <PRD/CLAUDE.md/CONTRIBUTING.md>, then go idle. Wait for the team lead to send the diff/files to review/audit."

For `release`, do NOT spawn at start; spawn only after user confirms post-audit. When you DO spawn it, the spawn prompt MUST instruct the releaser to include the changelog in the release: before tagging, fold the pending `CHANGELOG.md` entries into the version being cut (rename an `[Unreleased]` section to the version with its date, or otherwise assemble the version's notes); after the release publishes, confirm the release page / GitHub Release body carries that changelog section, not just an empty or auto-generated-commits-only body. A release that ships without its changelog is incomplete.

For `spec-keeper`, do NOT spawn at start; spawn after blocking review/audit findings are resolved. Its spawn prompt MUST carry a provenance breakdown of the change: which requirements and decisions came from the user (verbatim where possible) and which were AI choices made along the way. The team lead is the only one who has seen the full conversation, so assembling this breakdown is lead work; without it the spec-keeper cannot file decisions into `specs/human.md` vs `specs/ai.md` correctly.

**Parallel same-repo waves (multiple implementers editing one repo at once):**

- Do NOT rely on the Agent tool's `isolation: "worktree"` parameter for teammates: it has been observed to silently not isolate (one teammate switched the lead worktree's branch mid-run, 2026-06-10). Instead, put explicit worktree setup in each spawn prompt: `git worktree add <repo-parent>/<branch> <branch>`, then "cd there and do ALL work in that worktree; NEVER touch the lead's worktree or switch its branch".
- When N workers' branches will merge into one integration branch, instruct every worker NOT to bump VERSION/CHANGELOG (or any other shared file all milestones would touch); the lead does one consolidated bump after merging. Otherwise the merge hits N-way conflicts on those files.
- **Before merging a worker branch, verify its ref == the worker's last reported SHA** (`git rev-parse <branch>`; `git worktree list` must show the branch at that SHA, NOT `(detached HEAD)`). A follow-up commit (a review/hardening fix) made on a detached HEAD leaves the branch ref behind, so `git merge <branch>` silently integrates the STALE pre-fix code and DROPS the follow-up — and tests still pass when the dropped delta was additive, so it is invisible without this check (observed 2026-06-16: a coder's MEDIUM trace-read security fix vanished from the merge because its hardening commit sat on a detached HEAD; `(detached HEAD)` in `git worktree list` was the tell, caught only later by a docs fact-check against the code). Fix: merge the reported SHA directly (`git merge <sha>`), or confirm the ref first; either way, after merging grep the integration tree for a signature line from each follow-up to prove it landed. (This is the merge-time face of "trust but verify against artifacts" in Step 4.) When directing worker A to merge worker B's branch, pin B's last reported tip in the dispatch but add "check the live tip and take whatever it is; if it moved past the pinned SHA, say so instead of guessing" — B may land a final small commit while the dispatch is in flight (observed 2026-07-05: a cosmetic fix landed on the web branch seconds after the merge dispatch; the live-tip instruction absorbed it without a round-trip).
- If a wave is aborted before commits land, leftover empty branches are fine: tell the respawned worker to reuse the existing branch instead of deleting and recreating it (branch deletion may be denied by the permission classifier as destructive).
- **Shared utility needed by two parallel branches (e.g. two PRD teams both needing one new package): cherry-pick the exact commit that introduces it, never reimplement.** Byte-identical content on both branches merges trivially; a functionally-identical reimplementation guarantees add/add conflicts on the package plus divergent edits to shared wiring files (config, compose, .env.example). If a worker already committed its own version before the coordination signal arrived and that SHA has NOT been dispatched for review, replace it (`git stash -u` any adopted working-tree files → `git reset --hard HEAD~1` → `git cherry-pick <shared-sha>` → `git stash pop`) rather than living with the fork (observed 2026-07-03: two AES secretbox implementations, same public API, different comments and config wiring, on sibling PRD branches).
- **SEQUENTIAL pipelines can share ONE worktree with a lead-enforced writer token** (validated 2026-07-05: coder → documenter → spec-keeper → coder handoffs on one branch, 8 writer transitions, zero collisions). Rules: exactly one teammate unfrozen for writes at a time; every dispatch to a new writer names the verified tip SHA; the outgoing writer explicitly confirms FREEZE (and the lead verifies the tree is clean at the expected tip) before the next GO; read-only agents (reviewer/auditor/tester/fact-checker) run in parallel freely. Use per-worker worktrees only when writers must genuinely work CONCURRENTLY. When a validator must BUILD/TEST a pinned SHA while the shared tree carries the current writer's uncommitted WIP, it should verify in a throwaway detached worktree (`git worktree add --detach <tmp> <sha>`, removed after) — validated 2026-07-10: a reviewer's `go build` failed on the live tree purely from the coder's in-progress M3 edits, and the detached-worktree run cleanly verified the committed SHA. **Lead edits to a shared file in the active writer's worktree** (PRD checkbox bookkeeping, brief updates) are fine WITHOUT taking the token if you pre-warn the writer in the same breath: "the uncommitted change in <file> is MINE (team lead), expected — do not stop; fold it into your next commit." The pre-warning defuses the concurrent-writer guard the spawn prompt installed; without it the guard correctly reads the edit as a foreign writer and freezes the worker (validated 2026-07-13: a mid-milestone PRD progress edit landed cleanly inside the coder's next commit, zero disruption).

Teammates run as **background agents**: an `Agent(...)` spawn returns immediately (background is the Agent tool's default), and each teammate's completion or idle state arrives as an automatic notification, so you do NOT poll and there are no panes to lay out or watch. Drive the flow off those notifications (Step 4), and check status with `TaskList` / `TaskGet` when you need it. Never `Read` a teammate's `.output` file: for an agent it is the full conversation transcript and will overflow your context.

### Step 4: drive the flow

- Wait for coder's completion message via the automatic mailbox notifications (do NOT poll).
- On coder done: SendMessage to reviewer and auditor with the diff summary, file paths changed, and the coder's report. They claim their tasks and report findings.
- **Pin review scope to explicit commit SHAs** in the review dispatch, and if the worker adds commits after the review was dispatched (a follow-up fix, a crossed-in-flight reconciliation), immediately SendMessage the reviewer the new tip and require explicit confirmation that ALL commits were covered. Observed failure mode: a reviewer's report cited only the first of two commits on the branch; the second commit was verified only after a direct "did you cover SHA X?" follow-up. A second tell (observed 2026-07-05): a validator's report describing behavior that CONTRADICTS the worker's description of a later commit (e.g. praising semantics the follow-up reversed) means the validator reviewed a stale tip — require a re-read at the live tip and an explicit ruling before accepting either claim.
- **No amends after review dispatch — put this rule in the coder's INITIAL spawn prompt.** Workers may amend freely BEFORE a SHA is dispatched for review; once dispatched, fixes must land as follow-up commits, never amends. An amend orphans the pinned SHA (it stops being an ancestor of HEAD), invalidates in-flight review scope, and forces a re-confirmation round-trip with every validator. Observed twice in one run (2026-06-12): both amends crossed the reviewer's/auditor's reports in flight, and each validator had to re-diff and re-confirm coverage of the new tip; after the rule was sent mid-run, all later fixes stacked cleanly. Stating it at spawn time costs one sentence; correcting it mid-run costs a round per validator per amend.
- **Pipeline milestones across review waves**: once a milestone's SHAs are frozen and dispatched to the read-only validators, dispatch the coder's NEXT milestone immediately — do not idle the coder waiting for the wave. The no-amends rule makes this safe: any findings come back as labeled follow-up commits on top of whatever the coder has built since, and validators needing to build a pinned SHA use a detached worktree. Validated 2026-07-13: a 6-milestone PRD ran coder-implementation and review waves fully overlapped, zero rework, zero blocking findings, no idle coder time. Two docker-stack agents in one worktree (a validator's kept e2e stack + the coder's e2e gate) may NOT collide — check how the harness derives its compose project name (a PID-derived unique project = safe to overlap) before serializing them.
- On reviewer + auditor both done: synthesize findings for the user. If any blocking finding, route back to coder via SendMessage.
- **Trust but verify teammate-reported gate results against artifacts** when the result matters beyond the report: a teammate's "task build green" can be stale-cache luck or a partial run. Cheap checks: binary timestamp/version after a build claim, `git log` tip after a commit claim. (Observed: a release agent reported the build gate green while the installed binary was left months stale.) The same applies to **time-sensitive repo-state claims** ("branch X has/lacks file Y"): re-run the check yourself before making a coordination decision on it, especially when two teammates' claims conflict — a standby agent's primed check goes stale within minutes while parallel branches advance (observed 2026-07-03: an auditor's "secretbox absent on the sibling PRD branch" was contradicted by the reviewer; the lead's own `git ls-tree` settled it and reversed the build-vs-cherry-pick decision). **This generalises past gate results to every claim the team asserts, including its own comments and your relays** — see the "Re-derive the claim at the moment you assert it" section in the workflow-doc template above, which is the compact form to write into `.claude/agent-team.md` at init. The lead's specific share: **relay findings as claims to check, not facts to apply**, and say what was measured vs inferred. Validated 2026-07-16, where the lead twice propagated a validator's *inherited* attribution as verified — one of them a mechanism a comment had asserted and nobody had re-derived, which then collected a second reviewer's certification before a third agent froze the field and found nothing read it.
- A teammate going idle WITHOUT reporting, right after being asked for a small fix, may be stalled mid-fix rather than working: check `git -C <their-worktree> status` — idle + uncommitted edits = stalled; send a targeted "finish the loop: run the gate, commit, report the SHA" nudge. Prevent it at dispatch time too: any message that sends NEW requirements to a worker that already reported done MUST explicitly end with "run the gate, commit, and report the new tip SHA" (observed 2026-06-10: a coder applied forwarded security hardening but went idle with it uncommitted — the "release-ready" branch tip lacked the fix, and only the auditor's `git status` check caught it). **False-stall caveat**: when the role's gate is a long multi-phase job (an e2e stack that cycles services down/up, a long build), point-in-time evidence lies — "no container/process running" can be an inter-phase gap and stale source-file mtimes just mean the agent is waiting on the gate, not wedged. Before escalating, send ONE status question with forced options ("(a) working + where, (b) blocked on X + error, (c) done, committing") and wait a full known-gate-duration for the reply; escalate to checkpoint/respawn only after that window passes silent (observed 2026-07-05: a coder mid-e2e was nudged twice as "stalled" on no-stack-running + 28-min-old mtimes; the e2e legitimately cycles the stack and it was running the whole time). A worker legitimately waiting on a long gate may answer "(a) working — a background wait-loop will notify me on exit"; accept that, but do NOT trust the wake to happen: a background wait-loop does not reliably re-invoke the agent when the watched job exits (observed 2026-07-13: the e2e finished overnight — its exit trap even tore the stack down — but the coder never woke to report, leaving green-tested WIP uncommitted for hours). If the gate's known duration passes with no report, verify the artifacts yourself (stack gone? tree state?) and send a finish-the-loop nudge that STATES the verified evidence ("your stack is gone so the run exited; check your log, commit, report the tip"). When the LEAD arms its own watcher on the worker's background gate log, first verify the completion marker actually reaches the watched file (observed 2026-07-15: the worker ran `cmd > log 2>&1; echo EXIT=$?` — the marker goes to the shell's stdout, NOT the redirected log, so a grep-for-marker watcher would never fire); watch for the harness's own final log lines (its "all passed"/summary output) or process-disappearance instead. The validated watcher shape (2026-07-15): a lead-side background loop that pgrep-polls for the gate SCRIPT itself until the process disappears, then fires — no log path needed; follow it with a nudge that states the verified evidence, and the worker reconciles immediately.
- **Standby reviewers/auditors often surface baseline pre-flags while priming** (they read the target code before the coder finishes). Forward actionable pre-flags to the coder MID-implementation instead of holding them for the review round — requirements are cheaper upstream than as blocking findings (observed: an auditor's pre-flags became the spec for a hardening commit, avoiding a full fix-and-re-review cycle). CROSSING HAZARD (observed 2026-07-15): a pre-flag forwarded while the coder is mid-milestone can be acted on AFTER the milestone's review wave already ran — the coder lands a follow-up commit implementing a pre-flag suggestion whose OPPOSITE the wave just praised, leaving two contradictory validator positions on file. Don't let both reports stand: dispatch a scoped delta review of the follow-up commit that names the contradiction and demands an explicit keep/revert ruling (that run: both validators ruled KEEP and one retracted its earlier framing — one cheap delta round, clean record).
- **If the tester role exists and the change alters behavior, dispatch it by default** on the scenario surface (the real runtime path: live conversation, real container/deploy entrypoint, the spec's stated success criteria). Coder-authored unit/security suites are NOT a substitute for end-to-end scenario proof — they test components, not the user-visible criterion. Observed (2026-06-12): a PRD's headline success criterion ("answers question X end-to-end via the real docker path") sat unproven through four reviewed milestones because the test matrix looked comprehensive; the user caught the missing tester, whose E2E run then produced the only direct evidence of the criterion (plus an adversarial injection probe and live doc-example verification the suites couldn't provide). **Kept-stack validation wave** (validated 2026-07-15): when the coder's final long gate supports a keep-stack/keep-instance flag, have it run the gate WITH that flag so tester + web-ux validate against the SAME live instance in parallel right after — one gate run serves three validators. Hostile/mutating probes go to a PHANTOM second identity (e.g. a worker row minted from a fresh join token that no real process drives) instead of racing the real component's own update cadence — deterministic assertions, no flapping. The lead coordinates teardown only after the whole wave reports. SHARED-STACK VALIDATOR RACE (observed 2026-07-15): a MUTATING tester and a read-only browser validator on the same stack collide two ways — the tester's authorized mutations flip the persona states the other validator was briefed to expect (a token delete/save changes no_token/unavailable rows mid-pass), and an app with single-active-session-per-user revokes each other's logins mid-journey. Mitigations: partition personas per validator (tester mutates only personas the browser pass doesn't rely on), brief the read-only validator that states are a moving target and to verify each rendering against the authoritative API at read time (that's what saved the run — the SPA always matched its API, so both passed), and the moment the tester reports, relay its residual-state list to any validator still driving the stack.
- If the architect role exists: for a non-trivial task (new component, cross-cutting change, new or changed contract/interface), dispatch it BEFORE the coder and fold its design summary (or the ADR path it wrote) into the coder's spawn prompt; skip it for small fixes. Post-implementation, it can join the reviewer/auditor wave for an architectural-fit pass when the change moved boundaries. Also dispatch it whenever a PRD is being written or reviewed (including `/prd-create`-style flows): it contributes the architecture sections and the milestone decomposition/dependency graph when writing, and judges feasibility, hidden milestone coupling, and independent shippability when reviewing. Open design questions it flags go to the user, not to the coder as guesses.
- If the fact-checker role exists: dispatch it in the same wave as reviewer/auditor, scoped to the change's claim-bearing artifacts (docs, README, CHANGELOG, report prose), with explicit pointers to which claims matter. Treat REFUTED claims as blocking findings; UNVERIFIABLE ones go to the user with what would be needed to verify.
- If the web-ux role exists and the change touches a web interface: dispatch it in the same wave as reviewer/auditor, with a reachable URL for the running UI (start the dev server / compose service / demo build first, or tell it how to) and the list of changed flows. It validates in a real browser via `agent-browser` and reports UX findings plus refactor proposals; treat Blocking findings like reviewer blockers, and relay Enhancement proposals to the user rather than auto-scheduling them. Dispatch it on the wave where a user-facing control or journey LANDS (not only once at task end), and require it to drive the feature's PRIMARY journey end-to-end — the "can the user actually do the thing" click-through — because component reviews and API-level e2e both pass while a client-side gate dead-ends the UI (observed 2026-07-06: a server-side gate bypass was fully unit-tested, audited, and e2e-green while the UI's Start button stayed disabled — the client gate never learned the bypass; only the browser pass caught it). **Prefer pointing it at a mock/demo build or an isolated dummy-data stack** — its role forbids real mutations (destructive buttons, merges, sends) without explicit user permission, so on a real stack it can only navigate read-only and will report mutation-bearing flows as not-validated, proposing a mock instance instead; relay that proposal (or the permission ask) to the user.
- **Documentation migrations get a fidelity-first review pass.** When the documenter does a large doc change (a README→`docs/` migration, a relocation), scope the review to five lenses: content **fidelity** (diff the pre-change source against the new corpus — nothing dropped or altered), **link integrity** (relative links + anchors + images resolve), **accuracy** vs the source (env vars, script names, paths), **structure**/house-style (terse README, one concern per `docs/` file, no duplication/contradiction), and **newcomer-UX** (can a new reader get running and find things following only the docs). Dispatch reviewer + fact-checker scoped accordingly; the documenter should already have self-checked fidelity/links/inbound-refs before handing off (per its role). Scale the agent count to the change size — a few lenses for a small edit, one agent per lens for a full migration.
- If the spec-keeper role exists: once blocking findings are resolved, dispatch it with the change summary plus the user-vs-AI provenance breakdown (see Step 3). It applies `specs/ai.md` updates directly and sends proposed `specs/human.md` edits back; relay those to the user for confirmation before telling the spec-keeper to apply them. The task is not complete until specs are in sync — the bar is that the code could be rebuilt from `specs/` alone, with `specs/human.md` treated as the binding contract.
- Before release (if applicable): present an end-to-end verification summary and STOP. Ask the user to confirm before spawning the release teammate.
- When you spawn release, put the changelog requirement in its prompt (see Step 3): the releaser must ensure the version's `CHANGELOG.md` entries are folded into the cut and that the published release page carries them.
- **Expect default-branch drift before release/merge**: long team sessions race bot pushes (Renovate dep PRs auto-merging to main, CI config bumps) AND sibling agent-team sessions merging their own PRDs (observed 2026-07-05: a sibling PRD merged between "main unchanged" verified at push time and the MR merge minutes later — conflict + a migration-number collision). A drift check at push time can be stale by merge time: re-verify divergence/mergeability IMMEDIATELY before the merge/tag action. Reconcile with a plain `git merge origin/<default>` (NEVER force-push — destroys the bot commits; avoid rebasing already-reviewed merge commits — rewrites the audited SHAs), apply any repo-specific landing conventions the drift triggers — renumber append-numbered artifacts above the merged head (goose-style migrations AND monotonic spec/doc section numbers; validated 2026-07-10: ai.md §142-146 → §160-164 after two sibling PRDs landed): the mechanical renumber goes to the merging worker as part of conflict resolution, with a follow-up audit by the role that owns the file — re-run the build gate on the merged tip, then merge/tag that tip.

### Step 5: cleanup

When the task is complete and the user is done, do NOT send `shutdown_request` blindly. Verify each teammate is at a clean stop boundary first.

#### Pre-shutdown checks (mandatory before SendMessage shutdown_request)

1. **Task-ownership check**: Run TaskList. If the teammate owns any task in `in_progress` status, they may be mid-work. Investigate before shutting down.

2. **Idle vs mid-work**: a background teammate is at a clean stop when its most recent notification was completion/idle AND it owns no `in_progress` task. You cannot watch it work in real time, so when unsure do NOT assume: `SendMessage` a status question with forced options ("(a) working + where, (b) blocked on X, (c) done + clean") and wait for the reply.

3. **Uncommitted-state check**: inspect the teammate's worktree with `git -C <worktree> status`. Idle + uncommitted edits = stalled mid-fix, not done; send a finish-the-loop nudge (run the gate, commit, report the SHA) before shutting it down. With no pane to read, this git check is the reliable "is there unsaved work?" signal.

#### If the teammate is mid-work

Do NOT send `shutdown_request` directly. SendMessage a plain question instead: "we're stopping today; what's your cleanest stopping point from where you are?" Let the teammate propose the stop boundary. Valid responses include "finish current sub-step then stop", "current state is clean, send shutdown when ready", or "rollback needed first". Give them the agency to choose; their work product is what's at risk.

**Anti-pattern**: send `shutdown_request` without checking, then send a corrective "wait, are you OK?" message later. This burns inbox slots, confuses the teammate, and forces them to reconcile a conflicting protocol signal with the actual state.

#### After verification

Send `{type: "shutdown_request", reason: "<reason>"}` via SendMessage to each teammate and wait for `shutdown_response approve: true`. That is the whole cleanup: under the implicit-team API there is no `TeamDelete` — a shut-down teammate just frees its name slot. (On an older build that still exposes `TeamDelete`, call it only after every member confirms, never with an active member; see the "Old team API" note in Gotchas.)

**Stale `isActive` after a GRACEFUL shutdown** (observed 2026-06-12): even with `shutdown_approved` received and the teammate terminated, its config entry can stay `isActive: true`. This keeps the name slot from freeing for a clean respawn (and on older builds makes `TeamDelete` fail with "Cannot cleanup team with N active member(s)"). Confirm you received `shutdown_response approve: true` (the only proof of termination), then flip the flag in the team registry:

```bash
jq '.members |= map(if .name == "<name>" then .isActive = false else . end)' \
  ~/.claude/teams/<session>/config.json > /tmp/tc.json && mv /tmp/tc.json ~/.claude/teams/<session>/config.json
```

#### Worktree cleanup after shutdown

Shutting a teammate down does NOT remove the worktrees your spawn prompts told workers to create (the explicit `git worktree add` pattern from Step 3) — only worktrees the Agent tool itself created via `isolation`. After the wave's branches are merged, the lead must `git worktree remove <path>` each milestone worktree and delete the merged branches. Two follow-on gotchas: (1) removing a worktree can poison the shared golangci-lint cache — later `task lint` runs in surviving worktrees fail on phantom issues whose paths point into the removed worktree; fix with `golangci-lint cache clean`. (2) A single follow-up role task after the run (e.g. release) needs nothing special — the implicit team persists for the session, so spawn it standalone via `Agent(subagent_type: <role>)`.

#### Offer a reflect pass (end of a substantial run)

After a run that exercised the team substantially — several delegations, a mid-run role-file hotfix, a teammate that struggled, or work the roster handled awkwardly — OFFER (do not auto-run) a reflect pass: "Want me to run `/agent-team reflect` to capture agent improvements from this session?" There is no session-end hook, so this offer is the only trigger. Make it once, at cleanup, and only when the session actually surfaced something worth capturing; skip it for a quick one-delegation run that went cleanly.

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

Whether a respawn collides depends on how the previous holder ended. A graceful shutdown (shutdown_request, then `shutdown_response approve: true`, then termination) FREES the name slot: respawning with the same `name:` reuses it with no suffix (verified 2026-06-10). Force-stopping a teammate without the protocol (`TaskStop`, or a crash) leaves a stale entry in the team config (`~/.claude/teams/<session>/config.json`) with `isActive: false` that does NOT free the slot; a subsequent `Agent` spawn with the same name produces a `-N` suffix (e.g., `reviewer` becomes `reviewer-2` on first respawn, `reviewer-3` on the next). Two implications when the suffix happens:

- The live agent's NAME (for `SendMessage` targeting and `TaskUpdate` ownership) is the suffixed form. Address them as `reviewer-2`, not `reviewer`. The skill text + brief prompts must use the live name.
- The stale `reviewer` entry with `isActive: false` is harmless but clutters the roster. Optional cleanup: edit the config to remove the stale members before respawning, OR accept the suffix and move on. Do NOT wipe the team config (or call `TeamDelete` on an older build) to "reset" the team mid-run; that nukes the task list and any other live members.

If the agent is wedged (responded to `shutdown_request` with plain text instead of the protocol response, often because the tool allowlist excludes `SendMessage`), force-stop it with `TaskStop({task_id: "<teammate-name>"})` — the tool accepts a bare teammate name or agent ID. Then respawn (accepting the `-N` suffix) with the patched role file.

## Context recycling for long-running teammates

Each teammate is a full Claude Code session with its own context window (1M for Opus 4.7, smaller for Sonnet/Haiku). Over a multi-track session, the heaviest worker (usually `coder`) accumulates context fast and starts paying prompt-cache penalties. The lead should actively recycle teammates at clean task boundaries.

### Monitoring teammate context

There is no native readout of a background teammate's context budget: the old pane-statusline `% of 1000k` reading needed a visible pane, which the background model does not provide. Estimate instead by **counting task boundaries since the teammate spawned**: the heavy worker (usually `coder`) typically crosses ~30% within a milestone or two. Keep spawn prompts self-contained so a recycle costs little, and gate the >30% recycle heuristic below on this count rather than a precise number.

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

3. **Respawn**. Call Agent with the same `name` and `subagent_type` (plus the explicit `model`), and a cold-start prompt that references the checkpoint:

   ```
   Agent({
     name: "<role>",
     subagent_type: "<role>",
     model: "<tier from the role file>",
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
3. **On acceptance, gracefully shutdown the existing team.** Follow the Mode 3 Step 5 cleanup procedure: pre-shutdown checks (`TaskList` ownership + a status question if unsure), `SendMessage shutdown_request` + wait for `shutdown_response approve:true` from each teammate. Wedged teammates (no `SendMessage` in their allowlist) get `TaskStop`. Mid-work teammates mean the prior task wasn't actually done; resolve before transitioning.
4. **Spawn fresh.** With every prior teammate gracefully shut down, their name slots are freed, so respawning by the same names avoids the `-N` suffix collision documented in Step 6 above. There is no team to re-create.
5. **Run the task** per Mode 3 normal flow.

Exception: if the user explicitly wants the existing team kept alive across the transition (the next task is a tight follow-up on the same diff, primed context is directly reusable, no scope drift), skip the shutdown. But this is the exception; the default is recycle.

## Mode 4: reflect

Use when `/agent-team reflect` is invoked, or when the user accepts the end-of-run offer (Step 5 cleanup). Purpose: turn what THIS session revealed about the team into concrete agent improvements — refactors to existing roles, brand-new roles, version bumps — without silently changing anything.

Run it as a dedicated read-only reviewer subagent so the critique is not colored by the lead's own in-session choices. Spawn via `Agent(subagent_type: "researcher")` if that role exists in the repo, else any read-only reviewer/general agent. Give it in the prompt:

- the repo's current `.claude/agents/*.md` (roster, `version:`s, and each `## For this repo` tail),
- the library at `./roles.yaml` (source of truth for generic bodies + current versions),
- a summary of THIS session: what the team did, where a teammate struggled, missing-context surfaces, any mid-run role-file hotfix (Step 6.A), and tasks that had no good owner.

Ask it to return a structured proposal — findings only, no file edits:

1. **Refactors** to existing roles — a concrete body/description/tools change, WHY this session motivated it, and which role `version:` should bump.
2. **New roles** — name, one-line description, and the roster gap it fills (drawn from work the team handled awkwardly). Propose only roles that would RECUR, not one-offs.
3. **Stale roster** — agents whose file `version:` trails `roles.yaml` (the on-load staleness pass, restated with specifics).
4. **Library-worthy vs repo-local** — for each refactor, whether it is repo-specific (belongs in that agent's `## For this repo` tail) or generic (belongs in `roles.yaml`, and should then propagate to every repo + the uzi builtins).

Present the proposal to the user as a numbered list and STOP. Apply nothing without confirmation. On acceptance:

- **Repo-specific change** → edit the agent's `## For this repo` tail only.
- **Generic change** → edit `roles.yaml` (bump that role's `version:`), then run the `update` merge for THIS repo and raise the uzi-builtin-sync proposal (Mode 2). Editing `roles.yaml` is a library change — gated on the user per the Autonomy section.
- **New role** → add to `roles.yaml` at `version: 1` (a library change, gated); or, if it is a one-repo experiment, write it only into this repo's `.claude/agents/` and say so explicitly.

Reflect NEVER runs automatically at session end (no hook exists for that) — it is always an explicit command or an accepted offer.

## Role library reference

The full role library is in `./roles.yaml`. Read it during init/update to see the canonical role descriptions, default tool allowlists, and `triggers_on` patterns. The library may evolve over time; the skill always reads it fresh.

## Gotchas

- **Permission-classifier outage blocks mutating Bash temporarily** (observed 2026-07-15): the auto-mode safety classifier can go briefly unavailable, failing every state-changing Bash call ("temporarily unavailable, so auto mode cannot determine the safety") while read-only ops keep working. Don't spin on retries: interleave the non-Bash steps of your plan meanwhile (protocol shutdown_requests, file edits via Edit/Write, SendMessage coordination), wait ~1 min, then retry the blocked command — it recovered on the second window.

- **Old team API (`TeamCreate` / `team_name` / `TeamDelete`) — superseded by the implicit team.** Mode 3 above is written for the implicit-team API: the session has ONE implicit team, so you spawn with `Agent({name, subagent_type, model, prompt})` (optionally `run_in_background: true`), coordinate via `SendMessage` (target by teammate name) + the `Task*` tools, and retire a teammate with a graceful `shutdown_request` → `shutdown_response approve:true`. There is nothing to create or delete. Confirmed on Claude Code 2.1.178 (2026-06-16) through 2.1.215, where the Agent tool documents `team_name` as "Deprecated; ignored. The session has a single implicit team" and exposes no `TeamCreate`/`TeamDelete`. If you are on an OLDER build that still has them: call `TeamCreate({team_name, agent_type, description})` once before the first spawn, pass that `team_name` on every `Agent` spawn/respawn, and `TeamDelete` at the very end (only after all members confirm shutdown, never with an active member). Everything else in Mode 3 is identical.
- **`/clear` does not clear the team registry.** After a `/clear`, the session's `~/.claude/teams/<session>/config.json` still lists every pre-clear member, and those agents can be alive, idle, and WAKEABLE. Two observed effects (2026-07-03): (a) spawning a teammate with a role name a stale slot holds silently suffixes the new agent (`coder` → `coder-2`); (b) `TaskUpdate({owner: "coder"})` intended for the fresh spawn woke the STALE pre-clear `coder`, which read the on-disk brief and started implementing in the same worktree as the fresh `coder-2` — two writers in one worktree, caught only because both self-reported foreign uncommitted files. Rules: before run-mode spawns, list the session's team members and treat any entry you did not spawn in THIS conversation as a stale-slot hazard; always address teammates AND set task `owner` using the exact live name returned by the spawn result, never the bare role name; if a stale agent wakes, stop its writes first, then graceful-shutdown it.
- **A `teammate_terminated` notice can be FALSE — treat any termination you did not protocol-confirm as unconfirmed** (observed 2026-07-05): the system announced "coder has shut down", the lead verified the worktree clean and respawned the role (suffixed `coder-2`), and the "terminated" coder then kept working and committed a full milestone ~25 min later — two writers in one worktree. Rules: only your own `shutdown_request` answered by `shutdown_approved` proves termination; a worktree-clean check at respawn time is NOT proof (the zombie can commit later); when respawning a role into the same worktree, put a concurrent-writer guard in the spawn prompt ("if foreign uncommitted changes or unexplained commits appear, STOP writing and report") — that guard is what caught it; if the zombie surfaces, protocol-shutdown it and freeze the legitimate writer until `shutdown_approved` arrives; the zombie's COMMITTED work is salvage, not waste — verify it adversarially and adopt it rather than reimplementing. Name-reuse corollary: a fresh spawn that reuses the dead-looking holder's name can receive that name's PENDING stale `shutdown_request` and get killed mid-task — after any same-name respawn, verify the new agent's task actually completed (check the artifact, not the report) before relying on it.
- **Two writers in one worktree — containment and recovery** (observed 2026-07-03, same incident as the `/clear` gotcha; both writers were individually careful and it still took three rounds to contain):
  - A STOP/HOLD message can cross an agent's in-flight turn (mailboxes are read between turns): the woken agent acknowledged a HOLD, then resumed and committed anyway. Treat any stop as UNCONFIRMED until the protocol `shutdown_approved` arrives — and until it does, freeze the LEGITIMATE writer too. One live + one "stopped" writer still interleaved a sanctioned reset+cherry-pick with a fresh commit, leaving the branch mid-conflict.
  - The lead inspects (`git log`/`status`/`reflog`) but NEVER runs state-changing git in the contested worktree — a "helpful" abort makes a third writer. The reflog is the ground truth for reconstructing who did what, and commits reset off the branch remain recoverable by SHA.
  - Before sanctioning any history rewrite in a worktree with two-writer risk, require the worker to back up uncommitted/adopted files OUTSIDE the worktree (scratchpad). That backup is what makes every subsequent surprise recoverable.
  - A stale agent that ignores bare shutdown_requests may comply when you quote the pending `request_id` and the exact protocol JSON to reply with in a plain message; that worked where two bare requests did not.
  - Once termination is protocol-confirmed, hand the surviving writer ONE explicit recovery sequence: the lead-verified current state (HEAD, tree cleanliness, which commits are off-branch but alive), numbered steps, and "report final SHAs to freeze them (no amends after)".
- **Team config is global**, not per-repo. Multiple repos can each have their own `.claude/agents/`, but only one team can be active at a time across the whole Claude Code instance. If a team is already running for a different repo, ask the user to clean it up first.
- **Never delete another session's team state.** `~/.claude/teams/` (and `~/.claude/tasks/`) is shared across ALL Claude Code sessions on the host. A team dir you did NOT create in THIS session may belong to a different, still-running session — do not `rm -rf` it, and do not `TeamDelete` it, to "clear a slate" before spawning. Detecting "orphaned" from your own session is unreliable: you cannot enumerate another session's live teammates from here. Safe to remove unprompted: a team dir with NO `config.json` (empty junk). NOT proven dead by your inability to see its teammates: a populated `config.json` with `isActive: true` members — treat it as possibly-live and ASK the user (it is likely their other session) before touching it. Observed 2026-06-13: a populated PRD team dir was deleted as "orphaned" on flimsy evidence; it actually belonged to another live session, disrupting it.
- **Do NOT tell a teammate to kill a process you have not confirmed is theirs — and a teammate refusing to touch an unowned process is CORRECT behavior, not obstruction.** Distinct from the team-state rule above (that is about `~/.claude/teams/` dirs; this is about OS processes). A background gate (a long e2e run, a build) launched by one agent shows up in `pgrep`/`docker ps` right next to runs from OTHER agents and even other Claude Code sessions on the host. Before asking a worker to `kill` a stray process, verify ownership: match the process's shell-snapshot path (`snapshot-zsh-<ts>.sh`), its redirected log path, and its cwd against the worker's own — a DIFFERENT snapshot timestamp, or a log/cwd the worker never used, means it is NOT theirs. A stray run in a separate compose project (PID-derived name) that self-tears-down is harmless: leave it, or ASK the user, rather than killing what may be another session's work. Observed 2026-07-20: the lead saw a second `run-e2e.sh` running from the main worktree (at pre-feature code) and told the M5 coder to kill it; the coder correctly REFUSED, proving via its distinct shell snapshot + its own log paths (always from its own worktree) that the run was not its own. The lead accepted the correction and left the harmless stray alone.
- **Single-line description in `.claude/agents/<role>.md`**: multi-line YAML (`>-`, `|`) breaks Claude Code's parser.
- **`tools: []` field**: omit entirely if inheriting. Do not write an empty array.
- **Team-coordination tools on every non-empty `tools:` allowlist**: include `SendMessage, TaskUpdate, TaskList, TaskGet`. The role library enforces this; check survives any prompt-body or schema-tuning edits. A teammate spawned without these cannot report findings, claim/complete tasks, or respond to `shutdown_request`, even though its `prompt_body` says "Report via SendMessage". Symptom on first surface: teammate produces its report but cannot send it, and goes idle; lead has to notice and apply the hotfix in Step 6.A.
- **`subagent_type` matches the agent's name**, not its filename. The `name:` frontmatter field is authoritative.
- **Frontmatter `model:` is not reliably honored at spawn — always pass the Agent tool's `model` parameter** (observed 2026-07-05: a `model: sonnet` documenter ran on the parent session's model when spawned without an override). The role file defines the tier; the spawn param enforces it. See the Step 5 model note for tier guidance; never pin the smallest tier (`haiku`) for any role (auto-mode gating — verify the current capable-model list in the Claude Code docs, don't trust a version list).
- **New `.claude/agents/<role>.md` files ARE spawnable mid-session** (observed 2026-07-05: a role added to the repo mid-run spawned via `subagent_type` without restarting the session) — adding a role does not require a new session, only the file.
- **Idle is normal**: teammates go idle after every turn. Do not interpret idle as "done" or "stuck". Only act when a teammate sends a message or completes a task.
- **Tasks vs SendMessage**: use TaskUpdate to mark progress (shared task list); use SendMessage for human-readable communication. Do not send structured JSON status payloads via SendMessage.
- **The shared task list can vanish mid-run** (observed: lead's `TaskUpdate` returned "Task not found" and a teammate saw its task entry disappear, mid-session, with the team still healthy). Treat git state + SendMessage reports as the source of truth; the task list is a coordination convenience. Teammates should report findings via SendMessage directly when their task entry is missing instead of stalling, and the lead should not block any flow step on task-list bookkeeping succeeding.
- **Stale duplicate message re-deliveries** (observed ~5x in one session, 2026-06-12): teammates can receive a re-delivered copy of an earlier dispatch AFTER completing it — sometimes minutes later, sometimes after the task entry has vanished from the store. Correct teammate behavior: recognize it (HEAD unchanged, work already reported), take NO action, and reply that the prior verdict stands. Correct lead behavior: confirm "stale duplicate, no re-dispatch — your verdict stands" and never treat the re-delivery as new scope or respawn-worthy. A side effect to watch: a re-delivered pre-flag list can wake the coder into an UNREQUESTED extra work round — if uncommitted WIP appears with no dispatch behind it, check its worktree/task state, then require the standard finish-the-loop (gate, commit, report tip SHA) rather than aborting it. The INVERSE crossing is equally routine (observed ~4x in one session, 2026-07-05): a lead's "you still owe X" nudge crosses the worker's completion report in flight. Correct worker behavior: re-verify the LIVE state (`git rev-parse HEAD`, `git log`, the artifact itself) and reply with that evidence — never redo or re-commit already-landed work. Correct lead behavior: check the worktree/artifact immediately before nudging, and on discovering the crossing, ack "in sync, nothing owed" so the worker doesn't reconcile further. Prevention (observed 3x in one run, 2026-07-05): new requirements dispatched while a worker is MID-TURN frequently cross and go unactioned — prefer queueing new items until the worker's next report, and after any mid-turn dispatch verify the item actually appears in that report (or on disk) before proceeding; if it crossed twice, re-send it as a standalone single-item task while the worker has nothing else in flight. Expect this at EVERY milestone handoff, not occasionally: the next-task dispatch sent right after receiving a completion report crossed the worker's post-report idle transition on all four handoffs in one run (2026-07-13) — the standalone re-send while the worker sat idle recovered each one first try, so treat crossing+re-send as the normal handoff cost, not an anomaly to diagnose. A specific, avoidable re-delivery trigger (observed 3x in one run, 2026-07-10): the lead's own `TaskUpdate` bookkeeping (setting owner/status on a task) WAKES the named idle agent, which reads the assignment as new scope and re-reports already-completed work — do task-list bookkeeping BEFORE the SendMessage dispatch (or accept skipping it), and answer the resulting "already done" reply with the standard "in sync, nothing owed" ack. **Confirmed again 2026-07-16 (5x in ONE milestone), and the recovery held every time**: the standalone re-send to an idle worker recovered each one first try, and in all five the worker correctly checked live state and reported "already done" rather than redoing the work. Treat it as the normal cost of a fast review loop, not a defect to chase. The lead-side cheap fix is the one already stated — **verify the artifact immediately before dispatching, not before composing**: three of the five were the lead verifying at SHA `N`, writing a long dispatch, and sending it after the worker had reached `N+1`. Re-check the tip as the last act before send.
- **A teammate's `SendMessage({to: "main"})` report can bounce — you get only its idle notification, not the body.** Observed repeatedly (2026-06-16, across multiple background review agents): a background teammate's report addressed to `main` is silently dropped/rejected, so the lead receives only the `idle_notification` (which carries a short summary preview), NOT the findings body. Do NOT act on the summary preview as if it were the report. SendMessage the teammate (it is idle and resumable by name) asking it to RE-SEND its full findings to `main`; the resend usually arrives. In the spawn prompt, tell teammates to fall back to replying directly to the team-lead if `to: main` bounces (they often self-diagnose it: "the `to: main` route is rejected for me, routing through you").
- **Message timestamps are UTC; teammate-quoted wall-clock times are LOCAL.** Teammate/system JSON messages carry UTC timestamps, while human-facing times a teammate relays (e.g. a rate-limit notice "resets 4:10pm (Europe/Bucharest)") are in the user's local timezone. Never derive "time until X" by comparing a quoted local time against message timestamps — run `date` for the actual local clock before scheduling any wait (observed 2026-07-05: a 2.5h wait timer was set for a reset that had already passed).
- **Session-limit teammate failure is recoverable — do not respawn.** A teammate dying with idleReason `failed` + "You've hit your session limit" is the account-wide usage cap, not a crash. The same agent resumes by name via a plain SendMessage once the limit lifts (respawning costs the `-N` suffix and the accumulated context). Meanwhile other agents and fresh spawns may still work — keep read-only validation and other pipeline stages moving instead of blocking the whole run. The failure message names the reset time — note it. If the dead worker sat on the critical path, the lead may take over MECHANICAL steps only (a plain merge, a file rename, running gates, a push) to keep landing on schedule; anything semantic (conflict resolutions with judgment, test-fixture reconciliation, code fixes) waits for the reset and goes back to the worker — and whatever the lead DID resolve by hand gets a sanity review from the resumed worker before the next gate (observed 2026-07-05: coder died mid-landing; lead merged main + renumbered a migration, then handed the two semantic test breaks back to the reset coder with "review my three conflict resolutions" — clean). The 401 variant behaves the same: idleReason `failed` + "401 Invalid authentication credentials" (expired/limited OAuth token) is an auth outage, not a crash — before assuming lost work, check the worktree: the final dispatch may be fully committed (observed 2026-07-15: coder completed its renumber + stack teardown, committed, THEN died on 401; the lead only had to do the mechanical push + MR).
- **Teammate-environment-only failures**: a teammate may hit build/tool failures specific to its sandboxed session (observed: bare `go build` failing with buildvcs "exit status 128" in every worktree because the go toolchain's git subprocess was blocked — while the same command succeeded in the lead's shell). Before accepting workaround changes to shared build files, reproduce the failure in the lead's shell — in the SAME worktree the teammate used; if it doesn't reproduce, it's the teammate's environment — have them use a local env workaround (e.g. `GOFLAGS=-buildvcs=false`) and keep it out of the tree. Nuance (2026-07-15): the same buildvcs failure later reproduced in the lead's own shell inside a linked worktree — the cause can be repo LAYOUT (the worktree's `.git` pointer file trips go's VCS stamping), not the sandbox at all. Either way the resolution is identical: local-only flag, never committed; just don't mis-log it as a sandbox quirk when it's a worktree one.

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
Claude: [reads .claude/agent-team.md, spawns coder with the task,
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
