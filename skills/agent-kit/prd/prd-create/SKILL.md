---
name: prd-create
description: Creates documentation-first PRDs (a short forge issue plus a detailed prds/ file with milestones), then offers to start work, commit for later, or send the PRD to uzi. Use when the user wants to create a PRD, spec out a new feature, write a product requirements document, or turn a feature idea into a tracked GitHub/GitLab/Forgejo issue. Triggers include "create a PRD", "new PRD", "/prd-create", "write a PRD for", "spec this feature".
---

> Adapted from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) `shared-prompts/prd-create.md` (MIT, Copyright (c) 2025 Viktor Farcic). Original author: Viktor Farcic. The scoping and slicing rules draw on ideas from Matt Pocock's [mattpocock/skills](https://github.com/mattpocock/skills) (MIT): tracer-bullet vertical slices, blocking edges, and deep modules.

# PRD Creation Slash Command

## Instructions

You are helping create a Product Requirements Document (PRD) for a new feature. This process involves two main components:

1. **GitHub Issue**: Short, immutable concept description that links to the detailed PRD
2. **PRD File**: Project management document with milestone tracking and implementation plan

## Process

### Step 1: Understand the outcome (no interview)
Take the user's description and work out the outcome yourself. Do not run an interview, and never ask the user how code should be written or structured: technical choices are yours, recorded with their reason in the Decision Log.

- **Look facts up, never ask for them.** Read the code, docs, issues and existing PRDs. Check first whether the capability already exists or was proposed and rejected before (closed issues, `prds/done/`, Decision Logs).
- **Ask at most one batched question**, only for unresolved product intent or a fork whose options change what the user gets. Give a recommended answer for each. Skip it when the request is clear.
- **Existing escalations still apply unchanged**: external API contract changes, schema changes affecting existing data, auth or security-model changes, irreversible actions. Work that merely touches security code goes to the design-critique wave (agent-team), not to the user.
- **Bug PRDs** name a reproduction signal (a failing test, command or captured trace) that shows the bug, or say why none exists yet.

### Step 1.2: Scope gate (before creating anything)
**One PRD issue is one implementation run and one PR** (a uzi run, or one `/prd-full` loop; a documentation-only PRD may instead complete with a direct commit, per `/prd-done`). Milestone reshaping inside a PRD does not shrink that PR; only a smaller issue does. So size the issue, not just its milestones:

- **One PRD = one independently valuable outcome.** A slice earns its own PRD only if it is useful on its own; otherwise it stays a milestone inside a PRD.
- **Scope-review alarms** (not automatic splits): more than one independently valuable outcome, or an expected large PR (many components or a long file map). When one fires, propose a split into independently valuable PRDs with their order, as one yes/no for the user.
- **Hard error: a milestone that needs an unfinished PRD.** Move it to that later PRD or redraw the boundary; never leave it waiting inside this one.
- **A split effort gets an umbrella issue** that only indexes its child PRDs, their order and their blocking edges; each child is a normal PRD created by this skill. When the children go to uzi, default them to the `Auto` or `Seed & ship` mode so the user approves the split once, not every child plan.
- **Existing open PRDs** get the same scope review before they are dispatched (`/prd-start` runs it).

#### Split path (when the user accepts a split)

1. Ask Step 1.5's next-step and review questions **once**, before creating anything, and reuse the answers (including any uzi mode or sweep label) for every child.
2. Create the **umbrella issue**: a plain issue with no `PRD` label and no sweep label. Its body lists the child outcomes in order with their blocking edges; it holds no milestones.
3. Create each child with Steps 2-5 as a normal PRD (its own issue with the `PRD` label and its own `prds/` file), in dependency order, then add each child's link to the umbrella.
4. Apply the next step only to children whose blockers are finished. A child with unfinished blockers is committed and pushed, and nothing else: no work started, no uzi send, no sweep label. Its later activation uses the same recorded choice once its blockers close.

### Step 1.5: Capture the post-PRD workflow up front (before creating anything)
Before creating the issue or PRD, detect whether **uzi** is available (`command -v uzi` succeeds, **or** the `uzi-cli` skill is installed at `~/.claude/skills/uzi-cli/`). When uzi is available, also run `uzi schedule list --json` once to learn whether any **sweep** schedule exists and its label(s) — this gates the `Commit & push + queue for uzi sweep` option and its label choice below. Then gather **every** downstream choice now, back to back, so nothing interrupts the PRD writing later:

1. **AskUserQuestion (next step + review)**, one prompt with two questions:
   - **Next step**: `Start working now`, `Commit & push for later`, and, **only when uzi is detected**, `Commit & push + queue for uzi sweep` and `Send to uzi`. (2 options without uzi; up to 4 with.) `Commit & push + queue for uzi sweep` is a *deferred* handoff (see Option 4): it commits and pushes exactly like `Commit & push for later`, then labels the issue so a uzi **sweep schedule** implements it later, with no run started now. Offer it only when uzi is detected **and** `uzi schedule list --json` reports at least one sweep schedule.
   - **PRD review**: `No review`, `One reviewer`, or `Let the skill decide` (the skill picks a count from the PRD's size and complexity). The user can pick "Other" for an exact count.
2. **Immediately present any needed second AskUserQuestion, right after the first prompt and BEFORE the PRD is written** (do not defer to after PRD creation):
   - If the user picked `Send to uzi`: ask the uzi **mode** — load the `uzi-cli` skill and use its **Send to uzi** menu (Auto / Supervised / Seed & ship / Custom).
   - If the user picked `Commit & push + queue for uzi sweep` **and** `uzi schedule list --json` shows more than one sweep schedule: ask **which sweep label** to queue for (e.g. `Night`, `bug`). With exactly one sweep, use it and ask nothing.

Hold all answers and act on them **after** the PRD is created: run the review first, then execute the chosen next step (for `Send to uzi`, use the mode already selected here; do not re-ask). Do not present a trailing numbered menu. If running non-interactively (no user to prompt), default to `Commit & push for later` with `No review`.

**If `Send to uzi` or `Commit & push + queue for uzi sweep` is the chosen next step, the PRD must be self-contained for an offline worker** (both hand the PRD to a uzi worker — the sweep option via a scheduled sweep rather than an immediate run). uzi workers run with restricted egress: an allowlist that reaches the forge, `*.anthropic.com`, and the package caches, but **not the open web** (no arbitrary GitHub, no external docs sites, no `WebFetch`/`WebSearch`). A worker therefore cannot perform any investigation that needs the internet. So during PRD authoring (Step 5), **front-load every internet-requiring investigation now, locally, and write its findings into the PRD body as resolved facts** — never leave a milestone that says "the implementer will confirm X online." See the callout in Step 5.

### Step 2: Create GitHub Issue FIRST
Create the GitHub issue immediately to get the issue ID. This ID is required for proper PRD file naming.

**IMPORTANT: Add the "PRD" label to the issue for discoverability.**

### Step 3: Create PRD File with Correct Naming
Create the PRD file using the actual GitHub issue ID: `prds/[issue-id]-[feature-name].md`

### Step 4: Update GitHub Issue with PRD Link
Add the PRD file link to the GitHub issue description now that the filename is known.

### Step 5: Create PRD as a Project Management Document
Work through the PRD template focusing on project management, milestone tracking, and implementation planning. Documentation updates should be included as part of the implementation milestones.

> **🔴 When `Send to uzi` or `Commit & push + queue for uzi sweep` was chosen (Step 1.5): resolve internet-dependent investigations in the PRD body NOW.** The uzi worker has no open-web access (restricted egress: forge + `*.anthropic.com` + package caches only). Anything a milestone relies on that can only be learned from the open internet — external API or library semantics, upstream source you would have to browse rather than clone, a docs page, a web search, a CVE lookup — the worker cannot do, so the milestone stalls or the worker guesses. Before handoff:
> - **Do the lookup locally and bake the answer into the PRD** as a stated fact with its source, not as a task. Turn "confirm the widget API is rate-limited" into "the widget API is rate-limited at N/min per `<link>`, so M2 must back off."
> - **Prefer an offline-resolvable form** where one exists: an empirical measurement from the product's own data or logs, or a codebase read, beats an external lookup — and it lets the worker re-verify without egress. State such a check as offline in the milestone.
> - **If a load-bearing fact genuinely cannot be settled without the internet**, settle it yourself before sending, or the PRD is not ready for uzi. Flag any you could not resolve rather than shipping a milestone that silently depends on it.

#### PRD file sections

These are the sections of the `prds/` file. The "GitHub Issue Template" further down is only the short issue body, not the PRD file.

1. **Problem**: who hits it and how, from the user's side.
2. **Outcome**: what works when this PRD is done, plus 2-3 concrete acceptance examples (input, action, observable result). The user can object just by reading this.
3. **Out of scope**: what this PRD deliberately does not do, including anything moved to a later PRD.
4. **Modules and seams**: only the consequential ones. For each, the caller-facing interface (inputs, outputs, invariants, error behaviour), what complexity it hides, and where tests exercise it. Prefer existing seams and interfaces; add a new one only for a demonstrated need.
   - **A module that takes untrusted input states its resource bounds**: for each resource it lets a caller spend (stored bytes, memory after decompression, database connections and concurrency, background work, wall time), the bound and where it is enforced. Nothing irreversible that affects another owner (deleting, reclaiming, charging) happens on a declared value before it is verified.
   - **Reusing an existing component's safety story** (a clone path, an upload pattern, a limiter) states whether its accepted risks still hold for this PRD's callers; a risk accepted for admin-controlled input does not carry over to input a user or product controls.
5. **Testing decisions**: which behaviours are tested at which seam, and similar existing tests to follow.
6. **Milestones**: vertical slices, per the rules below.
7. **Decision Log**: every technical decision with its reason and the alternative rejected.

#### Milestones are vertical slices

- **Each milestone is one complete behaviour** that can be verified on its own, cutting through every layer it needs (schema, logic, API, UI, tests, docs). One fresh implementation run must be able to build and verify it.
- **Never a standalone layer milestone**: no "schema", "service layer", "tests" or "docs" milestone that only prepares a later one. Tests and docs ride inside the slice they cover.
- **Each milestone lists `Blocked by`** (only milestones that genuinely gate it, or "none") and its acceptance criteria.
- **Prefactor first.** If a refactor makes the feature easy, it is the first milestone.
- **Wide refactors are the exception.** A mechanical change with a codebase-wide blast radius (rename, retype) goes expand-contract: add the new form beside the old, migrate callers in batches, then remove the old form.
- **Migrations ride in the first slice that needs them.**
- **Every merged slice is safe on its own**: security-complete, or dark behind a default-off flag only when every reachable entry point checks the flag and migrations keep existing data and behaviour unchanged while it is off.
- **No fixed milestone count.** Size is governed by the scope gate (Step 1.2), not by a number.

Example (layer-shaped vs vertical, for "users earn points for completed lessons"):
- ❌ M1 schema; M2 points service; M3 dashboard widget; M4 tests
- ✅ M1 completing a lesson awards points and the dashboard shows the total (schema, service, widget, tests); M2 streaks extend the award and show on the dashboard (blocked by M1); M3 backfill points for past completions (blocked by M1)

#### Parallelism

Milestones whose blockers are done are **candidates** for parallel work, never a guarantee. Run two in parallel only when they own disjoint files and state; otherwise sequence them. The lead integrates, runs the gates on the combined tree and renumbers migrations where the repo requires it.

#### Acceptance is not a milestone

A PRD is done when its code is merged (a documentation-only PRD: when its docs change lands, per `/prd-done`). When the feature has behaviour only a live environment can show (a cluster, network policy, an external service), file a linked **`acceptance` issue** with the live checklist, owned by the user, with no deadline and never a uzi sweep label. It does not block merge or close. A later PRD references it, and blocks on it only when the user decides that case needs proven live behaviour.

## GitHub Issue Template (Keep Short & Stable)

**Initial Issue Creation (without PRD link):**
```markdown
## PRD: [Feature Name]

**Problem**: [1-2 sentence problem description]

**Solution**: [1-2 sentence solution overview]

**Detailed PRD**: Will be added after PRD file creation

**Priority**: [High/Medium/Low]
```

**Don't forget to add the "PRD" label to the issue after creation.**

**Issue Update (after PRD file created):**
```markdown
## PRD: [Feature Name]

**Problem**: [1-2 sentence problem description]

**Solution**: [1-2 sentence solution overview]

**Detailed PRD**: See [prds/[actual-issue-id]-[feature-name].md]([repo-web-url][forge-file-path]prds/[actual-issue-id]-[feature-name].md)

**Priority**: [High/Medium/Low]
```

## Working Through the PRD

Answer these yourself from the code and context; they are a checklist for the author, not questions for the user (see Step 1):

- **Problem and outcome**: who is affected, and what observably changes for them.
- **Existing work**: does it already exist, partly exist, or was it rejected before?
- **Seams**: which interfaces the feature crosses, and where it is tested.
- **Risks and dependencies**: what could break, what this needs from other systems, and whether any dependency is an unfinished PRD (a scope-gate error).
- **Slices**: the smallest end-to-end behaviour that proves the approach, then the slices that extend it.
- **Must-have vs nice-to-have**: nice-to-haves go to Out of scope or a later PRD.

**Forge-agnostic**: The `gh` commands below are GitHub examples. Detect the forge from `git remote get-url origin` and use the matching CLI, mapping each verb to its equivalent: **GitHub** → `gh`; **GitLab** → `glab` (a PR is a *merge request*, `glab mr …`); **Forgejo/Gitea** → `tea`. If the needed CLI is missing, tell the user and link its install page. Build `[repo-web-url]` from that same remote (strip `.git`, turn an SSH `git@host:org/repo` into `https://host/org/repo`) and `[default-branch]` from the remote's HEAD. The file path segment depends on the forge: GitHub `/blob/<branch>/`, GitLab `/-/blob/<branch>/`, Forgejo/Gitea (including Codeberg) `/src/branch/<branch>/`.

**Note**: If creating the GitHub issue fails because the "PRD" label does not exist, create the label first (`gh label create "PRD" --description "Product Requirements Document" --color 0052CC`) and then retry creating the issue.

## Workflow

1. **Concept Discussion**: Get the basic idea and validate the need
2. **Create GitHub Issue FIRST**: Short, stable concept description to get issue ID
3. **Create PRD File**: Detailed document using actual issue ID: `prds/[issue-id]-[feature-name].md`
4. **Update GitHub Issue**: Add link to PRD file now that filename is known
5. **Write the sections**: fill each PRD file section yourself (see "Working Through the PRD")
6. **Milestone Definition**: vertical slices with `Blocked by` edges and acceptance criteria; the scope gate (Step 1.2), not a count, bounds the PRD
7. **Review & Validation**: Ensure completeness and clarity

**CRITICAL**: Steps 2-4 must happen in this exact order to avoid the chicken-and-egg problem of needing the issue ID for the filename.

## Update ROADMAP.md (If It Exists)

After creating the PRD, check if `docs/ROADMAP.md` exists. If it does, add the new feature to the appropriate timeframe section based on PRD priority:
- **High Priority** → Short-term section
- **Medium Priority** → Medium-term section
- **Low Priority** → Long-term section

Format: `- [Brief feature description] (PRD #[issue-id])`

The ROADMAP.md update will be included in the commit at the end of the workflow (Option 2).

## After PRD Creation: review, then the chosen next step

The **next step** and **PRD review** choices were captured up front (Step 1.5, via AskUserQuestion, before anything was created). Now that the PRD file and issue exist, act on them in order: run the review first (if requested), then execute the chosen next step. Show the confirmation:

```
✅ PRD Created Successfully!

**PRD File**: prds/[issue-id]-[feature-name].md
**Issue**: #[issue-id]
```

### PRD Review (if requested)

If the user asked for review, spawn reviewer agent(s) with the **Agent** tool (`subagent_type: Explore` or `general-purpose`) to read `prds/[issue-id]-[feature-name].md` and critique it: scope (one independently valuable outcome; no milestone that needs an unfinished PRD), vertical slicing (no standalone layer milestones; real `Blocked by` edges; each slice fits one fresh run), testability at the named seams, for untrusted input, resource bounds, declared-value verification before any irreversible cross-owner action, and reused safety stories (Modules and seams), clarity, missing risks and dependencies. When the repo has an agent-team `architect` role, make it one of the reviewers.

- **One reviewer**: a single agent.
- **Let the skill decide**: pick the count from the PRD's size and complexity: 1 for a small single-component PRD, 2-3 for a large or multi-component one, each agent taking a distinct lens (scope/feasibility, milestones/testability, risks/dependencies). Run them in parallel.
- **A specific number**: spawn exactly that many, dividing the lenses among them.

Collect the findings, present them to the user, and apply the fixes they approve to the PRD file. Then continue to the chosen next step below.

### Option 1: Start Working Now

If the user picked **Start working now**, first commit and push the PRD (same as **Commit & push for later**), then instruct them:

---

**PRD committed and pushed.**

To start working on this PRD, run `/prd-start [issue-id]`

---

### Option 2: Commit and Push for Later

If the user picked **Commit & push for later**:

> **Commit the PRD straight to `main` — no PR, no feature branch.** This overrides any "branch first on the default branch" rule; branches and PRs are for the `prd-start` implementation, not the PRD file.

```bash
# Stage the PRD file (and ROADMAP.md if it was updated)
git add prds/[issue-id]-[feature-name].md
# If docs/ROADMAP.md exists and was updated, include it:
# git add docs/ROADMAP.md

# Commit with skip CI flag to avoid unnecessary CI runs
git commit -m "docs(prd-[issue-id]): create PRD #[issue-id] - [feature-name] [skip ci]

- Created PRD for [brief feature description]
- Defined [X] major milestones
- Documented problem, solution, and success criteria
- Added to ROADMAP.md ([timeframe] section)
- Ready for implementation"

# Pull latest and push to main
git pull --rebase origin main && git push origin main
```

**Confirmation Message:**
```
✅ PRD committed and pushed to main

The PRD is now available in the repository. To start working on it later, execute:
prd-start [issue-id]
```

### Option 3: Send to uzi (hand off to the uzi-cli skill)

**Only offer this option when uzi is available** (`command -v uzi` succeeds, or the `uzi-cli` skill is installed). Do not re-implement the uzi flow here. Hand the freshly created PRD to the `uzi-cli` skill's **Send to uzi** orchestration, using the mode already chosen up front in Step 1.5 (Auto / Supervised / Seed & ship / Custom), and drive the run from there.

0. **Pre-flight: the PRD must be internet-independent.** The worker has no open-web egress (see Step 5's callout), so re-scan the PRD for any milestone whose completion needs the open internet and resolve it into the body first. If a load-bearing external fact is still unresolved, settle it now or tell the user the PRD is not yet ready for uzi.
1. **Commit and push the PRD first** (exactly as Option 2) so the issue and `prds/` file are on the remote for the uzi worker to clone. Capture the pushed commit for the seeded path: `PRD_SHA=$(git rev-parse HEAD)`.
2. **Confirm uzi tracks this repo.** Load the `uzi-cli` skill (Skill tool) if not already loaded, then run `uzi repo list --json` and note the repo `id`. If the repo is not listed, tell the user it is not registered with uzi and fall back to Option 2.
3. **Hand off to the uzi-cli skill's *Send to uzi* section**, giving it this PRD's coordinates: repo `id`, issue `#[issue-id]`, and, for a seeded run, `--planned-commit "$PRD_SHA"`. Use the mode already chosen in Step 1.5:
   - **Auto / Supervised / let uzi plan it**: uzi plans from the pushed PRD issue, so no local plan is needed.
   - **Seed & ship**: write the plan locally from the PRD's milestones and technical scope (the uzi-cli *Authoring a seeded plan* section is the guide), then seed it.
4. **If uzi rejects the issue for a missing `PRD` label** even though this skill added it, its poller has not synced yet. Use the forge's **Promote** action on the issue (it writes the label and refreshes uzi's cache in one request), then retry.

If the installed `uzi-cli` skill predates its *Send to uzi* section (older uzi binary), fall back to its *Authoring a seeded plan* section for the seeded path, or `uzi run create --repo <id> --issue [issue-id]` for the gated path.

### Option 4: Commit & push + queue for a uzi sweep

**Only offer this when uzi is available AND `uzi schedule list --json` reports at least one sweep schedule.** It is the *deferred* sibling of Option 2: unlike **Send to uzi** (which starts a run now), it commits and pushes the PRD and then labels the issue so a uzi **sweep schedule** implements it on its own cadence (for example a nightly sweep). No run is started here.

0. **Pre-flight: the PRD must be internet-independent** (a sweep runs an offline worker, same as Option 3). Re-scan the PRD and resolve any open-web dependency into the body first; if a load-bearing external fact is unresolved, settle it or tell the user it is not yet ready.
1. **Commit and push the PRD first** (exactly as Option 2), so the issue and `prds/` file are on the remote before the sweep fires.
2. **Discover the sweep label — never hardcode it.** From the `uzi schedule list --json` read in Step 1.5, take the schedules whose target/kind is `sweep` and read their `labels`:
   - exactly one sweep → use its label;
   - several (e.g. `Night`, `bug`) → use the label chosen up front in Step 1.5;
   - none → there is nothing to queue for; fall back to **Option 2** and say so.
3. **Apply the label to the issue** with the forge CLI already detected for this repo (`glab` / `gh` / `tea`), keeping the `PRD` label the skill added. GitLab example:
   ```bash
   glab issue update [issue-id] --label "[sweep-label]"
   ```
4. **Do not start a run.** Confirm to the user that the PRD is pushed and the issue is labeled for the `[sweep-label]` sweep, which will pick it up on its schedule.

This runs the `uzi` **binary** directly (like Option 3's `uzi repo list --json`); the `uzi-cli` **skill** does not need to be loaded, and no label name is baked into this skill.

## Important Notes

- **Option 1**: Best when you have time to begin implementation immediately
- **Option 2**: Best when creating multiple PRDs or planning future work
- **Option 3 (send to uzi)**: hands the PRD to the `uzi-cli` skill's **Send to uzi** menu, which picks how much to automate (Auto / Supervised / Seed & ship / let uzi plan it) and explains the budget tradeoff. A seeded run uses uzi's global default budget (keep the plan small), while the gated path scales the budget to the milestones uzi freezes (fits large or multi-component PRDs).
- **Option 4 (queue for a uzi sweep)**: the deferred sibling of Option 2 — commits & pushes the PRD, then labels the issue for a uzi **sweep schedule** (label discovered at runtime via `uzi schedule list --json`, never hardcoded) so a scheduled sweep implements it later, with no run started now. Offered only when uzi is detected and a sweep schedule exists.
- **No PR for a PRD**: commit straight to `main` (Option 1/2), never a feature branch or PR. Overrides the general "branch first" rule; branches/PRs are for the `prd-start` implementation.
- **Skip CI flag**: Always use `[skip ci]` when committing PRD-only changes
- **Issue reference**: Include issue number in commit message for traceability
