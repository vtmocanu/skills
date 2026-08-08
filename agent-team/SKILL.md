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

Every generated `.claude/agents/<role>.md` carries a frontmatter `version:` copied from that role's `version:` in `./roles.yaml`. Whenever this skill loads in a repo that already has `.claude/agents/`, do a quick staleness pass before other work: for each agent file, read its `version:` and compare to the current role `version:` in `roles.yaml`. **Compare the BODY too, not only the number** — diff each file's generic body (everything above `## For this repo`) against that role's `prompt_body`. Surface the result in one line, e.g.:

> 3 of 6 agents are behind the library — coder (v1→v2), tester (v1→v3), documenter (missing version → treat as v0). Run `/agent-team update` to refresh.

A file with no `version:` field predates versioning; treat it as v0 (stale). This pass only INFORMS — it never auto-edits. The actual merge happens in `update` mode, which preserves each file's `## For this repo` tail. Skip the pass silently when every agent is current.

**Run it, do not re-derive it by hand: [`./scripts/sync.py`](scripts/sync.py) ships with this skill.**

**The two paths are anchored differently, so spell the script one out.** `./scripts/sync.py` is relative to THIS FILE's directory; `--agents` defaults to `.claude/agents` relative to your CWD, which is the consumer repo. So run it from the repo being checked, giving the script's own absolute path — the directory this SKILL.md was loaded from:

```sh
python3 "<this skill's directory>/scripts/sync.py" check
python3 "<this skill's directory>/scripts/sync.py" diff <role>
```

A bare `python3 ./scripts/sync.py check` cannot work from either location: from the consumer repo there is no `scripts/` there, and from the skill directory `--agents` then resolves against the skill directory too.

`check` classifies each file as `ok` / `STALE` / `MODIFIED` (equal version, content differs — see axis 3 in Mode 2) / `CUSTOM` (no library entry) / `LEGACY` (a STALE file with body lines the library lacks — `apply` replaces the body, so they go, and a backup is written) / `BAD-FM` (frontmatter that Claude Code's loader tolerates and a stricter parser rejects) / `ERROR` (unreadable). It compares version, generic body, `description`, `tools` and `model` — with two exemptions worth knowing, because both are cases where a green covers less than it looks: a `CUSTOM` file is not compared at all, and a `BAD-FM` file has its `description`/`tools`/`model` skipped, since those values could not be read. The script says so in the row rather than leaving you to infer it.

Every status predicts what `apply` does to that file: `BAD-FM` and `MODIFIED` are refusals, `LEGACY` and `STALE` apply. It exits **0** when clean, **1** when anything drifted, **2** when the instrument itself failed — an unreadable library is not a finding about the repo, and a caller that reads it as one goes hunting for drift that was never measured. `check` and `diff` write nothing.

The script exists because this pass is prescribed as mandatory and had no tooling, so every session either re-derived an 11-file body comparison by hand or wrote a throwaway to do it — which is the shape Mode 4 calls a workflow defect: a rule that binds only when someone remembers to do it manually.

**🔴 A VERSION-ONLY CHECK REPORTS ALL-CLEAR ON A TREE WHERE MOST BODIES HAVE DRIFTED, and the mechanism is this file's own bump rule.** `roles.yaml` can be edited without a version bump — the header explicitly allows one bump per release, not one per edit — so any body change that ships without an increment is invisible to a version-keyed comparison **by construction**. Measured 2026-08-03 in one repo: all 11 files' `version:` matched the library exactly, and **9 of 11 generic bodies differed anyway**, every one carrying superseded guidance about where teammates send their reports. The two that matched byte-for-byte were the two a recent explicit re-sync had touched. That is why the body diff is mandatory rather than a nicety: the number agreeing is not evidence the body agrees.

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
- **Editing the role library** at `./roles.yaml`, **or editing this skill's own `agent-team/SKILL.md` workflow**, or running `/agent-team update`. Both files are the shared library: a change propagates to every repo that uses the skill, not just this one.

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

  **Record the CHECK-mode variant, never the fixing one.** Many of these tools have both: `prettier --check` vs `prettier --write`, `gofmt -l` vs `gofmt -w`, `task fmt-check` vs `task fmt`, `eslint` vs `eslint --fix`. The tester runs these commands against a worktree the coder is still working in, so a fixing variant silently rewrites someone else's files mid-run. Where only a fixing variant exists — `pre-commit run -a` rewrites by design, since its hooks include formatters — record it with an explicit `(rewrites files)` suffix so the tester knows not to run it. **When the slot resolves to a task-runner target or an npm script, open its definition and read what it actually runs** — the preference for named targets over raw binaries is what hides the fixing flag, so a `lint` target wrapping `golangci-lint run --fix` looks safe from the outside.

  | Slot | Config signals | Typical command |
  |---|---|---|
  | format | `.editorconfig`, `.prettierrc*`, `rustfmt.toml`, `.clang-format`, gofmt (implicit for Go) | `task fmt-check`, `prettier --check .`, `gofmt -l .`, `cargo fmt --check` (check-mode only — never `--write`/`-w`) |
  | lint | `.golangci.y*ml`, `eslint.config.*`, `.eslintrc*`, `biome.json`, `.oxlintrc*`, `ruff.toml`, `.flake8`, `.rubocop.yml`, `clippy.toml` | `task lint`, `golangci-lint run`, `npm run lint`, `ruff check`, `cargo clippy` |
  | typecheck | `tsconfig.json`, `mypy.ini`, `pyrightconfig.json` | `tsc --noEmit`, `mypy .` |
  | test | test dirs/manifests from the rows above | `task test`, `go test ./...`, `pytest`, `npm test` |
  | dead code | `knip.json`, `.ts-prunerc`, `deadcode`/`unused`/`unparam` in a golangci config, `vulture` config | `knip`, `deadcode -test ./...`, `vulture .` |
  | coverage | `codecov.yml`, `.coveragerc`, a `-coverprofile`/`--coverage` flag anywhere in CI or task targets | `task test-coverage`, `go test -coverprofile=…`, `vitest --coverage` |
  | security scan | `.gitleaks.toml`, `.semgrep.yml`, `.trivyignore`, gosec/bandit/govulncheck/`npm audit`/`cargo audit` invocations in CI | `gitleaks detect`, `govulncheck ./...`, `npm audit` |
  | pre-commit | `.pre-commit-config.yaml`, `lefthook.y*ml`, `.husky/` | `pre-commit run -a (rewrites files)`, `lefthook run pre-commit (rewrites files)` |

  **Mine the CI config for these, not just the repo root.** A repo can lint in CI with no config file at the root, and a repo can carry a `.golangci.yml` that nothing ever invokes. The CI job definitions are the evidence of what actually runs.

  **Record a slot with no check as the literal `none (gap)`, never omit the line.** An omitted slot reads as "not investigated"; `none (gap)` is what lets the tester and auditor say "this repo has no linter" instead of silently skipping it. In a monorepo, record slots per component (`lint (api)`, `lint (web)`) — one flat gate that forces a four-toolchain run for a one-line change is a gate that stops being run.

  **Every recorded slot carries an ENVIRONMENT, read off the CI job that runs it: the image or runtime version, and what is ABSENT there.** `test — npm test — CI: node:24 alpine, no docker daemon, no network` is a slot; `test — npm test` is half of one. Take it from the CI job definition (the `image:`/`container:`/`runs-on` key plus its services), not from your own shell, and where a slot runs only locally say `local only — not in CI`, which is itself a finding.

  A gate command is not portable by default, and the environment is where the two runs differ. A suite that skips on a missing binary, a daemon, a network route or a runtime version passes identically in both places while covering different things — and a suite-level skip never registers its inner tests at all, so the count does not drop, it simply becomes a different count. Measured 2026-08-04: **86 tests were never registered in CI** while every local run showed them green. Recording the environment is what makes the tester's name-set diff (`roles.yaml`, the tester body) possible; without it there is nothing to diff against.

  **If the repo has no task runner and two or more slots need more than a single bare command, PROPOSE writing one — and ask before creating it.** "More than a single bare command" means anything a person cannot paste as-is from the slot line: a `cd` into a subdirectory first, two commands chained, an env var prefix, a flag set nobody remembers. `pytest` is one bare command; `cd api && go test ./... && go vet ./...` is not. Without a runner, each slot's raw recipe gets copied into every role tail, the workflow doc, and CLAUDE.md, and then drifts independently in each. A task runner collapses that to one name per slot. Same proposal when a runner exists but does not cover every populated slot (a repo with `npm test` whose lint lives only in a CI job): offer to add the missing targets. Never create or restructure a build file silently — this is the same consent gate the documenter uses before restructuring a README.

  Raise it as part of the Step 4 proposal, not as a separate mid-probe interruption — it is one more numbered item the user accepts or declines alongside the roster. **If they decline, record the raw recipes in the slots as normal, add a `task runner: declined <date>` line to the Quality gates block, and do not raise it again** while that line stands; the team still works, the commands are just longer and duplicated. Without the recorded line every future `update` re-runs discovery and re-proposes the same runner. Match the ecosystem's convention when picking the runner (`Taskfile.yml` where the org already uses Task, `justfile`, `Makefile`, or `package.json#scripts` in a Node-only repo) rather than importing a preference.

  When you do write one, write it **repo-local and self-contained**: every target defined inline in the repo's own file, no imports of shared or remote task libraries. Some projects do use shared libraries; do not introduce one unless the user asks. Targets invoke the tools directly (`golangci-lint run`, `shellcheck …`) and stay indifferent to what puts them on `PATH`, so the same target works in a local dev shell and in CI. Where the repo's CI already runs those commands, note that it can call the same targets — one definition, two callers.
- **Reproducible-env manifests**: `devbox.json`, `flake.nix`, `shell.nix`, `.nvmrc`, `pyproject.toml` (poetry), `environment.yml`, `.tool-versions`, `.envrc`. The first hit drives `init_command` references in the team workflow doc.
- **Agent launchers**: scripts/aliases that launch a Claude/opencode/etc. session. Look in `scripts/`, `Taskfile.yml`, `Makefile`, `package.json#scripts`, devbox script blocks. Record the FULL invocation form (`devbox run agent-big`, `task agent`, `make agent`), not the bare script name.
- **Project slash commands** the orchestrator can invoke between delegations: `.claude/commands/`, `.claude/skills/`. List them; the lead will reference them in the workflow doc.
- **Spec directories**: `prds/`, `specs/`, `rfcs/`, `proposals/`, `docs/adr/`, `docs/design/`.
- **CI configs**: `.github/workflows/`, `.forgejo/workflows/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.circleci/`.
- **Release signals**: `docs/releasing.md`, `RELEASING.md`, `.goreleaser.*`, `CHANGELOG.md`, `semantic-release.json`, `.releaserc*`, a release workflow in CI.
- **CLAUDE.md and CONTRIBUTING.md**: read both (top-level and any nested). They contain authoring rules workers must follow.

  **MEASURE the root `CLAUDE.md` and report the number, because every agent pays it.** A project `CLAUDE.md` is loaded in full at the start of every session, and a subagent is its own session: the sub-agents docs list `CLAUDE.md` among what loads at startup, so the file is paid **per teammate spawned** rather than per task, and it occupies that share of every teammate's context window regardless of what it costs. Claude Code's own guidance is **under 200 lines per file**; use that bar, because it is the documented one. (Two documented exceptions, and they cut in your favour: `Explore` and `Plan` skip `CLAUDE.md` entirely, so a delegated search pays none of this. Every other built-in and custom subagent loads it.)

  When the root file is well past that, **propose a split as part of the Step 4 proposal**, one more numbered item the user accepts or declines exactly like the task-runner proposal above. The root keeps what is genuinely repo-wide (identity, gate names, architecture, conventions, cross-cutting traps) **plus anything whose failure is irreversible**, for the reason given below; per-component detail moves into `.claude/rules/*.md` carrying `paths:` frontmatter, which Claude Code includes only when it reads a file matching those globs. A nested `<subdir>/CLAUDE.md` does the same job at directory granularity; prefer rules when the natural scope is not one directory (a migrations dir, a file type, a config file).

  Measured 2026-08-04 on a four-toolchain repo: a 578-line / 124,514-byte root file (about 31k tokens) became 186 lines / 40,453 bytes, so an eight-agent wave stopped carrying about 170k tokens of preamble at startup. Report the outcome against both bars and expect them to disagree: 186 lines passes the documented line guidance while 40,453 bytes is still a large file. Lines is the stated bar; bytes are informational, and quoting a byte threshold as though it were documented invents a number.

  **Move the content byte-exact and PROVE it** with a reassembly diff against the original, not by reading the result: extract by line range, concatenate in original order, diff. **But fidelity is the easy half, and passing it proves less than it looks.** Byte-exact extraction preserves every sentence and silently falsifies the ones describing POSITION: every "below", "above", "this file", and every count of the form "stated in four places" is a claim about a layout that no longer exists. Budget one pass for those, and a second for inbound references from files OUTSIDE the split (other docs, role files, workflow manifests) that name a section by a heading the split deleted. Measured on that same repo: zero content lost, and **26 sentences made false**, 14 of them in one workflow document nobody thought to sweep. Also re-measure every figure you quote AFTER the last edit, not before it.

  **Two properties belong in the root file itself, because they fail silently.** A path-scoped rule fires on a file **read**, so running a gate without opening a file in that component does not pull its rule in, and the rule therefore arrives AFTER the decision to act. That is precisely why a rule whose failure is irreversible (destroying data, breaking a shared machine) belongs in the root rather than behind a trigger: the agent about to do the damage is usually not reading a file that would summon the warning. Separately, and stated narrowly because the docs do: nested **`CLAUDE.md` files** are not re-injected after `/compact` while the root file is. The docs say nothing either way about `.claude/rules/`, so do not rely on rules surviving a compaction, and do not write the stronger claim into a consumer's repo.

- **Dead weight, and files too large to read**: directories imported by nothing (archived evidence, vendored corpora, completed-work archives) and single files big enough to crowd a teammate out. Record both in the workflow doc, so sweeps skip the first and nobody opens the second. **Use an executable threshold, not a judgement**: flag any single file above roughly 10% of the SMALLEST context window on the team, and record its size beside its path together with the scoped alternative (`git grep -n`, an explicit line range). For scale, a 1.3MB spec file is about 337k tokens, which is a third of a 1M window and more than a 200k window holds at all, so whether it ends a teammate depends on which model that teammate runs.

Cite the actual files/directories you found in the proposal you present to the user. Do not pad with generic boilerplate.

### Step 2: pick roles

Load the role library at `./roles.yaml` (relative to this SKILL.md). For each role:

- **coder, reviewer, auditor**: always include. These are the default trio.
- **tester**: include if any of the role's `triggers_on` patterns match a path in the repo (`tests/`, `test/`, `*_test.go`, `pytest.ini`, etc.), OR if any gate slot other than `test` is populated. A repo with a linter and no test suite still needs someone other than the coder to run that linter — otherwise the gate is back to one self-reporting owner and no verifier, which is the case this whole mechanism exists to fix.
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

- **coder**: give the gate slots it must run before reporting done — the same check-mode commands recorded for the tester (format, lint, typecheck, test at minimum; leave the security-scan slot to the auditor), not just the test command. The v2 coder body says "every slot named in your tail", so a tail naming only `pytest` silently drops format and lint back to having no owner. Reference CONTRIBUTING.md / CLAUDE.md by path, and the spec directory if found.
- **reviewer**: cite the authoring-rules file (CONTRIBUTING.md / CLAUDE.md) by exact path and quote one or two of its load-bearing rules if obvious. Name the dead-code command from the gate slots if the repo has one, so the deletion lens in the generic body has something to run.
- **auditor**: note if the repo is public (security implications differ); give the security-scan slot verbatim — the command if one exists, or `none (gap)` — since the generic body now tells the auditor to run it rather than merely name it.
- **tester**: **paste the gate-slot table for this repo**, one line per slot, each with the exact command including its working directory (`cd api && go test ./...`) or the literal `none (gap)`. Preserve each command's check-mode form and any `(rewrites files)` suffix verbatim — the tester decides what is safe to run from these strings. A framework name alone is not enough: the generic body tells the tester to run the populated slots scoped to what the change touched, so a tail that says "vitest" gives it nothing to invoke. This is the one role whose tail may exceed the 1-3 sentence cap below — a monorepo with four toolchains needs four sets of slots, and truncating them is what leaves checks unrun. Also record here any command whose runtime exceeds the generic 5-minute live-wait bound, with its real bound (e.g. "`./e2e/run-e2e.sh` takes ~30min; when a change warrants it, let it finish"). The tail is authoritative for COMMANDS; `noted` markers live only in the workflow doc's Quality gates block, so do not copy them here.
- **documenter**: name the doc site generator (mkdocs, hugo, docusaurus) or "plain markdown" if none. The documenter also owns the repo's `CHANGELOG.md` and keeps it TERSE: one concise line per change under an `[Unreleased]` section (Keep a Changelog style), not paragraphs. Every feature/fix/behaviour change the team ships gets a one-line entry there; the releaser later folds `[Unreleased]` into the cut version (see Mode 3 Step 3). If the repo has no CHANGELOG yet, the documenter creates one. The documenter also carries the README/docs house style (terse README as a launchpad, reference detail in a `docs/` folder); when the repo's README diverges (a large monolithic README, or no `docs/` — the same signal as the >500-line-README include trigger in Step 2), it PROPOSES a migration and asks the user before restructuring, never doing it silently. For repos with non-trivial architecture (multiple components/services, cross-cutting data flows, trust boundaries) it also keeps an `ARCHITECTURE.md` at the root, creating one when it helps a new reader and skipping it for small/simple repos where the README conveys the shape.
- **release**: name the release flow doc by path (`docs/releasing.md`, etc.) and the release command (`semantic-release`, `goreleaser`, manual tag-push).
- **architect**: name the design-doc/ADR directory and its numbering/format convention if one exists (or note there is none, so the role proposes before creating one); list the repo's major components so designs map onto them.
- **researcher**: name the spec directory.
- **spec-keeper**: name the spec directory path if it differs from `specs/`; note any existing spec files to adopt instead of creating fresh ones.
- **fact-checker**: name the claim-bearing surfaces in this repo (docs dir, README, CHANGELOG, published specs) and any authoritative sources to check against (the code itself, CI status, official upstream docs).
- **web-ux**: name how to reach a running instance of the UI (dev-server command, compose service + port, demo/mock build) and the design-token/style-system files if the repo has them; note any repo-specific UX contract (design system, a11y bar, target browsers).

Keep the tail tight: 1-3 sentences per role, the tester's gate-slot table excepted. The generic body already covers the shape; the `## For this repo` tail only carries what is specific to THIS repo (exact commands, file paths, framework names, how to reach the app). Keeping repo-specifics in the tail — never spliced into the generic body — is what lets `update`/library-sync replace the versioned generic body later without clobbering local tuning.

### Step 4: present a proposal

Show the user a numbered proposal:

```
Proposed team for <repo-name>:

1. coder (sonnet) - implements features, fixes bugs. Will run `<test-command>`
   before reporting done. Will follow rules in <CONTRIBUTING.md path>.
2. reviewer (sonnet) - reviews against <CONTRIBUTING.md rules>. Read-only.
3. auditor (sonnet) - security audit. Notes: <public/private repo>, secret
   scanner: <name>. Read-only.
4. tester (sonnet) - runs the gate slots (<slots>) + <test framework>. (included because <reason>)
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

**`version:` frontmatter** stamps which `roles.yaml` version this file was generated from. Claude Code tolerates the custom key (verified on 2.1.215: an agent with `version:` in frontmatter loads and spawns normally) — it is not in the documented key set but is silently ignored by the loader. Copy the integer straight from the role's `version:` in `roles.yaml`. This is the field `update` mode diffs to find stale agents. (A downstream builtin parser is stricter and rejects unknown keys, so versioning those builtins needs a separate change tracked in that project; it does not affect these Claude Code files.)

**`## For this repo` tail** holds ALL repo-specific tuning (Step 3), kept out of the generic body so `update`/sync can replace the generic body by version without touching local edits. Everything above the tail must be the verbatim `roles.yaml` body for that version.

**Tool allowlist format**: Claude Code subagent frontmatter accepts `tools:` as a comma-separated list. If `roles.yaml` says `tools: []` (empty array, meaning inherit all), OMIT the `tools` line entirely from the frontmatter. Do not write `tools: []`.

**Team-coordination tools (REQUIRED on any non-empty allowlist)**: every role with a `tools:` line MUST include `SendMessage, TaskUpdate, TaskList, TaskGet`. Without these, the spawned agent produces its report but cannot transmit it to team-lead, claim/complete tasks, or respond to `shutdown_request`. The role library at `roles.yaml` already includes them on every non-empty allowlist; verify they survived any prompt-body or schema-tuning edits you make in Step 3.

**Frontmatter MUST be single-line for `description`** (multi-line YAML block scalars break Claude Code's parser), **and QUOTE it when the value contains `: `**. A bare colon-space inside an unquoted scalar is illegal YAML — PyYAML answers `mapping values are not allowed here` and refuses the whole frontmatter. `roles.yaml` quotes the `tester` description for exactly this reason (its text reads *"…the repo actually has: unit-test framework…"*), and the quoting has been lost at generation at least once: the shipped `tester.md` in a real repo carried an unquoted description that no YAML parser accepts. Claude Code's loader tolerates it, which is why it survives unnoticed — but a stricter downstream parser does not, so the copy that looks fine is the one that hides the defect.

**Model tiers (define in frontmatter, honor them at spawn)**: the role file's `model:` is where the roster DEFINES each role's tier. How the lead honors it at spawn depends on whether it is an ALIAS or an EXACT model ID, and the two need OPPOSITE handling — measured 2026-08-08 on Claude Code 2.1.226 by reading the exact model ID each spawned agent's own harness declared:

- **Alias tier** (`sonnet` / `opus` / `haiku`): pass it explicitly via the Agent tool's `model` parameter on every spawn and respawn. The param's enum is alias-only (`sonnet | opus | haiku | fable`), so an alias is expressible, and passing it enforces the tier regardless of any frontmatter-honoring quirk.
- **Exact model ID pin** (e.g. `claude-opus-4-8`): the param's enum CANNOT carry a full ID, so the instruction "pass it explicitly via the `model` param" is *impossible to follow* here — and translating it to the nearest alias is LOSSY. `opus` resolves to the *session's current* opus generation (measured: `claude-opus-4-8` on a 4.8 session; it would be Opus 5 on an Opus-5 session), which defeats the entire point of pinning a generation. So for an exact-ID pin, **OMIT the `model` param**: an omitted param honors the agent-definition frontmatter (measured: frontmatter `claude-opus-4-8` ran `claude-opus-4-8[1m]`; frontmatter `sonnet` ran `claude-sonnet-5`; a frontmatter-LESS agent fell through to a default, `claude-opus-5[1m]`, honoring nothing). Omitting is the ONLY mechanism that enforces an exact generation. Because this honoring flipped across builds (see the Gotchas note), **verify it when the model matters** — spawn a probe, or ask a real teammate to report the exact model ID from its own context; that harness-declared line is the only ground truth, never the model's own guess.

Tier guidance (alias names, deliberately not version-pinned — model families rotate; the Claude 5 family shipped after this guidance was first written): reasoning-heavy roles (coder, reviewer, auditor, tester, architect, researcher, spec-keeper, fact-checker, web-ux) get the strong tier (`opus`); mechanical roles (documenter, release) get the mid tier (`sonnet`); never pin the smallest tier (`haiku`) for any role. Rationale for the haiku ban: auto-mode (the bias-to-act permission mode that lets teammates complete shared-system writes like a release push without a manual confirmation round-trip) is gated to specific models — the smallest tier has been excluded; **verify the current auto-mode-capable list in the Claude Code docs** rather than trusting a version list here. The tester is strong-tier because testing in most repos here is adversarial/scenario validation (crafting fixtures to break a guard, reasoning about evasion vectors, probing runtime contracts); keep tester on `sonnet` only when the repo's testing is a purely mechanical unit-test-suite run. Note (per the docs): a subagent's frontmatter `permissionMode` is ignored; its actions are classified under the parent session's rules, but only if the subagent's model is itself auto-mode-capable, which is why the model choice (not a mode flag) is the lever here.

Write `.claude/agent-team.md` as the workflow manifest. **The full template is the sibling file [`manifest-template.md`](./manifest-template.md) — copy it verbatim and fill in the values discovered in Step 1.** It ships in the generated skill directory alongside `roles.yaml`.

Its sections, so you know what you are filling in without opening it:

- **Team roster** — one row per picked role: role, subagent type, model, tools.
- **Orchestrator workflow** — the default flow for a typical task in this repo.
- **Context handoff** — why a spawn prompt is the teammate's entire world.
- **Re-derive the claim at the moment you assert it** — the evidence discipline, with its worked failures.
- **Sweep per FACT after the last behavioural commit**, **two negative results from instruments that share an assumption**, **an assertion defines its CHANNEL**, **mutate at the CALL SITE**, **TYPECHECK the mutated tree** — the mutation-testing and claim-checking sections.
- **Quality gates** — one line per slot, the block the lead pastes into every validator dispatch.
- **Project signals** — release flow, spec dir, authoring rules, CI, slash commands.

**Fill Quality gates and Project signals with cited values, never the word "discovered".** Those two blocks are the only ones whose content is repo-specific; everything else is copied as-is.

After writing, suggest the user commit `.claude/agents/` and `.claude/agent-team.md` to the repo so the team definition is reproducible.

## Mode 2: update

Use when `/agent-team update` is invoked, or when an existing team feels out of date.

1. Read existing `.claude/agents/*.md` files (each file's frontmatter `version:` and its `## For this repo` tail) and `.claude/agent-team.md`.
2. Re-run discovery (step 1 of init).
3. Diff along three axes:
   - **Roster**: roles in the library that match repo signals but are missing from `.claude/agents/`, OR roles present whose `triggers_on` no longer match (tests removed, release flow gone, etc.).
   - **Version staleness**: for each present role, compare its file `version:` to that role's current `version:` in `roles.yaml`. A lower or missing number means the generic body drifted behind the library — read both bodies so you can summarize WHAT changed, not just the number.
   - **Local modification at EQUAL version** — a distinct category from staleness, and invisible to a version comparison. Compare the file's generic body, `description`, `tools` and `model` against `roles.yaml` even when the numbers match, and report any difference as **MODIFIED LOCALLY** rather than folding it into "stale". Two real cases motivate this: a repo whose local `description` is BETTER than the library's (an improvement that nothing propagates back, and that the next sync silently reverts), and a body that is a hand-reflowed copy rather than a synced one. Local modification is not always wrong — but it must be visible, or the next version bump destroys it without anyone deciding to. Also report roles present in `.claude/agents/` that have **no `roles.yaml` entry at all** as **CUSTOM, NOT IN LIBRARY** — those are the repo's own and an update must never touch them. A repo that pins an EXACT model ID (`model: claude-opus-4-8`) where the library carries an alias (`opus`) is a specific, expected instance of `MODIFIED LOCALLY`: it reports a `model`-field difference forever, and its backport verdict is almost always `keep local` — the repo wants a stable generation while the library deliberately floats with an alias (Step 5), so this is a chosen divergence, not drift to reconcile.
   - **Tuning drift**: repo facts in a `## For this repo` tail that no longer hold (renamed test command, moved spec dir). Gate slots drift the same way and are worth re-deriving: a repo can gain a linter, a dead-code check, or a task runner after the team was generated, and a tail still naming the old raw recipe sends the tester at a command that no longer matches CI.
4. Present the diff to the user as a numbered proposal: additions / removals / version bumps (one line of "what changed" each) / tail fixes.

   **Every `MODIFIED LOCALLY` row must carry a BACKPORT VERDICT — `keep local` or `propose for roles.yaml` — with a one-line reason.** Not a new step: it is one more column on a diff this mode already computes and already presents.

   **Without it the flow is one-way by construction.** Library → repo is fully specified (bump the version, replace the generic body, preserve the tail). Repo → library has no mechanism at all, so a repo-local improvement is *protected* from the next sync and never *offered* upstream. Axis 3 above already names the exact case — "a repo whose local `description` is BETTER than the library's (an improvement that nothing propagates back)" — and then only prescribes making it visible. Visible is not the same as routed.

   Do not wait for a reflect pass to catch these. Mode 4 item 4 classifies library-worthy vs repo-local only for refactors **that session proposed**; it never sweeps content that is already sitting in a repo. So a better-than-library body written six months ago is invisible to both modes today.

   The verdict is a proposal like any other roles.yaml edit — gated on the user per Autonomy, and carrying the downstream-vendored-copy obligation below if accepted.

   Measured 2026-08-03 (a downstream project): the repo's vendored copy of these role bodies had been fixed for a defect the library still carries — subagent bodies naming an unreachable recipient, measured at 8 of 26 messages failing in real runs. The fix existed downstream for hours while `roles.yaml` shipped the defect to every other repo, because nothing in this mode asks "should this go back?".

5. On confirmation, apply targeted edits:
   - **Version bump**: `apply <role> [<role> ...]` replaces the generic body (everything ABOVE the `## For this repo` heading) with the current `roles.yaml` body, rewrites the frontmatter `version:`, and leaves the tail untouched. This is exactly why Step 3 keeps repo-specifics in the tail — the generic part is replaceable wholesale. The script re-reads each file after writing and aborts if the tail moved, comparing BYTES: "the tail was preserved" is the one claim worth checking against the file rather than against the string it just built, and a check that compares two newline-normalized strings cannot see a CRLF file being silently rewritten.
   - **A body that differs at EQUAL version** is the axis-3 case, not staleness, and `apply` REFUSES it: the difference is unexplained, so overwriting it destroys a local change nobody decided to discard. Read it, give it the backport verdict this step already requires, then re-run with `--force` if the verdict is `keep library`. This is the only content guard, and it is the one that fires on the signal that actually means "a human edited this".
   - **Lines in the generic body that the library lacks are DROPPED, with a warning naming the count.** Not refused — and the reason is worth knowing before you decide the warning is too weak. The measure cannot tell hand-written content from previous-release library text: a line the library REWORDED, and a line the library DELETED, both sit in an older copy looking exactly like something a human added. Refusing on it was measured at 17 of 51 real library edits in this repo's history and **11 of 11 roles on one real bump** — including the very release this skill cites as motivating the body diff. A guard that fires on the primary use case is one an operator learns to force past by reflex, and the reflex outlives the case it was protecting.
   - **Read the warning, and diff against the BACKUP — not against git.** Whenever lines are dropped, `apply` writes `<role>.md.pre-sync` beside the file, names it on stdout, and exits **3** rather than 0. All three matter: with the roster committed and the hand-tuning added since, `git diff` shows exactly one deletion — the version line — which is the signature the drop-free run calls the all-clear, so the natural check returns the reassuring answer. Measured unrecoverable from git in 2 of 3 ordinary repo states, one of them "the roster was generated by `init` and not yet committed", which every consumer passes through. `check` marks such a file `LEGACY` and says how many lines go; `sync.py diff <role>` lists them. If any is genuinely repo-specific it belongs in the `## For this repo` tail. The tail itself is never at risk; this is only ever about content someone put in the library-owned half.
   - **`.pre-sync` files are untracked plaintext copies inside a directory consumers commit**, so add `*.md.pre-sync` to the repo's `.gitignore` (or delete them once checked) rather than letting a `git add -A` sweep the recovery artifact into the tree.
   - **Legacy files with no tail split** (older generated agents): where the boundary is not mechanical, do NOT blind-overwrite. Read the file, separate library-origin paragraphs from manual ones, and migrate the manual repo-specifics into a new `## For this repo` tail so the NEXT update is clean. When unsure which is which, quote the paragraph and ask.
   - **Roster add/remove**: write or delete the role file as in init Step 5. Neither `check` nor `apply` does this — whether a role belongs is a roster decision. The script reports the gap in both directions: library roles with no file here, and files here with no library entry (`CUSTOM`, which it never touches).

**Downstream vendored copies (only when you EDIT `roles.yaml` itself, NOT on a normal repo `update`).** If a downstream app vendors these role bodies as its own built-in templates (shipped-in-binary defaults, seeded into a DB, etc.), a change to a role's generic body/description/tools/model in `roles.yaml` — or a new role — leaves that copy behind. When you make such a `roles.yaml` edit, propose re-syncing the downstream copy: apply the same generic-body change there, preserve each copy's own `## For this repo` tail and any built-ins it owns that have no `roles.yaml` equivalent, and mirror new roles across. Note that a downstream parser may be stricter than Claude Code's loader and reject the `version:` frontmatter key; such a copy carries the new body content but needs its own change before it can store the version stamp. This is a proposal gated like any `roles.yaml` edit; never auto-commit into another repo. (Concrete downstream targets and their tracking issues are kept out of this public file; check the maintainer's notes.)

## Mode 3: run

Use when `/agent-team <task description>` is invoked with a non-keyword first argument.

### Step 1: precheck

- Confirm `.claude/agents/` exists. If not, suggest running `/agent-team init` first.
- Confirm `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`.
- Read `.claude/agent-team.md` for the workflow + project signals.
- **FETCH BEFORE YOU RECORD THE BRANCH POINT. A local `main` is not the default branch; it is a cached guess at it.**

  ```sh
  git fetch origin && git rev-parse origin/<default>
  ```

  Record THAT SHA in the brief, not `git rev-parse main`. Measured 2026-08-03: a brief opened with *"Tip when this brief was written: `<sha>` (== `main`)"* and that parenthetical was **false at the moment it was written** — `origin/main` was four merges ahead, the most recent landing four minutes earlier, and one of those merges shipped the very thing that made half the branch's work redundant. `git merge-base --is-ancestor` settles it in one command. Nobody re-derived the claim because it is the one citation every other claim starts from.

  The drift was found 2h51m later, by accident, and cost **48 minutes** of re-merging and full validator re-runs plus a component built and then dropped. Note the failure is silent in the other direction too: **a two-dot diffstat's "deletions" against a moved base are not deletions**, they are files that exist on the default branch and not on yours. Use `git diff --stat origin/<default>...HEAD` (three dots) when you want your side only.

- **Put the agent-team artifacts on the work branch, NEVER the default branch.** Decide the feature branch (and, in a bare-clone-with-worktrees repo, which worktree) BEFORE writing any `.claude/agent-team-tasks/` brief or spawning a teammate, then operate from there:
  - **Single shared worktree:** if the session opened on the default branch (`main`/`master`), `git checkout -b <feature>` in THIS worktree first, so the briefs and every commit land on `<feature>`.
  - **Dedicated work worktree (bare-clone layout / parallel waves):** create the feature worktree (per the `git-worktrees` skill) and write the `.claude/agent-team-tasks/` briefs INTO that worktree — do not write them in a default-branch worktree the agents will not be in.

  Always name the exact branch in every spawn prompt, and ensure the lead itself is on/in the work branch before it authors briefs. Stranding these artifacts on the default branch breaks context handoff (the brief is invisible from the work branch/worktree) and pollutes the default branch. Observed 2026-06-13: a session started on `main` while its agents worked on another branch left the `.claude/agent-team-tasks/` ("agents dir") stranded on `main`, invisible from the work branch.

- **Decide WHERE the brief lives here, in the precheck, not later.** Step 4 makes the brief the spec; that only works if the brief outlives the run. **Check whether `.claude/agent-team-tasks/` is gitignored** (at least one downstream repo does). If it is, the brief is not durable and cannot be the spec — so pick one now: commit it to a tracked path on the work branch, or nominate the repo's own tracked artifact (the PRD, the issue, the ADR) as the spec. Name the chosen path in every spawn prompt.

  **PREFER A TRACKED PATH OUTSIDE THE IGNORED DIRECTORY** — the repo's own spec dir (`prds/`, `docs/`, an ADR). Force-adding the brief INTO the ignored directory works, and produces a file that is **tracked and simultaneously invisible to every recursive `grep`**, which is a combination nobody expects. Measured 2026-08-02 (a downstream project): `git ls-files --error-unmatch` reports it tracked; `grep -rl -F <string> .` does not find strings that are in it; **`--hidden`, the flag a careful person reaches for, changes nothing** (the path is *ignored*, not hidden — wrong axis); and plain `git check-ignore` on a tracked file is **fail-open**, exiting 1 with no output, so the natural diagnostic returns the reassuring answer (`--no-index` is required and names the `.gitignore` line at once). That defeats the retire-a-string sweep this same workflow mandates elsewhere. If you force-add anyway, record in the brief's own header that later sweeps over it must use `git grep`, which reads the index and finds it by construction.

  Deciding this at Step 4 instead, mid-flow, is how it gets skipped. Observed 2026-08-02 (a downstream project): the caveat existed only as a mid-flow choice, neither branch was taken, and **five days later the brief was unrecoverable** — the worktree was removed, the path is gitignored, and the spec of a merged change no longer exists anywhere. That run also found the right answer by accident: the ADR became the tracked brief, and it is the only reason the reasoning survived a squash merge at all. A brief that dies with its worktree cannot be amended, cannot be reviewed, and cannot be read by the reflect pass that is supposed to learn from it.

### Step 2: plan tasks

The session already has one implicit team — there is nothing to create. Go straight to planning the task list.

**Before you build the plan, gate on any acceptance criterion you cannot meet.** If investigation shows a stated AC is unsatisfiable as written — the only viable approach violates it — escalate a crisp go/no-go to the user *before* authoring a full multi-milestone plan, rather than burying the deviation inside a large plan and submitting it. An AC conflict is the likeliest reason a finished plan is rejected, and a one-line clarification is far cheaper than a discarded plan (measured 2026-07-20: a well-researched multi-milestone plan was hard-rejected with no recoverable reason because it knowingly contradicted a "one path, no second download" AC instead of surfacing the conflict first).

Create team-level tasks via TaskCreate:
- Task #0: **design critique** (owners: reviewer + auditor, and architect if the roster has one) — blocks every implementation task. Close it with a written reason when the change is small enough not to need one. This is a task for the same reason the roster sweep below is: it fires ONCE, at the moment you are already enumerating work, and a skip then LEAVES AN ARTIFACT somebody can contradict. Step 3 explains what the wave is for; this line is what makes it binding.

**HOW to close a skipped task, because the tool has no state for it and this rule failed on its own first run.** `TaskUpdate`'s statuses are `pending | in_progress | completed | deleted` — there is **no "closed with a reason"**. So write the reason into the record:

```
TaskUpdate({taskId, status: "completed",
            description: "<original> — SKIPPED <date>: <reason>"})
```

A reason stated only in conversation is **not an artifact** and does not satisfy this rule: the whole point of the paragraph below is that a later reader can see the decision and contradict it, and a chat message is invisible to every teammate and to the reflect pass. Setting `completed` on work that was genuinely *not done* is equally wrong — that is a false record, not a skip. If the step should have run and did not, leave it `pending`, say so, and surface it.

Observed 2026-08-02, on the first run after this task was added (a downstream project): the lead told the user it had "closed #6 and #9 with written reasons", never called `TaskUpdate`, and **three of nine tasks ended `pending` with no reason anywhere**. One of them was `specs/` sync, which Step 4 declares mandatory — so a prescribed step silently did not run and nothing recorded it. The reflect pass found it by reading `TaskList`; nobody else could have. **Add the check to Step 5 cleanup**, which already calls `TaskList` for `in_progress`: a task left `pending` at the end of a run is an unmade decision, not a skip.
- Task #1: implementation (owner: coder) — **ONE TASK PER UNIT. The count comes from the decomposition, not from this list.** Most work is one unit and stops here, producing exactly the graph these five bullets describe. Where the work splits into file-disjoint units, create one implementation task per unit and name that unit's file scope in the task description — that scope is the boundary the coder's own body already enforces.
- Task #2: review (owner: reviewer, blockedBy: #1) — **one per implementation task, each blocked on ITS OWN unit.** A single review task blocked on all of them cannot start while any other unit is still building, and that wait is the entire cost this shape exists to remove.
- Task #3: audit (owner: auditor, blockedBy: #1) — same: one per implementation task, blocked on its own unit.
- Task: **integrated pass** (owners: reviewer + auditor, blockedBy: every implementation task) — **create this ONLY when there is more than one unit.** With one unit the per-unit review IS the pass over the whole diff, and a second task is noise on the common path. With N units it is not optional: every per-unit validator was scoped to a diff that is not the diff being shipped, so cross-unit interaction is exactly what none of them could see.
- Task: spec sync (owner: spec-keeper, blockedBy: every #2 + #3, and the integrated pass where one exists) - only if spec-keeper role exists
- Task #4: release (owner: release, blockedBy: every #2 + #3, and the integrated pass where one exists, user-gated) - only if release role exists

**One unit is the default and costs nothing.** N=1 yields Task #0-#4 as listed, no integrated pass, no extra decision. The branch is available, not required. Note also that with N>1 the design freezes when the FIRST coder spawns, not the last: Step 3's frozen-spec rule is about a spec somebody has started building against, and after that a design change is a new wave for every unit, not a message to the one that has not started yet.

**A per-unit validator task needs a per-unit SHA, and only one of the two parallel modes hands you one.** Step 4 requires review scope pinned to explicit commit SHAs; `roles.yaml`'s coder body says a coder in parallel mode does not `git commit`. Both are right, and they only compose once you have said which mode you are running:

- **Per-worker worktrees and branches** (Step 3's *Parallel same-repo waves*): the worker commits on its own branch, so its unit already has a SHA. Dispatch that unit's validators against it, and before merging verify the branch ref equals the worker's last reported SHA — the same section's `(detached HEAD)` check, which exists because a follow-up commit on a detached HEAD merges as the stale pre-fix code.
- **Several coders in ONE shared worktree**: nobody commits, so there is no SHA to pin and that unit's validator task cannot start on its own. The lead commits the unit's reported edits by explicit path — the integrate step the coder body already assigns it — and dispatches the validators against THAT commit. Per-unit review here means one LEAD commit per unit, not one coder commit per unit.

If neither describes what you actually set up, the split is not settled yet; settle it before creating the tasks, because the graph you are about to build encodes the answer either way.

**What may overlap your integration gate is the READ-ONLY wave, never another implementation unit.** Reviewer, auditor, fact-checker and web-ux read; a second unit writes. In the shared-worktree mode every teammate is in one tree, so gating while a unit is still writing gates a tree that is moving underneath you, and the verdict is about nothing. Per-worker worktrees make the overlap safe because the writing happens somewhere else.

And **count the contention against the gate, not just against the clock.** Measured on a sibling project running this same fan-out shape: one suite ran 36.1s to 79.8s at a constant test tally, and 33.8s to 89.6s at another — 2.2x and 2.65x spreads driven purely by concurrent load, pushing into timeouts that had already been raised because of contention. A gate that reddens intermittently is WEAKER verification than a slow one, because the documented human response is to re-run, and the retry destroys the evidence. If fanning out buys wall clock with a flakier gate, it has not paid; report red-then-green-on-retry alongside the timing.

**Where the split rules live, and why they are not restated here.** The test for whether units are genuinely disjoint, the file-scope boundary each coder honours, and the no-commit / no-repo-wide-gate contract in parallel mode are all already written, in places the lead and the coder each read:

- `roles.yaml`, the `coder` body: the hard file-scope boundary, and the rule that in parallel mode a coder does not `git commit` and does not run gate/build/test commands beyond code it exclusively owns — *"the lead integrates, commits, and runs the repo-wide gate after all parallel units land."*
- `roles.yaml`, the `architect` body (section C): the decomposition contract — a dependency graph that maximises safe parallelism, and what a SEAM milestone owes every downstream milestone that consumes it.
- Step 3's **Parallel same-repo waves** (explicit worktree setup, merge-time SHA verification, cherry-pick over reimplement) and Step 4's **Pipeline milestones across review waves** (do not idle the coder while a wave runs).

A copy of any of them here would drift from the original, and nothing in this repo can detect that: `scripts/validate_skills.py` checks frontmatter only. Point at them; do not paste them.

**Then create a task for EVERY OTHER ROLE IN THE ROSTER WHOSE TRIGGERS FIRE ON THIS CHANGE, and close the ones you are not dispatching WITH A WRITTEN REASON.** Not a dispatch — a task. Tester when the change alters behavior; web-ux when it touches a web interface; architect on a new or changed contract; documenter when docs change; fact-checker when the change carries claims; spec-keeper when `specs/` exists. Closing one takes a sentence: `web-ux: skipped — no reachable instance` is a legitimate close. Leaving it uncreated is not.

**Write that sweep into the brief under a `## Roster` heading, one line per role, and keep it current as you dispatch** — `reviewer: dispatched at <sha>` / `web-ux: closed — no reachable instance`. The task list gets the same content, but the brief is the half that survives: Step 5's first cleanup check reads this section, and it can only do that if the heading exists. Without it the closure reasons live only in a store this file elsewhere calls a coordination convenience, and a session is on record where that store returned "No tasks found" at cleanup and took four written reasons with it.

**Why this is a task and not another instruction to remember.** The dispatch rules further down (Step 4) are conditional and fire mid-flow, each requiring you to notice a condition while doing something else. This fires ONCE, unconditionally, at the moment you are already enumerating roles. And it changes what a skip LEAVES BEHIND: today a role that is never spawned produces no artifact at all, so nobody — not the user, not a teammate, not a later reflect pass — can see that a decision was made, or contradict the reason. A closed task with a stated reason is visible and falsifiable. `web-ux: skipped — needs a running instance` sitting next to a role file that names a mock build is a sentence somebody catches in seconds.

This does NOT force a dispatch. Skipping a role is often right. What it forbids is skipping one silently.

**WRITE THE ROSTER SWEEP INTO THE DURABLE BRIEF AT THE SAME MOMENT, as a `## Roster` section — one line per role, dispatched or closed-with-reason.** Two writes, one of which survives.

**That section carries ONE MORE LINE: the unit split from the top of this step** — `units: 1 — one file, no split`, or `units: 3 — api/, web/, docs/`. Writing "1" is not a decision, it is the record that no decision was needed, so the single-unit path costs one sentence in a section you are already writing. What it buys is what Task #0 and the sweep buy: a lead that fanned out where the units were not disjoint, or serialised where they were, has left a line somebody can contradict. A split that exists only in the lead's head is invisible to the reflect pass by construction, and that pass (Mode 4 item 7) is the only thing that measures whether any of this worked — it reads this line first.

**The task list is documented-volatile and this rule depends on it entirely.** Gotchas already records that the shared task list can vanish mid-run; measured 2026-08-02, it did — `TaskList` returned "No tasks found" at cleanup, so Step 5's mandated check for tasks left `pending` was **unperformable**, and four written closure reasons were unrecoverable. Every justification for this rule ("a later reader can see the decision and contradict it"; "the reflect pass found it by reading `TaskList`") rests on a store this same file calls a coordination convenience. The brief is durable by the Step 1 precheck; write it there too and the rule stops depending on the store.

### Step 3: freeze the design, then spawn teammates

**For anything beyond a small fix, dispatch a DESIGN-CRITIQUE wave BEFORE the implementer, and treat the design as FROZEN once the implementer spawns.** Send reviewer + auditor + fact-checker (and architect, if the roster has one) the brief ALONE — no code exists yet. Settle what comes back, rewrite the brief, and only then spawn the coder, once, against a frozen spec.

**The fact-checker belongs in THIS wave, not a later one, and it is not redundant with the citation pass below.** A citation pass asks "does the brief's claim match the file it cites"; a fact-check asks "is the cited source itself true". They are different questions with different instruments, and the first cannot reach the second. Measured 2026-08-03: a reviewer returned 10 of 10 CONFIRMED and an architect 8 of 8 CONFIRMED on one brief, and **both missed that its central technical premise was inherited verbatim from a false sentence in the repo's own `CLAUDE.md`** — every citation resolved correctly, the source document was wrong. The fact-checker was dispatched late, found it, and the brief had to be amended mid-implementation.

**The wave's required deliverable is a CITATION, not a critique.** "Attack the design" is a mood, and moods produce variable output. Ask instead for the one thing that is mechanical and cheap: **for every mechanism the brief asserts, name the file that implements it and quote the line.** A brief that claims how a server aggregates, how a field is parsed, or what a table stores is making a claim about code that already exists and that the wave can read before any new code does. Have them attack the design after that; the lookup is what has actually paid.

Observed 2026-08-02 (a downstream project): the one design-stage finding that changed the outcome was not an attack, it was a lookup — the reviewer opened a pre-existing schema migration, a migration predating the branch that neither commit touches, and disproved the lead's claim about how the server aggregates, turning a caveated fix into an exact one. Nothing about that required adversarial posture, only reading the file the brief was talking about. It also attacks the dominant defect class of that run (prose asserting a mechanism the code does not have) at the earliest and cheapest possible moment.

Observed 2026-08-02 by its absence (a downstream project). The lead investigated, wrote a brief, and dispatched the coder immediately with reviewer/auditor priming in standby — the order this step used to prescribe. Corrections then went out as messages while the coder worked; two crossed it mid-turn, so it committed without the two most important ones and redid a full implement-and-gate cycle (measured: +485/−67 across 5 files in the follow-up, including a file the first commit never opened).

**The one finding that was genuinely available on day zero:** the reviewer read a pre-existing schema migration — a migration that predates the branch and that neither commit touches — and disproved the lead's claim about how the server aggregates, which turned a caveated fix into an exact one. No code needed to exist for that.

**Be careful how much this proves, because the honest reading cuts both ways.** That finding surfaced during standby PRIMING, which the old order already produced (see the pre-flag bullet in Step 4) — so it is equally evidence that priming works and that the real defect was the correction CHANNEL, which the Step 4 brief rule fixes independently. What the design wave adds is not the finding itself but its TIMING and its EXPLICITNESS: a critique that is asked for, time-boxed, and settled before the spec freezes, rather than one that arrives incidentally while a coder is already building against a spec the lead is still rewriting. Do not let this one anecdote carry more than that.

Corollary: **once the implementer has spawned, a DESIGN change is a new wave, not a message.** Amend the brief (Step 4) and say plainly if work in flight is invalidated. This does not retire the pre-flag rule in Step 4 — the two cover different things and the distinction is worth holding: a **pre-flag** is a finding about code or requirements that the coder can absorb without re-deciding anything, and it stays a mid-implementation forward; a **design change** re-decides something the coder has already built on. If you cannot tell which one you have, treat it as a design change.

Skip this wave for a small fix — a one-line change with an obvious mechanism does not need a design round, and a step that fires on everything gets skipped on everything. Step 2 makes the skip explicit by closing the task with a reason.

Then spawn each teammate in parallel via Agent calls in a single message:

```
Agent({
  name: "coder",
  subagent_type: "coder",
  model: "<alias tier from the role file>",   // pass an ALIAS explicitly; if the role pins an EXACT id (e.g. claude-opus-4-8) OMIT this line — the enum takes aliases only, and an omitted param honors the frontmatter (Step 5)
  prompt: "<task-specific cold-start prompt>"
})
```

For `coder`, the prompt includes the full task description plus paths to read.

For `reviewer` and `auditor`, the design-critique wave above IS their first dispatch — spawn them with the brief and an explicit "attack this design" ask, not with "stand by and go idle". After that wave settles they stay resident for the review round, so the standby cost is paid once and buys a critique instead of an idle. Only where you skipped the design wave (a small fix) is the older form right: "stand by, prime your context by reading <PRD/CLAUDE.md/CONTRIBUTING.md>, then go idle. Wait for the team lead to send the diff/files to review/audit."

For `tester`, do NOT spawn at kickoff. Spawn it after the coder's FIRST commit, so it builds its harness against a tree that is not moving. Observed 2026-08-02 (a downstream project): a tester spawned at kickoff built an elaborate and genuinely excellent control harness, then rehearsed it repeatedly as the design changed under it — it reported roughly a third of its work redone, and every pre-commit rehearsal measured a tree nobody would ship.

**Its single most valuable finding is also the proof of this rule**, and it is worth following the arithmetic because it cuts against the design wave above. The tester measured that the entire web suite passed IDENTICALLY under both candidate semantics — so a green gate could not distinguish the right implementation from the wrong one, and the coder's first commit shipped the wrong one. That measurement was over **1595** tests, which is the count AT the first commit (HEAD later runs 1606, and the follow-up nets +11). It could not have been taken before that commit existed. A tester's value is measurements at a real SHA; give it one.

For `release`, do NOT spawn at start; spawn only after user confirms post-audit. When you DO spawn it, the spawn prompt MUST instruct the releaser to include the changelog in the release: before tagging, fold the pending `CHANGELOG.md` entries into the version being cut (rename an `[Unreleased]` section to the version with its date, or otherwise assemble the version's notes); after the release publishes, confirm the release page / GitHub Release body carries that changelog section, not just an empty or auto-generated-commits-only body. A release that ships without its changelog is incomplete.

For `spec-keeper`, do NOT spawn at start; spawn after blocking review/audit findings are resolved. Its spawn prompt MUST carry a provenance breakdown of the change: which requirements and decisions came from the user (verbatim where possible) and which were AI choices made along the way. The team lead is the only one who has seen the full conversation, so assembling this breakdown is lead work; without it the spec-keeper cannot file decisions into `specs/human.md` vs `specs/ai.md` correctly.

**Parallel same-repo waves (multiple implementers editing one repo at once):**

- **Name each implementer after its UNIT, not its role: `coder-m2`, `coder-api`, never `coder` / `coder-2` / `coder-3`.** The `-N` suffix is assigned by slot-collision order, so it encodes when the agent spawned and nothing else — and it is the name you then address, the name in the roster line, and the name in every dispatch. A unit name makes roster line, worktree, branch and merge target ONE identity you can check by eye (`coder-m2` in `../m2` on `feat/m2`), so a dispatch aimed at the wrong worker is visible while you are composing it rather than after it writes. It also survives a recycle: a respawned `coder-m2` is still the m2 implementer, whereas `coder-2` respawned becomes `coder-4` and every earlier reference to it now points somewhere else.
- Do NOT rely on the Agent tool's `isolation: "worktree"` parameter for teammates: it has been observed to silently not isolate (one teammate switched the lead worktree's branch mid-run, 2026-06-10). Instead, put explicit worktree setup in each spawn prompt: `git worktree add <repo-parent>/<branch> <branch>`, then "cd there and do ALL work in that worktree; NEVER touch the lead's worktree or switch its branch".
- When N workers' branches will merge into one integration branch, instruct every worker NOT to bump VERSION/CHANGELOG (or any other shared file all milestones would touch); the lead does one consolidated bump after merging. Otherwise the merge hits N-way conflicts on those files.
- **Before merging a worker branch, verify its ref == the worker's last reported SHA** (`git rev-parse <branch>`; `git worktree list` must show the branch at that SHA, NOT `(detached HEAD)`). A follow-up commit (a review/hardening fix) made on a detached HEAD leaves the branch ref behind, so `git merge <branch>` silently integrates the STALE pre-fix code and DROPS the follow-up — and tests still pass when the dropped delta was additive, so it is invisible without this check (observed 2026-06-16: a coder's MEDIUM trace-read security fix vanished from the merge because its hardening commit sat on a detached HEAD; `(detached HEAD)` in `git worktree list` was the tell, caught only later by a docs fact-check against the code). Fix: merge the reported SHA directly (`git merge <sha>`), or confirm the ref first; either way, after merging grep the integration tree for a signature line from each follow-up to prove it landed. (This is the merge-time face of "trust but verify against artifacts" in Step 4.) When directing worker A to merge worker B's branch, pin B's last reported tip in the dispatch but add "check the live tip and take whatever it is; if it moved past the pinned SHA, say so instead of guessing" — B may land a final small commit while the dispatch is in flight (observed 2026-07-05: a cosmetic fix landed on the web branch seconds after the merge dispatch; the live-tip instruction absorbed it without a round-trip).
- If a wave is aborted before commits land, leftover empty branches are fine: tell the respawned worker to reuse the existing branch instead of deleting and recreating it (branch deletion may be denied by the permission classifier as destructive).
- **Shared utility needed by two parallel branches (e.g. two PRD teams both needing one new package): cherry-pick the exact commit that introduces it, never reimplement.** Byte-identical content on both branches merges trivially; a functionally-identical reimplementation guarantees add/add conflicts on the package plus divergent edits to shared wiring files (config, compose, .env.example). If a worker already committed its own version before the coordination signal arrived and that SHA has NOT been dispatched for review, replace it (`git stash -u` any adopted working-tree files → `git reset --hard HEAD~1` → `git cherry-pick <shared-sha>` → `git stash pop`) rather than living with the fork (observed 2026-07-03: two AES secretbox implementations, same public API, different comments and config wiring, on sibling PRD branches).
- **SEQUENTIAL pipelines can share ONE worktree with a lead-enforced writer token** (validated 2026-07-05: coder → documenter → spec-keeper → coder handoffs on one branch, 8 writer transitions, zero collisions). Rules: exactly one teammate unfrozen for writes at a time; every dispatch to a new writer names the verified tip SHA; the outgoing writer explicitly confirms FREEZE (and the lead verifies the tree is clean at the expected tip) before the next GO; read-only agents (reviewer/auditor/tester/fact-checker) run in parallel freely. Use per-worker worktrees only when writers must genuinely work CONCURRENTLY. When a validator must BUILD/TEST a pinned SHA while the shared tree carries the current writer's uncommitted WIP, it should verify in a throwaway detached worktree (`git worktree add --detach <tmp> <sha>`, removed after) — validated 2026-07-10: a reviewer's `go build` failed on the live tree purely from the coder's in-progress M3 edits, and the detached-worktree run cleanly verified the committed SHA. **Lead edits to a shared file in the active writer's worktree** (PRD checkbox bookkeeping, brief updates) are fine WITHOUT taking the token if you pre-warn the writer in the same breath: "the uncommitted change in <file> is MINE (team lead), expected — do not stop; fold it into your next commit." The pre-warning defuses the concurrent-writer guard the spawn prompt installed; without it the guard correctly reads the edit as a foreign writer and freezes the worker (validated 2026-07-13: a mid-milestone PRD progress edit landed cleanly inside the coder's next commit, zero disruption).

  **That carve-out covers FILE EDITS and nothing else. REF-MOVING commands — `merge`, `rebase`, `reset`, `checkout`, `stash`, `push` — are never the lead's to run in ANY worktree holding a live writer.** Either the single writer runs them, or you take the token first. The predicate is *a live writer*, not *a contested tree*: "contested" is the wrong test precisely when the lead is the one making it contested, and a single-writer worktree is exactly where the lead feels entitled to reach in. Observed 2026-08-03: a lead ran `git merge origin/main` into an agent's active worktree twice in one session, safe both times only because the tree happened to be clean at that instant. A `reset` or `checkout` there takes uncommitted work, and the agent cannot detect it happening — the Bash tool resets cwd between calls and nobody re-reads HEAD before each edit.

  **And publish the SHA you gated, not the branch that points at it:**

  ```sh
  git push origin <sha>:refs/heads/<branch>
  ```

  A branch name resolves at push time; a gate result is bound to a SHA. Same session: the lead gated a merge commit, an agent committed twice into the same worktree in the intervening minutes, and the lead's `git push -u origin <branch>` published the agent's tip — a commit the lead had never gated — after which it opened an MR whose description claimed verification of a different SHA. Nothing had to go wrong for that: no stray push, no hook, no misconfiguration. **The refspec form makes it impossible; "re-verify before pushing" only makes it less likely.**

Teammates run as **background agents**: an `Agent(...)` spawn returns immediately (background is the Agent tool's default), and each teammate's completion or idle state arrives as an automatic notification, so you do NOT poll and there are no panes to lay out or watch. Drive the flow off those notifications (Step 4), and check status with `TaskList` / `TaskGet` when you need it. Never `Read` a teammate's `.output` file: for an agent it is the full conversation transcript and will overflow your context.

### Step 4: drive the flow

- **A MESSAGE CARRIES AT MOST ONE ACTIONABLE ITEM.** Two or more go in the brief; the message names the section and nothing else. This is the protocol, not a style preference — count the asks before you send, which is checkable while composing in a way that "be careful about crossing" never is.

  **Why one and not two.** Mailboxes are read between turns (see Gotchas), so delivery races a turn boundary and the loss is **per item** — and a partially-absorbed multi-item message is indistinguishable from a fully-absorbed one to both parties. One item either landed or visibly did not. N items are N independent chances with no observable difference, which is why the failure presents as "the worker ignored point 3" rather than as a delivery error.

  Measured across **six** sessions now (2026-06-12, 07-05 twice, 07-13, 07-16, 08-03), always with the same recovery: a standalone single-item re-send to an *idle* worker lands first try. In the most recent, one finding crossed **three times** inside multi-item messages and landed immediately once sent alone. Six sessions of better prose about crossing has not moved that number; the item count is the only lever that has.

  **RECORD EVERY CROSSING IN THE BRIEF, one line, at the moment you recover from it:**

  ```
  crossed: <role> — <item> — recovered by standalone re-send
  ```

  **Crossing is currently UNFALSIFIABLE, and that is a defect in this rule, not a property of the world.** Every session above is a recollection; nothing writes crossings down, so the record can only ever accumulate the ones somebody happened to notice and remember. A 2026-08-04 run produced **2216 lines of run log and zero crossing records** — which is equally consistent with "the one-item rule worked perfectly" and with "crossings happened and went unlogged", and no reader can tell which. A rule justified by measurement has to be measurable, or the next six sessions add nothing but confidence.

  **GATE THE SEND ON THE RECIPIENT'S STATE, NOT ON THE PAYLOAD'S SIZE.** Before dispatching, confirm you have SEEN that worker's idle/completion notification since its last dispatch. One actionable item is a property of what you wrote; being idle is a property of whether it can receive. The first is checkable while composing and is the cheap default, but it is the second that the recovery has always turned on: *"a standalone single-item re-send to an **idle** worker"* — every recorded recovery names idleness, and the item count came along for the ride.

- **THE BRIEF IS THE SPEC. Corrections AMEND THE BRIEF; messages only point at it.** When a requirement changes, edit `.claude/agent-team-tasks/<slug>.md` and send a short message naming the section that moved — never a long message carrying the requirement itself. Date each amendment inside the brief so a worker can tell what it has already seen.

  This one rule fixes three failures at once, and all three are already documented separately in this file as things to be careful about. **Crossing stops mattering**: a worker re-reads the file at its own pace, so a mid-turn dispatch cannot be missed the way an inbox message can. **Messages get short**, which is the actual cure for a lead burning a worker's context on prose. And **there is one authoritative spec** instead of a spec plus N amendments scattered across an inbox, so "did you get requirement 6?" stops being a question anyone has to ask.

  Observed 2026-08-02 (a downstream project): the lead wrote a brief, then sent every subsequent correction as a long message instead. Two crossed the coder mid-turn; it committed without the two most important requirements and redid a full implement-and-gate cycle. The corroborating artifact is the brief's own mtime — written once and never touched again, while commits landed half an hour and an hour later. (The count of correction rounds is the lead's own recollection, not a measurement; the never-amended brief is the part that is checkable.)

  **The diagnosis is sharper than "the lead ignored a rule", and it is why this belongs HERE.** The "write long context to a file" rule already existed — but inside the *Context handoff* section of the **workflow-doc template**, i.e. text destined for the repo's own `.claude/agent-team.md`, describing how to write a SPAWN prompt. The skill's own dispatch steps never carried it, and nothing anywhere said that CORRECTIONS are context too. So the lead followed it exactly where it was written and abandoned it everywhere else.

  **The brief must be at the durable path you chose in Step 1's precheck.** If you skipped that decision, make it now before relying on this rule: a gitignored brief is not a spec, it is a scratch file that dies with the worktree.

  Keep messages for what they are good at: a pointer ("brief §3 changed, re-read before your next commit"), a question, an ack, a verified-evidence nudge.

- **PASTE THE POINTERS YOU ALREADY FOUND, AND LABEL THE SET `exhaustive` OR `starting point`.** A dispatch that says "go find X" buys N re-derivations of a search you have already run, because validators cold-start with no memory of your investigation: a reviewer, an auditor, a fact-checker and a tester sent at one diff each independently grep for the same symbols.

  This is not *Context handoff* restated. That rule says the spawn prompt must be self-contained, which is about completeness. This one is about DUPLICATION: the lead already holds the expensive half.

  **The label is what makes this a step rather than a caution.** Which of the two words you wrote is visible while composing, exactly as the one-actionable-item rule above is countable while composing, and a rule with no such check is the kind Mode 4 says not to add. It is also not bookkeeping, because **the SET of locations is itself a claim, and it is the one that bites.** The bullet after this warns that a lead's expectation reaches every validator at once; a pointer list is that expectation in its most credible form. Name four files when the defect is in a fifth and all four validators inherit the omission simultaneously, each believing it was handed the map. `starting point` keeps them obliged to look past it; `exhaustive` is a claim you own, so write it only when you have actually enumerated.

  **Two further limits.** Paste the LOCATION, never your conclusion about what is there: naming a file is context, while telling a validator what it will find is authoring the control for your own claim (see the bullet below). And a pointer is a citation, so it inherits a citation's obligation, pinned to the SHA you are dispatching, because a bare line number goes stale the moment the tree moves.

  Where a sweep genuinely is needed and its product is a conclusion rather than a corpus, delegate it to a read-only search subagent. `Explore` is the documented instance and is cheaper than it looks: it skips `CLAUDE.md` entirely, so it pays none of the per-spawn preamble above, and its context is discarded so the caller pays only the answer.

- **THE LEAD DOES NOT AUTHOR THE CONTROL FOR ITS OWN CLAIM.** When you dispatch a validation, name the **behaviour** that must be pinned. Do NOT name the fold class, and do NOT state the expected result. Require the validator to choose its own instrument and to say why that instrument *could* produce the disconfirming answer. If you have already formed an expectation, label it as your prediction — never as the acceptance criterion.

  **A control written from inside the change inherits the change's blind spots.** That sentence is usually applied to a coder folding its own code; it applies to the lead at least as hard, because a lead's expectation reaches every validator at once.

  Two shapes, both measured in one session. **A criterion that contradicts its own fix:** the lead demanded a red from a *positional* fold on a fix whose entire purpose was removing the positional dependency — if the fix worked, that red could not exist, and a round was spent proving it. **A fold class named in the brief:** the lead wrote the class into the brief, three agents used it in three different wordings, and the agreement was read as confirmation. All three were *presence* mutations, so they were one instrument, and the class they could not see turned out to be **five pins wide** and took three further rounds to close. The record's own summary: *"Three separate controls agreeing was three readings of one instrument."*

  Note this bullet asks you to write **less** in a dispatch, not more. (It scopes to this bullet only: the pointer rule above asks you to write more, and deliberately, because pasting a location you already have is not the same as telling a validator what to conclude.)

- **THE MECHANISM NARRATIVE IS WRITTEN ONCE, BY ONE OWNER, AFTER THE LAST BEHAVIOURAL COMMIT.** While findings are still in motion, a mechanism belongs in reports and in the brief — **not** in code comments, docstrings or an ADR. A claim written into three artifacts must be corrected in three artifacts, by whoever holds each file, and every corrector re-derives it from scratch. Wait for the facts to stop moving, then dispatch the documenter (or take it yourself and *say* you did) to write it in ONE place, with the other artifacts pointing at that place rather than restating it.

  **If the claim is load-bearing, it ships with a named test, not with a paragraph.**

  Measured 2026-08-02 (a downstream project). The merged commit is **~106 lines of production code inside 2,293 added lines** — about 21:1 prose-and-fixtures to code, with one module alone adding 320 comment lines against 87 code lines. One claim (which guards floor a negative value) was narrated simultaneously in the module header, the test file and the ADR, and was stated **wrongly five times by four different agents including the lead** before it was right. It stopped moving at the exact moment it acquired an executable control, not before. Five wrong statements is the arithmetic of that shape, not a lapse of care by anyone.

  This is the dispatch-side half of *Sweep per FACT after the last behavioural commit* in the workflow-doc template above — **read that section before running the sweep.** And note why this bullet has to exist at all: that section is excellent and appears exactly once in this file, as its own heading, inside text destined for the repo's `.claude/agent-team.md`, which teammates cold-start and never read. The same seam swallowed the brief rule (see the diagnosis in the bullet above) and the "a comment is an assertion" rule, which lived only in that template and appeared in **no role body at all** until 2026-08-02. When a rule in the template keeps failing in practice, the fix is a step in Mode 3 that executes it, not a better paragraph there.

- Wait for coder's completion message via the automatic mailbox notifications (do NOT poll).
- On coder done: SendMessage to reviewer and auditor with the diff summary, file paths changed, the coder's report, **and the `## Quality gates` block from `.claude/agent-team.md`**. Their bodies tell them to run the dead-code and security-scan slots, and teammates cold-start — only you have read the workflow doc, so a slot you do not paste is a slot they cannot run. Same when dispatching the tester.
- **Pin review scope to explicit commit SHAs** in the review dispatch, and if the worker adds commits after the review was dispatched (a follow-up fix, a crossed-in-flight reconciliation), immediately SendMessage the reviewer the new tip and require explicit confirmation that ALL commits were covered. Observed failure mode: a reviewer's report cited only the first of two commits on the branch; the second commit was verified only after a direct "did you cover SHA X?" follow-up. A second tell (observed 2026-07-05): a validator's report describing behavior that CONTRADICTS the worker's description of a later commit (e.g. praising semantics the follow-up reversed) means the validator reviewed a stale tip — require a re-read at the live tip and an explicit ruling before accepting either claim. **The lead-side half of the same protocol: require every report to LEAD with its tip SHA, and verify THAT SHA, never `HEAD`** — `HEAD` moves under you while the worker works, so a `HEAD`-based probe is a race you lose silently (validated 2026-07-21, proposed by the coder after it cost a round-trip: the lead grepped for a symbol at `HEAD`, found nothing, and declared seven items unlanded; they had landed one commit later). **And distinguish EARLY from STALE when a probe disagrees with a report** — they have different fixes and the familiar one hides the other. Stale = you read a value that has since changed (fix: pin what you verify). Early = you ran the probe before the worker applied the change, so your result was correct when taken and correct forever *at that SHA* (fix: re-probe, do not re-dispatch). Same run, both occurred; the second was nearly mis-attributed to a bad grep pattern, which would have "fixed" a grep that was never wrong.

  **AND PASTE THE TREE EVIDENCE INTO THE DISPATCH. Do not STATE whether a writer is live — paste the OUTPUT that answers it. A pinned SHA does not make a shared tree safe.**

  ```sh
  git -C <worktree> status --short; git -C <worktree> log --oneline -3; git worktree list
  ```

  Three commands, one paste, at the top of the dispatch. If it shows a live writer, also require the validator to build from `git worktree add --detach` or `git archive`, never from the shared tree.

  **Producing that output IS the check; writing the sentence is not.** That is the entire reason this is a paste and not an assertion, and it is why the fix is here rather than in another caution. A lead that runs the commands cannot get the answer wrong, and a lead that writes "the tree is clean" is making a claim it may have formed minutes ago, about a tree that has moved since, or from memory of a different worktree. The cost is identical; only one of the two can be wrong.

  Measured 2026-08-03: the lead pinned SHAs correctly throughout and **three of four validators were still contaminated**, because the SHA it pinned was also the coder's checked-out worktree — each built a mid-edit or mutated tree, and each was caught only by a CONTRADICTION between static reading and observed behaviour rather than by suspicion. The one that complied was the one whose role body carried the rule. Measured again 2026-08-04, on the assertion side: a lead made six wrong claims about tree and commit state in one run, and **four of the six were exactly this paste** — a worktree described as clean while a writer was live in it, a "no writer live" that was false, a diff described as containing a fix that was in a later commit, and a `git reset --hard` run in a worktree with an assigned writer.

  The reviewer and auditor bodies in `roles.yaml` carry the receiving half: *your dispatch must open with the dispatcher's tree evidence; if absent, derive it yourself and report that it was missing.* Enforcement lives there on purpose, because **the lead has no role file** and nothing else in this system constrains what it asserts.
- **Dispatching against an UNCOMMITTED working tree needs its own pin, and the SHA rule above silently does not cover it.** Reviewing an edit before you commit it is often right — it is how you avoid committing something a validator would have refused. But "pin to a SHA" has nothing to pin, so the validator checks a tree you are still editing, and every finding it returns is against a state that no longer exists by the time you read it. **Snapshot first** (`git stash create` gives you a throwaway commit object without touching the tree; a scratch copy works too), dispatch the validator at the snapshot, and say in the prompt which snapshot it is.
  Without it you pay twice. The findings arrive against text you have already rewritten, so you cannot tell a live defect from one you fixed an hour ago — and worse, **the validator loses the ability to claim independence**. A fact-checker that re-derives a mechanism *before* reading your assertion of it has corroborated you; one that reads your sentence first can only agree with it. Which of those you got becomes an accident of timing rather than a property of the check.
  Validated 2026-08-03: a lead dispatched a fact-checker and a reviewer at an uncommitted PRD edit, then rewrote it twice while they worked. Both delivered, and the fact-checker had to segregate its verdicts into EARLY (correct at the state it read, target since deleted) versus current — an extra round of work that a snapshot would have removed entirely. It made the point itself: its strongest finding *"only counts as corroboration because I happened to reach it before the file changed under me."*
- **No amends after review dispatch — put this rule in the coder's INITIAL spawn prompt.** Workers may amend freely BEFORE a SHA is dispatched for review; once dispatched, fixes must land as follow-up commits, never amends. An amend orphans the pinned SHA (it stops being an ancestor of HEAD), invalidates in-flight review scope, and forces a re-confirmation round-trip with every validator. Observed twice in one run (2026-06-12): both amends crossed the reviewer's/auditor's reports in flight, and each validator had to re-diff and re-confirm coverage of the new tip; after the rule was sent mid-run, all later fixes stacked cleanly. Stating it at spawn time costs one sentence; correcting it mid-run costs a round per validator per amend.
- **Pipeline milestones across review waves**: once a milestone's SHAs are frozen and dispatched to the read-only validators, dispatch the coder's NEXT milestone immediately — do not idle the coder waiting for the wave. The no-amends rule makes this safe: any findings come back as labeled follow-up commits on top of whatever the coder has built since, and validators needing to build a pinned SHA use a detached worktree. Validated 2026-07-13: a 6-milestone PRD ran coder-implementation and review waves fully overlapped, zero rework, zero blocking findings, no idle coder time. Two docker-stack agents in one worktree (a validator's kept e2e stack + the coder's e2e gate) may NOT collide — check how the harness derives its compose project name (a PID-derived unique project = safe to overlap) before serializing them.
- **On reviewer + auditor both done, run these four in order. It is a sequence, not four things to remember.**

  **(a) Synthesize** the findings for the user.

  **When two validators DISAGREE, send each the other's DEMONSTRATION and require a measurement back, not a position.** Not a summary of the other's view, not your paraphrase of it: the command, the fixture, the quoted line, whatever the finding rested on. Then rule. **A ruling that adopts NEITHER is a normal outcome** and worth saying out loud, because the shape of the question invites picking a winner: two validators can both be measuring correctly and disagree because they measured different things, and the answer is the third thing neither one ran. Adjudicating from the two reports alone reduces to trusting whichever is written more confidently, which is uncorrelated with being right.

  **(b) Apply the severity bar, and RAISE it when the round has turned inward.** A findings round's bar is a property of the ARTIFACT, never of the reader's standard. When a wave's findings are dominated by the PREVIOUS wave's output rather than by the deliverable, announce to every validator in one message: **Blocking now requires a demonstration** — an execution that fails, or a re-derivation showing a sentence is FALSE. "Could be sharper", "imprecise", "unsupported but probably true" become Non-blocking, reported in a separate list and never suppressed. Read that list yourself; an item naming a MECHANISM rather than a preference gets promoted.

  **The trigger is a COMMAND, not a judgement — run it as the last act of (a) Synthesize, before you write anything.**

  ```sh
  git diff --numstat <round N-1 tip> <round N tip> -- <deliverable paths>
  ```

  **If the executable content did not move, round N produced no behavioural change.** Arm the bar by default and say so in the message that opens round N+1. The older trigger — *round N's findings cite artifacts introduced in round N-1* — is still true and still worth knowing, but it is a judgement about provenance made while you are synthesizing the findings, which is the noticing-not-mechanism failure this step exists to remove.

  **The exception, or this over-fires and gets waved through:** a round whose deliverable is legitimately prose — a docs migration, a spec sync, a comment that IS the product — will trip this every time. When that is the case, say which artifact is the deliverable and move on. The bar is for rounds that *intended* behavioural change and produced none.

  Measured on two branches in one session: one change's deliverable took **148 added lines after its implementation commit, every one a comment or blank**, across nine follow-up rounds. The other's product template went **19 → 8 → 6 → 4** added lines across four rounds while the findings prose stayed above 100 each time — 37 lines of deliverable against ~540 of record. Neither fired the old trigger, and the lead had read it.

  Why it is on the artifact: *imprecise* and *could be sharper* are properties of the reader, and a reader's standard rises as the artifact improves, so a loop gated on them has no exit condition. *States something false* is a property of the artifact — decidable and finite. Measured 2026-08-02 (a downstream project): 19 commits of which four touched executable content, seven findings rounds, **zero behavioural defects**; rounds 5-7 were finding defects in the corrections, and each correction could acquire its own unchecked support. The user imposed this bar ad hoc and it terminated the loop in one round. The code-heavy predicate is the same rule with a different demonstration — for code this file already names it (*a control that produces no output is not a control*); prose is the case where nobody noticed the demonstration was missing, because a sentence about a sentence reads like analysis.

  **The caveat, which is the failure mode of the bar itself**: a validator that cannot afford the demonstration will file Non-blocking and move on, and a real defect will sit in that list. The mandatory separate list plus your reading of it IS the mitigation. Do not skip it.

  **(c) Write the accepted findings into the BRIEF as a dated amendment, and commit it.** Not into a message. This is the step that makes the rule in the first bullet actually bind — see the diagnosis there.

  **A worklist item QUOTES the finding's demonstration and NAMES its author; your paraphrase goes on a separate line, labelled as yours.** `reviewer: "<the quoted demonstration>"` then `lead's read: <your paraphrase>`. Two things collapse into one sentence otherwise, and the collapse is not recoverable afterwards: the worker cannot tell what was measured from what was inferred, and cannot go back to the author to ask. A paraphrase also silently upgrades: a validator's "this may not hold when X" becomes the lead's "this is broken", the coder fixes the lead's version, and the validator's actual finding is never addressed.

  **And a CARRIED-FORWARD item names THE FACT THAT CHANGED, not the token.** "Sweep for `OLD_VAR`" survives its own completion — someone greps, finds nothing, ticks it, and every sentence that assumed the old behaviour without naming it stands untouched. Write the fact instead: "the config is no longer single-device; every sentence assuming one device is now false." A grep cannot close that, and a reader can check it.

  **The SAME COMMIT updates the `## Roster` row for every role that amendment came from, and declares the exception in (b) if you are invoking it.** Both are one edit with the amendment, not separate disciplines to remember, and that is the whole point: they fire on a cadence that already exists. Measured 2026-08-03 on a run with 24 amendments: **6 of 11 roster rows still read "pending"** at branch completion, including one for a role that had been dispatched and had produced a numbered amendment — and the shared task list returned "No tasks found" at cleanup, so every disposition decided *after* kickoff was unrecoverable. Only the three closed at kickoff survived. Same run, the (b) exception applied to the whole session and nobody invoked it by name, which is indistinguishable afterwards from having forgotten the bar existed.

  **(d) Send each worker a message whose body is the amendment's section name and nothing else.**

  Steps (c) and (d) sit between two things you already do, so skipping (c) leaves a visible hole in a sequence you are mid-way through. The previous form asked you to notice, while composing a message, that the message had become a spec. Measured 2026-08-02: a lead that had amended the brief correctly twice then sent every findings-round correction as a long message instead, and **four dispatches crossed the coder mid-turn**. It also removes the stale-SHA failure for free — two of that lead's "outstanding items" lists were built by reading the tree at a SHA the coder had already moved past, and **an amendment cannot go stale the way a message does**, because the worker re-reads the file at its own pace at whatever SHA it is on.
- **Trust but verify teammate-reported gate results against artifacts** when the result matters beyond the report: a teammate's "task build green" can be stale-cache luck or a partial run. Cheap checks: binary timestamp/version after a build claim, `git log` tip after a commit claim. (Observed: a release agent reported the build gate green while the installed binary was left months stale.) The same applies to **time-sensitive repo-state claims** ("branch X has/lacks file Y"): re-run the check yourself before making a coordination decision on it, especially when two teammates' claims conflict — a standby agent's primed check goes stale within minutes while parallel branches advance (observed 2026-07-03: an auditor's "secretbox absent on the sibling PRD branch" was contradicted by the reviewer; the lead's own `git ls-tree` settled it and reversed the build-vs-cherry-pick decision). **This generalises past gate results to every claim the team asserts, including its own comments and your relays** — see the "Re-derive the claim at the moment you assert it" section in the workflow-doc template above, which is the compact form to write into `.claude/agent-team.md` at init. The lead's specific share: **relay findings as claims to check, not facts to apply**, and say what was measured vs inferred. Validated 2026-07-16, where the lead twice propagated a validator's *inherited* attribution as verified — one of them a mechanism a comment had asserted and nobody had re-derived, which then collected a second reviewer's certification before a third agent froze the field and found nothing read it.
#### Waiting on a long-running worker

- **Idle + uncommitted edits = stalled mid-fix, not done.** Check `git -C <their-worktree> status`; send a "finish the loop: run the gate, commit, report the SHA" nudge. Prevent it at dispatch time: any message sending NEW requirements to a worker that already reported done MUST end with that same instruction (2026-06-10: a coder applied forwarded hardening but went idle with it uncommitted, so the "release-ready" tip lacked the fix).
- **Point-in-time evidence lies when the gate is a long multi-phase job.** "No container running" can be an inter-phase gap; stale mtimes mean waiting, not wedged. Send ONE status question with forced options — "(a) working + where, (b) blocked on X + error, (c) done, committing" — and wait a full known-gate-duration before escalating (2026-07-05: a coder mid-e2e was nudged twice as stalled and was running the whole time).
- **Do NOT trust a background wait-loop to wake the worker.** It does not reliably re-invoke on exit (2026-07-13: an overnight e2e finished, its trap tore the stack down, and the coder never woke — green-tested WIP sat uncommitted for hours). If the known duration passes silent, verify the artifacts yourself and nudge with the evidence stated: "your stack is gone so the run exited; check your log, commit, report the tip."
- **A lead's own watcher must watch an IDENTITY, not a pattern.** Capture the PID at spawn and `kill -0 <pid>` until it exits. Never `pgrep -f <script-name>`: a harness that re-execs itself (via `env -i`) reconstructs argv, the path prefix disappears, and the watcher fires a phantom "finished" mid-run (2026-07-22). `kill -0` fails safe — on PID reuse it never fires rather than false-firing. If you genuinely cannot capture the PID, match an invariant the re-exec cannot change: its growing log file, its compose project, a lockfile.
- **Verify the completion marker actually reaches the file you are watching.** `cmd > log 2>&1; echo EXIT=$?` sends the marker to the shell's stdout, NOT the redirected log, so a grep-for-marker watcher never fires (2026-07-15). Watch for the harness's own final summary lines, or for the process disappearing.
- **NEVER instruct a worker to edit a script while that script is executing, and treat a refusal as CORRECT.** `bash` reads a script incrementally, so a mid-run edit can resume the interpreter at a byte offset landing mid-token — silent corruption presenting as a bizarre new gate failure (2026-07-21: the coder declined, cited the mechanism, and that is what let the lead verify instead of overrule). Committing during a run is safe; git does not rewrite the working file.
- **Standby reviewers/auditors often surface baseline pre-flags while priming** (they read the target code before the coder finishes). Forward actionable pre-flags to the coder MID-implementation instead of holding them for the review round — requirements are cheaper upstream than as blocking findings (observed: an auditor's pre-flags became the spec for a hardening commit, avoiding a full fix-and-re-review cycle). CROSSING HAZARD (observed 2026-07-15): a pre-flag forwarded while the coder is mid-milestone can be acted on AFTER the milestone's review wave already ran — the coder lands a follow-up commit implementing a pre-flag suggestion whose OPPOSITE the wave just praised, leaving two contradictory validator positions on file. Don't let both reports stand: dispatch a scoped delta review of the follow-up commit that names the contradiction and demands an explicit keep/revert ruling (that run: both validators ruled KEEP and one retracted its earlier framing — one cheap delta round, clean record).
- **If the tester role exists and the change alters behavior, dispatch it by default** — after the coder's first commit, not at kickoff (Step 3) — on the scenario surface (the real runtime path: live conversation, real container/deploy entrypoint, the spec's stated success criteria). Coder-authored unit/security suites are NOT a substitute for end-to-end scenario proof — they test components, not the user-visible criterion. Observed (2026-06-12): a PRD's headline success criterion ("answers question X end-to-end via the real docker path") sat unproven through four reviewed milestones because the test matrix looked comprehensive; the user caught the missing tester, whose E2E run then produced the only direct evidence of the criterion (plus an adversarial injection probe and live doc-example verification the suites couldn't provide). **Kept-stack validation wave** (validated 2026-07-15): when the coder's final long gate supports a keep-stack/keep-instance flag, have it run the gate WITH that flag so tester + web-ux validate against the SAME live instance in parallel right after — one gate run serves three validators. Hostile/mutating probes go to a PHANTOM second identity (e.g. a worker row minted from a fresh join token that no real process drives) instead of racing the real component's own update cadence — deterministic assertions, no flapping. The lead coordinates teardown only after the whole wave reports. SHARED-STACK VALIDATOR RACE (observed 2026-07-15): a MUTATING tester and a read-only browser validator on the same stack collide two ways — the tester's authorized mutations flip the persona states the other validator was briefed to expect (a token delete/save changes no_token/unavailable rows mid-pass), and an app with single-active-session-per-user revokes each other's logins mid-journey. Mitigations: partition personas per validator (tester mutates only personas the browser pass doesn't rely on), brief the read-only validator that states are a moving target and to verify each rendering against the authoritative API at read time (that's what saved the run — the SPA always matched its API, so both passed), and the moment the tester reports, relay its residual-state list to any validator still driving the stack.
- If the architect role exists: for a non-trivial task (new component, cross-cutting change, new or changed contract/interface), dispatch it BEFORE the coder and fold its design summary (or the ADR path it wrote) into the coder's spawn prompt; skip it for small fixes. Post-implementation, it can join the reviewer/auditor wave for an architectural-fit pass when the change moved boundaries. Also dispatch it whenever a PRD is being written or reviewed (including `/prd-create`-style flows): it contributes the architecture sections and the milestone decomposition/dependency graph when writing, and judges feasibility, hidden milestone coupling, and independent shippability when reviewing. Open design questions it flags go to the user, not to the coder as guesses.
- If the fact-checker role exists: dispatch it in the same wave as reviewer/auditor, scoped to the change's claim-bearing artifacts (docs, README, CHANGELOG, report prose), with explicit pointers to which claims matter. Treat REFUTED claims as blocking findings; UNVERIFIABLE ones go to the user with what would be needed to verify.
- If the web-ux role exists and the change touches a web interface: dispatch it in the same wave as reviewer/auditor, with a reachable URL for the running UI (start the dev server / compose service / demo build first, or tell it how to) and the list of changed flows. It validates in a real browser via `agent-browser` and reports UX findings plus refactor proposals; treat Blocking findings like reviewer blockers, and relay Enhancement proposals to the user rather than auto-scheduling them. Dispatch it on the wave where a user-facing control or journey LANDS (not only once at task end), and require it to drive the feature's PRIMARY journey end-to-end — the "can the user actually do the thing" click-through — because component reviews and API-level e2e both pass while a client-side gate dead-ends the UI (observed 2026-07-06: a server-side gate bypass was fully unit-tested, audited, and e2e-green while the UI's Start button stayed disabled — the client gate never learned the bypass; only the browser pass caught it). **Prefer pointing it at a mock/demo build or an isolated dummy-data stack** — its role forbids real mutations (destructive buttons, merges, sends) without explicit user permission, so on a real stack it can only navigate read-only and will report mutation-bearing flows as not-validated, proposing a mock instance instead; relay that proposal (or the permission ask) to the user.

  **Before dispatching, name the fixture value that exhibits the condition under test.** State in the prompt which fixture or instance it will observe and what value in that data makes the bug visible. **If you cannot name that value, the pass validates RENDERING rather than BEHAVIOUR — say so in the dispatch**, so the report comes back correctly scoped instead of confidently wrong. This is the cheap, checkable half of the rule the workflow-doc template calls the only one that prevents work rather than catching it after; it costs one line and the receiver can verify it.

  Observed 2026-08-02 (a downstream project): the dispatch pointed at a mock build whose fixtures emit the two fields under test with **identical numbers** and a single model key, so mock mode was structurally incapable of exhibiting a divergence the fix existed to close. The instrument was fine; the **data** could not produce the disconfirming answer. It returned a confident false bug report against correct code, retired only by a live-DB query. Note the failure is one level below the blind-instrument traps that section already documents: there the instrument cannot see the answer, here the fixture cannot produce it.
- **Documentation migrations get a fidelity-first review pass.** When the documenter does a large doc change (a README→`docs/` migration, a relocation), scope the review to five lenses: content **fidelity** (diff the pre-change source against the new corpus — nothing dropped or altered), **link integrity** (relative links + anchors + images resolve), **accuracy** vs the source (env vars, script names, paths), **structure**/house-style (terse README, one concern per `docs/` file, no duplication/contradiction), and **newcomer-UX** (can a new reader get running and find things following only the docs). Dispatch reviewer + fact-checker scoped accordingly; the documenter should already have self-checked fidelity/links/inbound-refs before handing off (per its role). Scale the agent count to the change size — a few lenses for a small edit, one agent per lens for a full migration.
- If the spec-keeper role exists: once blocking findings are resolved, dispatch it with the change summary plus the user-vs-AI provenance breakdown (see Step 3). It applies `specs/ai.md` updates directly and sends proposed `specs/human.md` edits back; relay those to the user for confirmation before telling the spec-keeper to apply them. The task is not complete until specs are in sync — the bar is that the code could be rebuilt from `specs/` alone, with `specs/human.md` treated as the binding contract.
- Before release (if applicable): present an end-to-end verification summary and STOP. Ask the user to confirm before spawning the release teammate.

  **That summary MUST carry a list of the VALUES this change writes into a live system that no gate can check** — usernames, service accounts, endpoints, hostnames, ports, secret key names, namespaces, cluster names. One line each: the value, the file it lands in, and the system it will address. Not "config reviewed": the literal strings.

  This is the one class of defect the whole gate is blind to by construction. Typecheck, lint, the full suite and every validator pass on a perfectly-formed value that names something which does not exist, because the repo has no instrument that can reach the system the value refers to. **The user is that instrument, and this gate is the only moment they are cheap to consult** — they either recognise the account name or they do not, in seconds, and after the release the same question costs an incident.

  Measured 2026-08-04: a device credential block shipped with a service account copied out of the spec's own illustrative example. Every gate was green through six milestones and a validator wave; the first live call after deploy returned HTTP 401. The account named was real and belonged to a different system, which is why nothing pattern-matched it as wrong. The fact-checker half of this rule is in `roles.yaml` (report such a value UNVERIFIABLE with the operator check named); this is the lead-side half, and it fires even when no fact-checker is on the roster.
- When you spawn release, put the changelog requirement in its prompt (see Step 3): the releaser must ensure the version's `CHANGELOG.md` entries are folded into the cut and that the published release page carries them.
- **Expect default-branch drift before release/merge**: long team sessions race bot pushes (Renovate dep PRs auto-merging to main, CI config bumps) AND sibling agent-team sessions merging their own PRDs (observed 2026-07-05: a sibling PRD merged between "main unchanged" verified at push time and the MR merge minutes later — conflict + a migration-number collision). A drift check at push time can be stale by merge time: re-verify divergence/mergeability IMMEDIATELY before the merge/tag action. Reconcile with a plain `git merge origin/<default>` (NEVER force-push — destroys the bot commits; avoid rebasing already-reviewed merge commits — rewrites the audited SHAs), apply any repo-specific landing conventions the drift triggers — renumber append-numbered artifacts above the merged head (goose-style migrations AND monotonic spec/doc section numbers; validated 2026-07-10: ai.md §142-146 → §160-164 after two sibling PRDs landed): the mechanical renumber goes to the merging worker as part of conflict resolution, with a follow-up audit by the role that owns the file — re-run the build gate on the merged tip, then merge/tag that tip.

### Step 5: cleanup

When the task is complete and the user is done, do NOT send `shutdown_request` blindly. Verify each teammate is at a clean stop boundary first.

#### Pre-shutdown checks (mandatory before SendMessage shutdown_request)

1. **Roster read-back, from the BRIEF first.** Read the brief's `## Roster` section: every line must read *dispatched, with a SHA* or *closed, with a reason*. A line that is neither is an undispatched step, not a skip — surface it. **Then** run `TaskList` as a cross-check only.

   The brief is the durable half and the task list is not: this file records a session where `TaskList` returned "No tasks found" at cleanup, making a mandated check unperformable and losing four written closure reasons. And observed 2026-08-03, the failure this ordering removes: a review task was created in Step 2, never dispatched, and caught at the very end only by a glance at the task list — the one store the file itself calls a coordination convenience.

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
4. Respawn the affected teammate (see slot-collision handling below). The live teammate's tool set AND model are frozen at spawn time; the patched file only takes effect on the next spawn. This also covers mid-run model changes: editing `model:` in a role file does nothing for live teammates; recycle them (graceful shutdown, then respawn), and — for an alias tier — pass the Agent tool's `model` parameter explicitly on the respawn to override regardless of role-file state. If the edited `model:` is an EXACT id (e.g. `claude-opus-4-8`), the param cannot carry it: OMIT the param on the respawn so the patched frontmatter is honored instead (Step 5).
5. After the run completes, decide whether the hotfix should be backported into the role library at `./roles.yaml` and propagated via `/agent-team update`. Flag this to the user; the team-lead does NOT edit the role library directly.

Out of scope for hotfixes: adding new roles, removing roles, changing a role's responsibilities, changing `model:`. Those are `/agent-team init`/`update` work.

**B. Slot-collision handling when respawning a teammate.**

Whether a respawn collides depends on how the previous holder ended. A graceful shutdown (shutdown_request, then `shutdown_response approve: true`, then termination) FREES the name slot: respawning with the same `name:` reuses it with no suffix (verified 2026-06-10). Force-stopping a teammate without the protocol (`TaskStop`, or a crash) leaves a stale entry in the team config (`~/.claude/teams/<session>/config.json`) with `isActive: false` that does NOT free the slot; a subsequent `Agent` spawn with the same name produces a `-N` suffix (e.g., `reviewer` becomes `reviewer-2` on first respawn, `reviewer-3` on the next). Two implications when the suffix happens:

- The live agent's NAME (for `SendMessage` targeting and `TaskUpdate` ownership) is the suffixed form. Address them as `reviewer-2`, not `reviewer`. The skill text + brief prompts must use the live name.
- The stale `reviewer` entry with `isActive: false` is harmless but clutters the roster. Optional cleanup: edit the config to remove the stale members before respawning, OR accept the suffix and move on. Do NOT wipe the team config (or call `TeamDelete` on an older build) to "reset" the team mid-run; that nukes the task list and any other live members.

If the agent is wedged (responded to `shutdown_request` with plain text instead of the protocol response, often because the tool allowlist excludes `SendMessage`), force-stop it with `TaskStop({task_id: "<teammate-name>"})` — the tool accepts a bare teammate name or agent ID. Then respawn (accepting the `-N` suffix) with the patched role file.

## Context recycling for long-running teammates

Each teammate is a full Claude Code session with its own context window (1M for Opus 4.7, smaller for Sonnet/Haiku). Over a multi-track session, the heaviest worker (usually `coder`) accumulates context fast, and the more it carries the less of what it carries is relevant to the task in hand. The lead should actively recycle teammates at clean task boundaries.

### Monitoring teammate context

**You cannot read a teammate's context budget, and no workaround recovers it.** Checked against the docs 2026-08-04, because the rules below used to prescribe a percentage:

- **No live readout exists FOR A TEAMMATE.** Your own session has one — `/context` gives *"a live breakdown by category with optimization suggestions"* — and that asymmetry is the whole problem: the instrument exists and cannot be pointed at anyone else. Stated as a conclusion, not a citation: the telemetry Claude Code exports is an asynchronous per-request export, and nothing in the documented tooling offers a live query for a running subagent's current context. **The docs contain no sentence saying this** — I checked, after quoting one that did not exist (see the note at the end of this list).
- **`TaskGet` and `TaskList` expose no token fields**, and `TaskOutput` is deprecated and steers you away from a local agent's `.output` (quoted in full under *What IS observable* below, rather than paraphrased twice).
- **OpenTelemetry cannot distinguish your teammates from each other.** `claude_code.token.usage` carries `query_source: "subagent"` and an `agent.name`, but the rendered page reads `Other user-defined agent names are replaced with` followed by a backticked `"custom"` (the source wraps it, so grep the rendered text, not this line), and every agent-team role is user-defined. All N teammates land in one bucket.
- **A percentage is model-relative anyway**: *"a subagent's context window is sized by its own model, not the parent's."* So one number does not denote one amount across a mixed-tier roster.

The old pane-statusline `% of 1000k` reading needed a visible pane, which the background model does not provide. It is not coming back: do not write a rule that depends on it.

**What IS observable, and it is enough.** Primary instrument: **count the tasks you have dispatched to a teammate since it spawned.** It comes from your own record, needs no tooling, and cannot go stale. Secondary: **grep the teammate's transcript for `compact_boundary`** (never `Read` it, per the overflow hazard above). Measured on a real host: subagent transcripts live at `~/.claude/projects/<project>/<session>/subagents/agent-<name>-<hash>.jsonl`, one file per subagent, so attribution here is clean in exactly the way OpenTelemetry's is not. The `.jsonl` above is the canonical per-subagent artifact and the thing to grep. A `.output` file is a different animal and the two are easy to conflate: `TaskOutput` (itself marked DEPRECATED) says *"Background tasks return their output file path in the tool result"* and, for local agents specifically, *"Do NOT Read the .output file — it is a symlink to the full subagent conversation transcript (JSONL) and will overflow your context window."* Note what that does and does not license: it is a reason not to READ one, not a reason not to find one, and it says nothing about where they live. Measured on one host, the only `.output` files present were background BASH output, four of them, none a symlink — consistent with the schema, which promises symlinks only for local agents. Nobody on this branch has observed a local-agent `.output`. Each event carries `compactMetadata.preTokens`, so a teammate that has compacted is one the harness says outgrew its window, rather than one you estimated. Treat the grep as a bonus signal; the count is what the rules below actually run on.

**A note on the first bullet, kept because it is the point.** Its original form carried a direct quotation attributed to the monitoring docs. That sentence does not appear on any of the 174 pages of the documentation; it was the prose a summarising fetch tool produced when asked to answer a question about the page, and it got quoted as though it were the page. The conclusion survived the check; the citation never existed. **A fetch tool's answer is not a source** — if you are going to put something in quotation marks, grep the bytes, because quotation marks are exactly what stops the next reader from doing so.

**And note what auto-compaction implies.** Subagents compact automatically on the same logic as the main conversation, so a teammate does not fall off a cliff at some threshold, it compacts and continues. Recycling is therefore about the RELEVANCE of carried context, not about rescuing a session from overflow.

### When to recycle (heuristic)

**Mandatory pre-dispatch check.** Before handing a teammate a NEW task (a fresh scope, not a fix or iteration on the task it just did), **count the NEW tasks you have dispatched to it since it spawned** — new tasks, not dispatches, so a run of fix rounds on one task counts once. Recycle before dispatching once that count reaches **two**, i.e. before handing it a third: have it write a checkpoint, then shut it down and respawn fresh. A teammate has no in-place `/clear` the lead can trigger remotely, so checkpoint + shutdown + respawn-fresh IS the clear. The reason is relevance, not size: a new task rarely needs the prior task's working memory, and carrying two tasks' worth of now-irrelevant context degrades output. The heaviest worker (usually `coder`) reaches this within a milestone or two, so expect to recycle it at most new-task boundaries.

The count is a proxy and will be wrong for a teammate whose single task was enormous. That is the cost of using an instrument that exists; the percentage it replaced could not be read at all, so it was a check nobody could perform. If you have the teammate's transcript path, a `compact_boundary` hit is stronger evidence than the count and overrides it.

**Force-recycle regardless of coupling** when a teammate has **compacted at least once** (grep its transcript for `compact_boundary`), or when it reports its own degradation, or when you can no longer reconstruct what it is carrying. Even on a same-track continuation, externalize via checkpoint and respawn. Note this is weaker than the `>85%` figure it replaces on TWO axes, and the second matters more. It is weaker in rationale, because auto-compaction means the session is not at risk of dying, so what you protect is the quality of a teammate now reasoning over a summarised history. And it is weaker in TIMING, which is the half that changes how you use it. The structural statement needs no numbers and is the part to plan around: the trigger is necessarily post-hoc, since it can only be true after the summarisation you wanted to avoid. It is the honest trade for an instrument that exists over one that did not. For colour rather than for planning: on one host, four subagent auto-compactions carried `preTokens` of roughly 975k-1002k, so it fires late. Do not convert that to a percentage — nobody measured those agents' window sizes, and one reading exceeded 1,000,000, which is its own argument against reading `preTokens` as a fraction of anything. A fifth event on that host, a MANUAL compaction of a main conversation, sat at 354k.

**Do NOT recycle (keep the teammate, ignore the task count) when:**
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
     model: "<alias tier from the role file>",   // OMIT this line if the role pins an exact model id — see Step 5
     prompt: "Cold-start. Read .claude/agent-team-tasks/<slug>-checkpoint.md for prior context. Then: <new task>."
   })
   ```

The respawned teammate starts fresh; the checkpoint is the bridge.

### Anti-patterns

- **Recycle at NEW-task boundaries, not within a task or for same-task fixes.** Each recycle costs a cold-start prompt + checkpoint-write effort + risk of losing context that wasn't externalized. The task count gates *new-task* dispatch; it does not license recycling mid-task or per-fix.
- **Recycling without a checkpoint** orphans whatever the teammate knew that wasn't in the file system.
- **Recycling mid-task** loses working state and forces re-discovery.
- **Recycling reviewer/auditor between dispatches** is usually wasteful: they're typically dispatched once per task they're reviewing, finish their report, and idle. Their context stays low. Coder is the one to watch.

### Lead's own context

The lead's context grows from teammate wrap-up reports too. The lead cannot self-recycle without losing the team. Unlike a teammate's, the lead's OWN budget is readable: run `/context`. If it is >70%, consider asking teammates to write more terse reports referencing files rather than pasting content inline, and prune lead-side conversation by summarizing into a CLAUDE.md or memory file before context fills.

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

Use when `/agent-team reflect` is invoked, or when the user accepts the end-of-run offer (Step 5 cleanup). Purpose: turn what THIS session revealed into concrete improvements — to the ROLES the team is built from, and to the WORKFLOW this skill prescribes — without silently changing anything.

**Reflect covers `SKILL.md` itself, not only `roles.yaml`.** A session's worst failures are usually not "a role's prompt was weak" but "the flow put the wrong step first", and a reflect pass that can only propose role-body edits will file a workflow defect as a role defect and fix neither. Established 2026-08-02 (a downstream project): the run's largest cost was that Mode 3 Step 3 prescribed spawning the implementer before the design was settled, with validators priming to idle. No role body was wrong. Every role performed well. The ORDER was wrong, and only a SKILL.md edit could fix it.

Two cautions when proposing a SKILL.md change, both learned from that same session:

- **Prefer a change to the ORDER OF OPERATIONS over another warning.** This file already documented mid-turn crossing at length, and the lead read it and crossed twice anyway. Past some size, added prose reduces compliance rather than improving it — only a step that must be executed actually binds. If the proposal is "add a caution about X", ask what step would make X impossible instead.
- **Renumbering steps breaks cross-references, and they are not where you would guess.** Mode 3's steps are cited from **Autonomy** (Step 4, Step 5, Step 6.A), **Mode 4** (Step 5 cleanup, Step 6.A), **Gotchas** (Step 6.A) and **PRD-task transitions: fresh team per task** (Step 5). The workflow-doc template cites **no** step at all, despite being the longest block in the file. Re-derive the list with `grep -n 'Step [0-9]'` before touching any heading rather than trusting this sentence — it was wrong in both directions on first writing, listing the template and omitting Gotchas and PRD-task transitions. Fold a new rule into the step that already owns the decision instead of inserting a numbered step between two existing ones.

Run it as a dedicated read-only reviewer subagent so the critique is not colored by the lead's own in-session choices. Spawn via `Agent(subagent_type: "researcher")` if that role exists in the repo, else any read-only reviewer/general agent. Give it in the prompt:

- the repo's current `.claude/agents/*.md` (roster, `version:`s, and each `## For this repo` tail),
- the library at `./roles.yaml` (source of truth for generic bodies + current versions),
- a summary of THIS session: what the team did, where a teammate struggled, missing-context surfaces, any mid-run role-file hotfix (Step 6.A), and tasks that had no good owner.
- **the durable brief's path and the work branch name**, so item 7 below has something to read. Without these the pass can describe the session but cannot measure it.

Ask it to return a structured proposal — findings only, no file edits:

1. **Refactors** to existing roles — a concrete body/description/tools change, WHY this session motivated it, and which role `version:` should bump.
2. **New roles** — name, one-line description, and the roster gap it fills (drawn from work the team handled awkwardly). Propose only roles that would RECUR, not one-offs.
3. **Stale roster** — agents whose file `version:` trails `roles.yaml` (the on-load staleness pass, restated with specifics).
4. **Library-worthy vs repo-local** — for each refactor, whether it is repo-specific (belongs in that agent's `## For this repo` tail) or generic (belongs in `roles.yaml`, and should then propagate to every repo plus the downstream builtin roster).
5. **Workflow changes to `SKILL.md`** — where the FLOW, not a role, caused the cost: a step in the wrong order, a decision made too early or too late, a channel used for the wrong kind of content, a validator dispatched against a moving target. State the step by name, what it currently prescribes, what it should prescribe, and the observed cost of the current form. A proposal here needs the same evidence bar as a role change: what happened, what it cost, and what would have prevented it.
6. **Knowledge that belongs somewhere ELSE** — findings this session produced that are durable but are neither role nor workflow: a repo-specific trap for that project's `CLAUDE.md`, a gate caveat for `.claude/agent-team.md`'s Quality gates block, an ADR-worthy decision. Reflect's job is to route these, not to absorb them. A general lesson filed into a skill nobody reads while working on that repo is a lesson lost.

   **A RULE THAT HAS FAILED TWICE MIGRATES; IT DOES NOT GROW.** When a session's "finding" turns out to restate a rule already written in `.claude/agent-team.md`, the finding is not new knowledge — **the failure is the CHANNEL**. Do not propose a second copy, a sharper wording, or another paragraph of evidence. Propose moving the rule's operative sentence into the ROLE BODY of whoever must keep it (`roles.yaml`), leaving the evidence in the manifest with a pointer.

   The mechanism is structural, not a matter of diligence: **teammates cold-start and read their role file; nobody reads the manifest.** This file says so itself in Step 4 ("teammates cold-start and never read this file themselves"), and it is measurable — `grep -rn 'agent-team.md' .claude/agents/*.md` in one mature repo returned **one** hit, a passing citation. So a rule living only in the manifest is unenforced on teammates **by construction**, however well written.

   Measured 2026-08-02 (a downstream project), twice in one session and independently: the manifest already carried *"STAGE BY PATH. Never `git add -A`…"* (added a week earlier) and *"Apply the screen PER CLAIM… the credibility is borrowed from the neighbour"* (added twelve days earlier). The coder then did `git add -A` and swept another agent's file; the reviewer independently re-derived the credibility rule from its own two misses and reported it as a new finding. Both rules were right, both were present, neither bound. In the same window that manifest had grown from 163 to 1948 lines. **More prose in a file nobody is required to read cannot fix a rule that is already in it.**

7. **Concurrency and wall clock — the numbers, from the artifacts that survived the run.** Every other item on this list is a judgement; this one is arithmetic, and it is the only item that can tell a later reader whether a workflow change helped or merely felt better. Report:

   - **Unit split**: the `units:` line from the brief's `## Roster` section, and the number of implementation tasks the run actually created. A `units: 3` next to one implementation task is a finding in itself. **Source that second number from the BRIEF's dispatch record, not from `TaskList`** — the task list is documented-volatile and this pass runs at the very end, which is exactly when it has been observed empty; a `TaskList` returning nothing yields "0 implementation tasks", which is a false measurement rather than a missing one. If only the volatile source is available, label the figure `recalled` per the rule below.
   - **Peak concurrency**: the largest number of teammates simultaneously dispatched-and-not-yet-reported, per the `## Roster` lines and the dispatch record.
   - **Wall clock**: first dispatch to the last gate on the integrated tree, from `git log --format='%h %aI %s' <base>..<work-branch>`, plus the span of each implementation lane.
   - **Idle implementer time**: spans where the coder held no dispatched work while a validator wave ran. This is what Step 4's pipelining rule targets, so it is the number that says whether the rule fired.
   - **Gate flakiness**: how many gate runs went red then green on retry with no change in between. Concurrency buys wall clock and spends it on contention, and a gate that reddens intermittently is weaker verification than a slow one. A run that got faster while acquiring a flaky gate did not improve; without this figure the report cannot tell the two apart.

   **Label every figure `measured` or `recalled`, and never blend them into one range.** Commit timestamps are measured; dispatch and completion times are the lead's recollection unless they were written down, and the task list is documented-volatile — this file records a session where `TaskList` returned "No tasks found" at cleanup. A recalled figure is still worth reporting; a recalled figure presented as measured is exactly the defect the rest of this file exists to prevent, and it would be self-inflicted here.

   **Report the numbers even when nothing changed, and especially then.** A run with peak concurrency 1 on a single-unit task is the correct outcome, not a null result, and it is the baseline every multi-unit run gets compared against. Skipping the item because the session was ordinary is how the comparison never becomes possible.

Present the proposal to the user as a numbered list and STOP. Apply nothing without confirmation. On acceptance:

- **Repo-specific change** → edit the agent's `## For this repo` tail only.
- **Workflow change** → edit `agent-team/SKILL.md` in the skills repo (a library change, gated on the user exactly like `roles.yaml`), then regenerate. Never edit the generated copy under `~/.claude/commands/`; it is overwritten.

  **REGENERATING FROM A `--repo` SOURCE NEEDS `dot-ai prompts refresh` FIRST, OR IT SILENTLY SHIPS STALE CONTENT.** The server caches the source repo, so `dot-ai skills generate … --repo <url>` will **rewrite every generated file** (fresh mtimes, `Skills generated successfully`, exit 0) from the cached copy — a success report over a no-op. Measured 2026-08-02: a push landed on `origin/main` with four role `version:` bumps, `git show origin/main:agent-team/roles.yaml` confirmed them on the remote, and the regenerate still produced the OLD versions; `dot-ai prompts refresh` then made the same command correct. **Verify by content, not by the success line** — read a `version:` you just bumped, or grep a string you just added, out of the generated copy.
- **Elsewhere-knowledge** → propose the edit to the owning file (the project's `CLAUDE.md`, `.claude/agent-team.md`, an ADR) and say plainly which file, so the user approves a specific change rather than a category.
- **Generic change** → edit `roles.yaml` (bump that role's `version:`), then run the `update` merge for THIS repo and raise the builtin-sync proposal (Mode 2). Editing `roles.yaml` is a library change — gated on the user per the Autonomy section.
- **New role** → add to `roles.yaml` at `version: 1` (a library change, gated); or, if it is a one-repo experiment, write it only into this repo's `.claude/agents/` and say so explicitly.

Reflect NEVER runs automatically at session end (no hook exists for that) — it is always an explicit command or an accepted offer.

## Role library reference

The full role library is in `./roles.yaml`. Read it during init/update to see the canonical role descriptions, default tool allowlists, and `triggers_on` patterns. The library may evolve over time; the skill always reads it fresh.

Besides this file, four others ship with the skill:

| file | what it is |
|---|---|
| `./roles.yaml` | the role library — versioned generic bodies, descriptions, tool allowlists, `triggers_on` |
| [`./manifest-template.md`](manifest-template.md) | the template copied into a repo as `.claude/agent-team.md` at init |
| [`./scripts/sync.py`](scripts/sync.py) | the check/diff/apply tool for the staleness pass and the Mode 2 merge |
| [`./scripts/test_sync.py`](scripts/test_sync.py) | its regression suite — `python3 agent-team/scripts/test_sync.py`, stdlib `unittest`, no pytest |

`sync.py` needs PyYAML and nothing else; it refuses to run without it rather than hand-parsing `roles.yaml`, whose block scalars a partial parse would silently read as clean bodies it never saw.

Every test in `test_sync.py` is a defect review found in the first draft of `sync.py`, written as a fixture because that is the form that would have caught it. Add one there before fixing anything in `sync.py`.

## Gotchas

- **Permission-classifier outage blocks mutating Bash temporarily** (observed 2026-07-15): the auto-mode safety classifier can go briefly unavailable, failing every state-changing Bash call ("temporarily unavailable, so auto mode cannot determine the safety") while read-only ops keep working. Don't spin on retries: interleave the non-Bash steps of your plan meanwhile (protocol shutdown_requests, file edits via Edit/Write, SendMessage coordination), wait ~1 min, then retry the blocked command — it recovered on the second window.

- **Old team API (`TeamCreate` / `team_name` / `TeamDelete`) — superseded by the implicit team.** Mode 3 above is written for the implicit-team API: the session has ONE implicit team, so you spawn with `Agent({name, subagent_type, model, prompt})` (optionally `run_in_background: true`), coordinate via `SendMessage` (target by teammate name) + the `Task*` tools, and retire a teammate with a graceful `shutdown_request` → `shutdown_response approve:true`. There is nothing to create or delete. Confirmed on Claude Code 2.1.178 (2026-06-16) through 2.1.215, where the Agent tool documents `team_name` as "Deprecated; ignored. The session has a single implicit team" and exposes no `TeamCreate`/`TeamDelete`. If you are on an OLDER build that still has them: call `TeamCreate({team_name, agent_type, description})` once before the first spawn, pass that `team_name` on every `Agent` spawn/respawn, and `TeamDelete` at the very end (only after all members confirm shutdown, never with an active member). Everything else in Mode 3 is identical.
- **`/clear` does not clear the team registry.** After a `/clear`, the session's `~/.claude/teams/<session>/config.json` still lists every pre-clear member, and those agents can be alive, idle, and WAKEABLE. Two observed effects (2026-07-03): (a) spawning a teammate with a role name a stale slot holds silently suffixes the new agent (`coder` → `coder-2`); (b) `TaskUpdate({owner: "coder"})` intended for the fresh spawn woke the STALE pre-clear `coder`, which read the on-disk brief and started implementing in the same worktree as the fresh `coder-2` — two writers in one worktree, caught only because both self-reported foreign uncommitted files. Rules: before run-mode spawns, list the session's team members and treat any entry you did not spawn in THIS conversation as a stale-slot hazard; always address teammates AND set task `owner` using the exact live name returned by the spawn result, never the bare role name; if a stale agent wakes, stop its writes first, then graceful-shutdown it.
- **A `teammate_terminated` notice can be FALSE — treat any termination you did not protocol-confirm as unconfirmed** (observed 2026-07-05): the system announced "coder has shut down", the lead verified the worktree clean and respawned the role (suffixed `coder-2`), and the "terminated" coder then kept working and committed a full milestone ~25 min later — two writers in one worktree. Rules: only your own `shutdown_request` answered by `shutdown_approved` proves termination; a worktree-clean check at respawn time is NOT proof (the zombie can commit later); when respawning a role into the same worktree, put a concurrent-writer guard in the spawn prompt ("if foreign uncommitted changes or unexplained commits appear, STOP writing and report") — that guard is what caught it; if the zombie surfaces, protocol-shutdown it and freeze the legitimate writer until `shutdown_approved` arrives; the zombie's COMMITTED work is salvage, not waste — verify it adversarially and adopt it rather than reimplementing. Name-reuse corollary: a fresh spawn that reuses the dead-looking holder's name can receive that name's PENDING stale `shutdown_request` and get killed mid-task — after any same-name respawn, verify the new agent's task actually completed (check the artifact, not the report) before relying on it.
- **Two writers in one worktree — containment and recovery** (observed 2026-07-03, same incident as the `/clear` gotcha; both writers were individually careful and it still took three rounds to contain):
  - A STOP/HOLD message can cross an agent's in-flight turn (mailboxes are read between turns): the woken agent acknowledged a HOLD, then resumed and committed anyway. Treat any stop as UNCONFIRMED until the protocol `shutdown_approved` arrives — and until it does, freeze the LEGITIMATE writer too. One live + one "stopped" writer still interleaved a sanctioned reset+cherry-pick with a fresh commit, leaving the branch mid-conflict.
  - The lead inspects (`git log`/`status`/`reflog`) but NEVER runs state-changing git in a worktree with a **live writer** — contested or not; see Step 3's ref-moving rule, and note "contested" was the original wording and is the wrong predicate, since a single-writer tree is exactly where a lead feels entitled to reach in. A "helpful" abort makes a third writer. The reflog is the ground truth for reconstructing who did what, and commits reset off the branch remain recoverable by SHA.
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
- **Frontmatter `model:` handling is ALIAS-vs-EXACT-ID, and was measured HONORED on 2.1.226 — verify when it matters.** Omitting the Agent tool's `model` param honors the agent-definition frontmatter (measured 2026-08-08 on 2.1.226: frontmatter `claude-opus-4-8` ran `claude-opus-4-8[1m]`, `sonnet` ran `claude-sonnet-5`); passing the param overrides it, and the param's enum is alias-only (`sonnet | opus | haiku | fable`) so it CANNOT carry an exact ID. This REVERSES an earlier observation (2026-07-05: a `model: sonnet` documenter ran on the parent's model when spawned without an override) — the behavior changed across builds, so trust neither state blind: for an alias tier pass it explicitly, for an exact-ID pin OMIT the param, and verify the spawned model (probe the agent's declared model ID) when the exact model matters. Never pin the smallest tier (`haiku`) for any role (auto-mode gating — verify the current capable-model list in the Claude Code docs, don't trust a version list). See the Step 5 model note.
- **New `.claude/agents/<role>.md` files ARE spawnable mid-session** (observed 2026-07-05: a role added to the repo mid-run spawned via `subagent_type` without restarting the session) — adding a role does not require a new session, only the file.
- **Idle is normal**: teammates go idle after every turn. Do not interpret idle as "done" or "stuck". Only act when a teammate sends a message or completes a task.
- **Tasks vs SendMessage**: use TaskUpdate to mark progress (shared task list); use SendMessage for human-readable communication. Do not send structured JSON status payloads via SendMessage.
- **The shared task list can vanish mid-run** (observed: lead's `TaskUpdate` returned "Task not found" and a teammate saw its task entry disappear, mid-session, with the team still healthy). Treat git state + SendMessage reports as the source of truth; the task list is a coordination convenience. Teammates should report findings via SendMessage directly when their task entry is missing instead of stalling, and the lead should not block any flow step on task-list bookkeeping succeeding.
- **Stale duplicate message re-deliveries and mid-turn crossing.** Mechanism: mailboxes are read BETWEEN turns, so a dispatch races a turn boundary and a re-delivered copy can arrive after the work is done. Symptom: an item in a multi-item message goes unactioned, or a worker is woken to re-report finished work — including by the lead's own `TaskUpdate` bookkeeping, so do that BEFORE the dispatch or skip it. Recovery: a standalone SINGLE-ITEM re-send to an idle worker, which has landed first try in every recorded instance (2026-06-12, 07-05 x2, 07-13, 07-16, 08-03). Correct worker behaviour on a stale duplicate is to re-verify live state (`git rev-parse HEAD`, the artifact) and reply with that evidence — never redo landed work; correct lead behaviour is to ack "in sync, nothing owed". **The removal, not the coping strategy, is Step 4's one-actionable-item-per-message rule — read that instead of managing this.**
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
       4. tester (sonnet) - runs the gate slots (fmt-check, clippy, cargo test)
          independently of the coder; cargo test framework in tests/.
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
