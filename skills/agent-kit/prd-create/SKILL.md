---
name: prd-create
description: Create documentation-first PRDs that guide development through user-facing content
---

> Vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) `shared-prompts/prd-create.md` (MIT, Copyright (c) 2025 Viktor Farcic). Original author: Viktor Farcic.

# PRD Creation Slash Command

## Instructions

You are helping create a Product Requirements Document (PRD) for a new feature. This process involves two main components:

1. **GitHub Issue**: Short, immutable concept description that links to the detailed PRD
2. **PRD File**: Project management document with milestone tracking and implementation plan

## Process

### Step 1: Understand the Feature Concept
Ask the user to describe the feature idea to understand the core concept and scope.

### Step 1.5: Capture the post-PRD workflow up front (before creating anything)
Before creating the issue or PRD, detect whether **uzi** is available (the `uzi` CLI is on `PATH`, i.e. `command -v uzi` succeeds, **or** the `uzi-cli` skill is installed at `~/.claude/skills/uzi-cli/`), then use the **AskUserQuestion** tool to capture two choices in a single prompt:

1. **Next step** (what happens once the PRD exists): `Start working now`, `Commit & push for later`, and, **only when uzi is detected**, `Send to uzi`. Present 2 options without uzi, 3 with. `Send to uzi` hands the PRD to the `uzi-cli` skill's own **Send to uzi** menu (Auto / Supervised / Seed & ship / Custom), so do not ask the uzi mode here; that second menu picks it.
2. **PRD review**: whether agent(s) review the freshly created PRD: `No review`, `One reviewer`, or `Let the skill decide` (the skill picks a count from the PRD's size and complexity). The user can pick "Other" to name an exact number.

Hold both answers and act on them **after** the PRD is created (review first, then the chosen next step); do not present a trailing numbered menu. If running non-interactively (no user to prompt), default to `Commit & push for later` with `No review`.

### Step 2: Create GitHub Issue FIRST
Create the GitHub issue immediately to get the issue ID. This ID is required for proper PRD file naming.

**IMPORTANT: Add the "PRD" label to the issue for discoverability.**

### Step 3: Create PRD File with Correct Naming
Create the PRD file using the actual GitHub issue ID: `prds/[issue-id]-[feature-name].md`

### Step 4: Update GitHub Issue with PRD Link
Add the PRD file link to the GitHub issue description now that the filename is known.

### Step 5: Create PRD as a Project Management Document
Work through the PRD template focusing on project management, milestone tracking, and implementation planning. Documentation updates should be included as part of the implementation milestones.

**Key Principle**: Focus on 5-10 major milestones rather than exhaustive task lists. Each milestone should represent meaningful progress that can be clearly validated.

**Consider Including** (when applicable to the project/feature):
- **Tests** - If the project has tests, include a milestone for test coverage of new functionality
- **Documentation** - If the feature is user-facing, include a milestone for docs following existing project patterns

**Good Milestones Examples:**
- [ ] Core functionality implemented and working
- [ ] Tests passing for new functionality (if project has test suite)
- [ ] Documentation complete following existing patterns (if user-facing feature)
- [ ] Integration with existing systems working
- [ ] Feature ready for user testing

**Avoid Micro-Tasks:**
- ❌ Update README.md file
- ❌ Write test for function X
- ❌ Fix typo in documentation
- ❌ Individual file modifications

**Milestone Characteristics:**
- **Meaningful**: Represents significant progress toward completion
- **Testable**: Clear success criteria that can be validated
- **User-focused**: Relates to user value or feature capability
- **Manageable**: Can be completed in reasonable timeframe

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

**Detailed PRD**: See [prds/[actual-issue-id]-[feature-name].md](https://github.com/vfarcic/dot-ai/blob/main/prds/[actual-issue-id]-[feature-name].md)

**Priority**: [High/Medium/Low]
```

## Discussion Guidelines

### PRD Planning Questions
1. **Problem Understanding**: "What specific problem does this feature solve for users?"
2. **User Impact**: "Walk me through the complete user journey — what will change for them?"
3. **Technical Scope**: "What are the core technical changes required?"
4. **Documentation Impact**: "Which existing docs need updates? What new docs are needed?"
5. **Integration Points**: "How does this feature integrate with existing systems?"
6. **Success Criteria**: "How will we know this feature is working well?"
7. **Implementation Phases**: "How can we deliver value incrementally?"
8. **Risk Assessment**: "What are the main risks and how do we mitigate them?"
9. **Dependencies**: "What other systems or features does this depend on?"
10. **Validation Strategy**: "How will we test and validate the implementation?"

### Discussion Tips:
- **Clarify ambiguity**: If something isn't clear, ask follow-up questions until you understand
- **Challenge assumptions**: Help the user think through edge cases, alternatives, and unintended consequences
- **Prioritize ruthlessly**: Help distinguish between must-have and nice-to-have based on user impact
- **Think about users**: Always bring the conversation back to user value, experience, and outcomes
- **Consider feasibility**: While not diving into implementation details, ensure scope is realistic
- **Focus on major milestones**: Create 5-10 meaningful milestones rather than exhaustive micro-tasks
- **Think cross-functionally**: Consider impact on different teams, systems, and stakeholders

**Forge-agnostic**: The `gh` commands below are GitHub examples. Detect the forge from `git remote get-url origin` and use the matching CLI, mapping each verb to its equivalent: **GitHub** → `gh`; **GitLab** → `glab` (a PR is a *merge request*, `glab mr …`); **Forgejo/Gitea** → `tea`. If the needed CLI is missing, tell the user and link its install page.

**Note**: If creating the GitHub issue fails because the "PRD" label does not exist, create the label first (`gh label create "PRD" --description "Product Requirements Document" --color 0052CC`) and then retry creating the issue.

## Workflow

1. **Concept Discussion**: Get the basic idea and validate the need
2. **Create GitHub Issue FIRST**: Short, stable concept description to get issue ID
3. **Create PRD File**: Detailed document using actual issue ID: `prds/[issue-id]-[feature-name].md`
4. **Update GitHub Issue**: Add link to PRD file now that filename is known
5. **Section-by-Section Discussion**: Work through each template section systematically
6. **Milestone Definition**: Define 5-10 major milestones that represent meaningful progress
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

If the user asked for review, spawn reviewer agent(s) with the **Agent** tool (`subagent_type: Explore` or `general-purpose`) to read `prds/[issue-id]-[feature-name].md` and critique it: milestone sizing (5-10 meaningful milestones, not micro-tasks), testability, clarity, missing risks and dependencies, and scope realism.

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

**Only offer this option when uzi is available** (`command -v uzi` succeeds, or the `uzi-cli` skill is installed). Do not re-implement the uzi flow here. Hand the freshly created PRD to the `uzi-cli` skill's **Send to uzi** orchestration, which asks how much to automate (Auto / Supervised / Seed & ship / Custom) and drives the run from there.

1. **Commit and push the PRD first** (exactly as Option 2) so the issue and `prds/` file are on the remote for the uzi worker to clone. Capture the pushed commit for the seeded path: `PRD_SHA=$(git rev-parse HEAD)`.
2. **Confirm uzi tracks this repo.** Load the `uzi-cli` skill (Skill tool) if not already loaded, then run `uzi repo list --json` and note the repo `id`. If the repo is not listed, tell the user it is not registered with uzi and fall back to Option 2.
3. **Hand off to the uzi-cli skill's *Send to uzi* section**, giving it this PRD's coordinates: repo `id`, issue `#[issue-id]`, and, for a seeded run, `--planned-commit "$PRD_SHA"`. Its menu picks the mode:
   - **Auto / Supervised / let uzi plan it**: uzi plans from the pushed PRD issue, so no local plan is needed.
   - **Seed & ship**: write the plan locally from the PRD's milestones and technical scope (the uzi-cli *Authoring a seeded plan* section is the guide), then seed it.
4. **If uzi rejects the issue for a missing `PRD` label** even though this skill added it, its poller has not synced yet. Use the forge's **Promote** action on the issue (it writes the label and refreshes uzi's cache in one request), then retry.

If the installed `uzi-cli` skill predates its *Send to uzi* section (older uzi binary), fall back to its *Authoring a seeded plan* section for the seeded path, or `uzi run create --repo <id> --issue [issue-id]` for the gated path.

## Important Notes

- **Option 1**: Best when you have time to begin implementation immediately
- **Option 2**: Best when creating multiple PRDs or planning future work
- **Option 3 (send to uzi)**: hands the PRD to the `uzi-cli` skill's **Send to uzi** menu, which picks how much to automate (Auto / Supervised / Seed & ship / let uzi plan it) and explains the budget tradeoff. A seeded run uses uzi's global default budget (keep the plan small), while the gated path scales the budget to the milestones uzi freezes (fits large or multi-component PRDs).
- **Skip CI flag**: Always use `[skip ci]` when committing PRD-only changes
- **Issue reference**: Include issue number in commit message for traceability
