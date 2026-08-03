# PRD #13: Reconcile the orchestrator's task graph with its own parallelism rules

**Issue**: [#13](https://github.com/vtmocanu/skills/issues/13) | **Label**: PRD | **Priority**: Medium
**Area**: `agent-team/SKILL.md`, Mode 3 Step 2 (the whole feature). `agent-team/manifest-template.md` is alignment only (M3).
**Status**: not started.

**Evidence basis**: this PRD is derived from reading the three files that make up
the `agent-team` skill. No timing data for agent-team sessions exists, which is
why M0 is a measurement milestone and why no speedup figure appears anywhere in
this document.

**This PRD was rewritten after review.** Its first draft claimed the skill could
not fan implementation out and batched all validators to the end. Both claims
were false, and they were false because the draft was derived from
`manifest-template.md` alone, which is one of three files. The capability
already exists in the other two. What survives is smaller, and it is the part
that binds: the three files disagree with each other, and the one the lead
executes is the serial one. The correction is recorded here rather than silently
applied, because the failure mode that produced it (reading one artifact and
generalising to the feature) is the one `manifest-template.md:198-232` already
names: *"Two negative results from instruments that share an assumption are ONE
negative result"*, and its line *"All four search a space defined by what you
already found"*.

Note the em dashes retained inside quotations throughout this document. The
repo's style rule (`CLAUDE.example.md:19`) governs authored prose; silently
restyling punctuation inside quote marks would edit the artifact being cited,
which in a document whose whole thesis is "read all three files accurately"
would be its own small version of the same mistake.

## Problem

The skill already supports parallel implementation and pipelined review. Three
artifacts say so:

- `agent-team/roles.yaml`, the `coder` role's `prompt_body`: *"You may be
  dispatched as one of several coders working in parallel in the same worktree.
  When your delegation prompt assigns you a file scope, treat it as a hard
  boundary ... In parallel mode do NOT run `git commit`, and do not run gate,
  build, or test commands unless they cover only code you exclusively own ... the
  lead integrates, commits, and runs the repo-wide gate after all parallel units
  land."*
- `agent-team/SKILL.md`, the section headed *"Parallel same-repo waves (multiple
  implementers editing one repo at once)"*.
- `agent-team/SKILL.md`, Mode 3 Step 4: *"**Pipeline milestones across review
  waves**: once a milestone's SHAs are frozen and dispatched to the read-only
  validators, dispatch the coder's NEXT milestone immediately — do not idle the
  coder waiting for the wave ... Validated 2026-07-13: a 6-milestone PRD ran
  coder-implementation and review waves fully overlapped, zero rework, zero
  blocking findings, no idle coder time."*

**The defect is that the step which actually binds contradicts all three.**
Mode 3 Step 2 tells the lead to create this task graph, and a task graph is
executed rather than read:

```
- Task #0: design critique (owners: reviewer + auditor, and architect if the
           roster has one) - blocks #1. Close it with a written reason when the
           change is small enough not to need one.
- Task #1: implementation (owner: coder)
- Task #2: review (owner: reviewer, blockedBy: #1)
- Task #3: audit (owner: auditor, blockedBy: #1)
- Task:    spec sync (owner: spec-keeper, blockedBy: #2 + #3) - only if
           spec-keeper role exists
- Task #4: release (owner: release, blockedBy: #2 + #3, user-gated) - only if
           release role exists
```

All six bullets are quoted deliberately. Task #0 and Task #1 are separated in
the source by a long block on how to close a skipped task, which is exactly how
an excerpt loses them, and losing Task #0's `blocks #1` edge or the two
`only if ... role exists` conditionals in a rewrite is the same orphaning hazard
D3 exists to prevent one artifact over.

Note what this corrects about the shape: reviewer and auditor are **already**
dispatched before implementation, as Task #0. So the current graph is
critique -> implement -> review/audit, not implement -> review/audit. The defect
survives that correction intact, because it is about the **implementation** task
being singular and the validators of a *finished* unit being unable to start
while another unit is still building. But the first version of this section
understated what Step 2 already does.

A lead that follows Step 2 has committed to the serial shape before it reaches
Step 4's pipelining rule **over a hundred lines later** (116 lines from the last
task bullet, 137 from the Step 2 heading), and the tasks it created are what it
works from thereafter.

`agent-team/manifest-template.md`'s "Default flow for a typical task" repeats
the same serial chain in prose, which is consistent with Step 2 and inconsistent
with everything else.

So the cost is not a missing capability. It is that the capability is written
down in the files the lead reads and absent from the structure the lead builds,
and structure wins.

### Why more prose is not the fix

`agent-team/SKILL.md`'s own reflect guidance, written after this exact
intervention failed: *"**Prefer a change to the ORDER OF OPERATIONS over another
warning.** This file already documented mid-turn crossing at length, and the
lead read it and crossed twice anyway. Past some size, added prose reduces
compliance rather than improving it — only a step that must be executed actually
binds."*

That is the argument for changing Step 2 rather than adding a section, and it is
the repo's own conclusion from its own measured failure.

## Solution

Change the task graph Step 2 creates so it can express what the rest of the
skill already permits:

1. **Make the implementation task count follow the decomposition**, not a
   constant. One unit produces today's graph exactly. N disjoint units produce N
   implementation tasks.
2. **Block each validator task on its own unit**, not on all implementation.
3. **Keep an integrated pass** over the combined diff, because cross-unit
   interactions are what a per-unit review cannot see.
4. **Point at the existing rules rather than restating them.** The disjointness
   test, the ownership boundary, and the no-commit/no-repo-wide-gate contract
   already exist in `roles.yaml` and in Step 4. Step 2 references them.

No role changes, no verification removed. It does add text to Step 2 - a task
count that follows the split needs saying, and the lead needs a pointer to where
the disjointness test lives - but it adds no new guidance *section*, which is
the distinction that matters given this file's own warning about length.

## Decision log

**D1: Serial stays the default and stays the zero-effort path.** A single-unit
task must produce exactly today's graph, with no extra decision to make. The
change adds a branch, not a requirement.

**D2: Reference, do not duplicate.** The parallel contract lives in
`roles.yaml`'s `coder` body and the wave rules live in Step 4. Restating either
in Step 2 or in the manifest creates copies that drift, and nothing in this repo
can detect drift between them: `scripts/validate_skills.py` checks frontmatter
only. This is the specific reason the first draft's D2/D3/D4 were dropped rather
than reworded.

**D3: The release gate is untouched.** `manifest-template.md`'s flow continues
past the part this PRD is about, with a release step gated on explicit user
confirmation. Any rewrite must carry those steps through unchanged. Called out
because a rewrite scoped to "the default flow" is exactly how a user-confirmation
gate gets orphaned.

**D4: Success is speed and findings, and both need thresholds.** A flow that
finishes faster while surfacing fewer findings is a regression. The criteria
below state each half separately so neither can pass by pointing at the other.

**D5: Scope.** Mode 3 Step 2, plus the manifest's flow paragraph for
consistency. Not `roles.yaml`, not the role bodies, not the generated
`.claude/agents/*.md`.

## Milestones

- [ ] **M0: Baseline.** Mode 4 (reflect) already produces dated session
      observations and is the starting point, not a blank sheet. What it does
      not produce is timing or concurrency data. Add that: for a completed
      session, how many teammates were active concurrently, and where the wall
      clock went. Without it, M4 cannot compare anything.
- [ ] **M1: Task graph follows the decomposition.** Rewrite Step 2 so the
      implementation task count comes from the unit split and each validator
      task blocks on its own unit, with the integrated pass retained (D1, D2).
- [ ] **M2: Step 2 and Step 4 agree.** Verify by reading that the graph Step 2
      builds is one the Step 4 pipelining rule can actually operate on, and that
      neither section contradicts `roles.yaml`'s coder contract.
- [ ] **M3: Manifest alignment.** Update `manifest-template.md`'s flow paragraph
      to match, carrying the release steps through unchanged (D3).
- [ ] **M4: Validation.** Run a genuinely multi-unit task and report against
      M0's baseline: concurrency, wall clock, and the classes of finding the
      review lane produced.
- [ ] **M5: Conventions.** Add a line to `.github/CONTRIBUTING.md` describing
      this repo's own `prds/` convention, which no document currently describes.
      (`prds/` is *mentioned* in four places - `SKILL.md:101`, `:116`, `:271`,
      `roles.yaml:994` - but only as a directory to look for in OTHER repos.)
      **Record as a decision that creating `prds/` changes this repo's own role
      roster**: `roles.yaml:994` lists `"prds/**"` in the architect's
      `triggers_on` and `SKILL.md:116` includes architect when the repo has a
      design surface, so the next `init`/`update` here selects an architect it
      would not have before. Probably desirable, but it is the one behavioural
      change this PRD makes to a live system before any milestone is
      implemented. Also record the change in `CHANGELOG.md` under
      `## [Unreleased]`.

## Success criteria

1. A single-unit task produces today's task graph, unchanged, with no extra
   decision required of the lead.
2. A task with N disjoint units produces N implementation tasks and per-unit
   validator tasks.
3. Step 2's task graph, Step 4's pipelining rule, and `roles.yaml`'s coder
   contract point at the same shape. Today they do not: the graph Step 2 hands
   the lead is serial and nothing in it signals that the implementation task
   count may vary, so the parallel path is reachable only by a lead who recalls
   a section 76 lines further on. (Stated as a claim about the DEFAULT, not
   about possibility: `roles.yaml`'s clause is permissive - "You **may** be
   dispatched as one of several coders" - and nothing in Step 2 forbids creating
   N tasks, so "they cannot all be followed" would be false.)
4. On a multi-unit task, measured concurrency exceeds M0's baseline **and wall
   clock does not regress**. Both halves are required: fan-out raises
   concurrency *mechanically*, so a concurrency-only criterion passes by
   construction of the experiment, and R3 predicts contention that could raise
   wall clock while concurrency rises. Concurrency alone is the mechanism; wall
   clock is the outcome (D4).
5. The review lane surfaces the same *classes* of finding as the baseline.
   Stated as classes and not as a count: two different tasks have different true
   finding populations, so a count comparison would score a cleaner
   implementation as a regression.
6. The lead still integrates, still gates once over the combined diff, and the
   release step is still user-gated (D3).

## Risks

- **R1: Prompt and structure changes are not deterministic.** Changing the task
  graph makes the serial shape harder to fall into; it does not make it
  impossible. M4 measures rather than asserts.
- **R2: Fan-out on units that were not genuinely disjoint.** The mitigation is
  the ownership contract already in `roles.yaml`, which is why D2 forbids
  restating it in a second place where the two copies could diverge.
- **R3: More concurrent teammates means more contention** on the machine and on
  the test suite. Step 4's existing rule that the lead gates once over the
  integrated tree is the main lever, and it is unchanged.
- **R4: The graph gets harder to read.** A conditional task graph is more
  complex than a fixed one, and every reader pays that including on one-file
  changes. D1 and criterion 1 are the mitigation.

## Open questions

- What does M0 actually measure, and from what surface? This determines whether
  M4 is a real comparison or a qualitative one.
- Should Step 2 require the lead to state the unit split explicitly (an
  artifact a later reader can contradict, matching how Task #0 is justified), or
  leave it implicit?
