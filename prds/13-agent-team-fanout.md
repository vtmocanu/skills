# PRD #13: Reconcile the orchestrator's task graph with its own parallelism rules

**Issue**: [#13](https://github.com/vtmocanu/skills/issues/13) | **Label**: PRD | **Priority**: Medium
**Area**: `agent-team/SKILL.md`, Mode 3 Step 2 (the whole feature). `agent-team/manifest-template.md` is alignment only (M3).
**Status**: **CLOSED 2026-08-03.** M0, M1, M2, M3 and M5 landed (PR #14, `06dfd06`;
follow-up PR #15, `6d0e333`). **M4 was SKIPPED by decision, not completed** — it is
validation only, nothing shipped depends on it, and it needed a live multi-agent
session against a baseline that did not exist. Its box stays unchecked and carries
the reason. Success criteria 1, 2, 3 and 6 are met; **4 and 5 are unproven by
decision** rather than by oversight, which means this change ships as reasoning
rather than as measurement. See M4 and R1.

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
`.claude/agents/*.md`. Mode 4 is also touched, for M0's instrument only.
M2's contradiction was fixed inside Step 2 rather than in Step 3 to hold this
line: the singular "the implementer" in Step 3's frozen-spec rule is now
qualified where the N-unit graph is built, not where the rule is stated.

**D6: Creating `prds/` in this repo changes this repo's own role roster, and
that is accepted rather than worked around.** `roles.yaml`'s architect lists
`"prds/**"` in `triggers_on` and `SKILL.md`'s Step 2 includes architect when the
repo has a design surface, so the next `init`/`update` here selects an architect
it would not have selected before. This is the one behavioural change to a live
system that landed before any milestone was implemented (the directory arrived
with the PRD itself, in commit 8156f01). Desirable: a repo whose main artifact
is agent-workflow design is exactly a repo an architect should be on. Recorded
rather than silently absorbed because it is a roster change nobody asked for,
and `.github/CONTRIBUTING.md` now states it so the next reader meets it as a
documented consequence rather than a surprise.

## Milestones

- [x] **M0: Baseline.** Mode 4 (reflect) already produces dated session
      observations and is the starting point, not a blank sheet. What it does
      not produce is timing or concurrency data. Add that: for a completed
      session, how many teammates were active concurrently, and where the wall
      clock went. Without it, M4 cannot compare anything.
      **Landed** as reflect return item 7 (`SKILL.md` Mode 4), with the brief
      path and work-branch name added to the pass's inputs so the item has
      something to read. Measures unit split, peak concurrency, wall clock, and
      idle-implementer time; requires every figure labelled `measured` or
      `recalled`, and requires reporting on single-unit runs too, since those
      are the baseline.
- [x] **M1: Task graph follows the decomposition.** Rewrite Step 2 so the
      implementation task count comes from the unit split and each validator
      task blocks on its own unit, with the integrated pass retained (D1, D2).
      **Landed.** The integrated pass is created only when N>1: with one unit
      the per-unit review already is the pass over the whole diff, and adding a
      fourth task would have broken success criterion 1.
- [x] **M2: Step 2 and Step 4 agree.** Verify by reading that the graph Step 2
      builds is one the Step 4 pipelining rule can actually operate on, and that
      neither section contradicts `roles.yaml`'s coder contract.
      **Done, and it found one.** Step 4 requires review scope pinned to explicit
      commit SHAs; `roles.yaml`'s coder body says a coder in parallel mode does
      not `git commit`. A per-unit validator task therefore has no SHA to pin in
      the shared-worktree mode, which would have made the new graph
      un-executable exactly where it matters. Resolved inside Step 2 (D5 scope)
      by naming the two modes: per-worker branches supply a coder SHA; a shared
      worktree means one LEAD commit per unit, which is the integrate step the
      coder body already assigns the lead. Also fixed there: with N units the
      design freezes when the FIRST coder spawns, which Step 3's frozen-spec
      rule states in the singular.
- [x] **M3: Manifest alignment.** Update `manifest-template.md`'s flow paragraph
      to match, carrying the release steps through unchanged (D3).
      **Landed.** Steps 4 and 5 (the release summary and its user-confirmation
      gate) are byte-identical to before; the flow now names `SKILL.md` Mode 3
      Step 2 as authoritative so the summary cannot drift into a second spec.
- [ ] **M4: Validation.** Run a genuinely multi-unit task and report against
      M0's baseline: concurrency, wall clock, and the classes of finding the
      review lane produced.
      **SKIPPED 2026-08-03, by decision, and the box stays unchecked.** M4 is
      validation only: nothing in the shipped behaviour depends on it, and
      M0-M3 and M5 are the change itself. It needs a live multi-agent session
      plus a measurement, and it was blocked twice over — M0 ships the
      instrument, but no session had been measured through it, so there was no
      baseline to compare against.
      **What the skip costs, stated rather than buried.** Criteria 4 and 5 are
      the two that would have shown the fan-out actually helps, so this change
      ships as reasoning rather than as measurement. R1 already names that
      exposure: *"M4 measures rather than asserts."* The trade is defensible
      for a workflow document whose cost of being wrong is a slower run, not a
      broken one, and it is recorded here so a later reader can disagree with
      it on the evidence rather than discover it by absence.
      **How it becomes doable, if anyone wants it later.** R5's path is
      unchanged: run an agent-team session inside a repo whose factory already
      emits run-log concurrency profiles, and take the paired comparison there
      instead of from reflect item 7's partly-recalled figures. That does not
      require reopening this PRD.
      The box is not checked, per this repo's own convention: a milestone the
      work did not complete keeps its empty box and carries the reason.
- [x] **M5: Conventions.** Add a line to `.github/CONTRIBUTING.md` describing
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
      **Landed** as a `## Product Requirements Documents` section in
      `.github/CONTRIBUTING.md` (with a TOC entry), which also states the
      architect side effect for the next reader. Recorded as D6 below and in
      `CHANGELOG.md` under `## [Unreleased]`.

## Success criteria

**Met: 1, 2, 3, 6. Unproven by decision: 4 and 5** — both depend on M4, which was
skipped (see M4). They are not "probably fine"; they were never measured, and the
distinction is the whole point of stating them separately.

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

  **R3 is no longer an assertion. It has been measured, elsewhere, and it is
  larger than this document assumed.** The sibling change in the `uzi` repo
  (PRD #215, "Pipeline the lead's review lane and overlap its integration
  gate", which cites this PRD in its own D1) measured the same suite under
  contention at a **constant test tally**, which is the comparison that is not
  confounded by a moving tree: `npm test` ran **36.1 / 43.7 / 43.9 / 52.6 /
  73.3 / 79.8 s** at 1474 tests (2.2x spread) and **33.8 through 89.6 s** at
  1541 tests (2.65x). Its D2 records why that matters beyond latency: those
  runs push into timeouts that had *already* been raised because of contention,
  and a gate that reddens intermittently is **weaker verification**, because
  the documented human response is to re-run and the retry destroys the
  evidence. Its source repo states the principle directly: *"The gate's job is
  a trustworthy verdict, not a fast one."*

  Two consequences for this PRD, neither of which the original R3 supports:

  - **Criterion 4's "wall clock does not regress" is necessary but not
    sufficient.** A run can hold wall clock and still buy it with a flakier
    gate. M4 should report gate red-then-green-on-retry counts, which is
    PRD #215's criterion 7 and is the half this document has no instrument for.
  - **What may overlap the integration gate is the READ-ONLY wave only**
    (PRD #215's D3). Reviewer, auditor, fact-checker and tester read; a second
    implementation unit writes. Every teammate in the shared-worktree mode
    shares one tree, so overlapping the gate with a *writing* unit gates a tree
    that is moving underneath it. Step 2's two-modes paragraph makes the SHA
    question explicit but does not say this, and it should.

- **R5: This PRD's M0 is the weaker of the two instruments now in existence.**
  Reflect item 7 reads artifacts after the fact and labels half its figures
  `recalled`. PRD #215's M0 is a script over `uzi run logs --json` producing a
  concurrency profile with per-wave timings, and it recommends a **paired**
  comparison (the same issue before and after) because wall clock is dominated
  by issue shape rather than by the prompt. That is a better design and this
  PRD should not pretend otherwise. It is also the concrete unblock path for
  M4: an agent-team session run inside a repo that factory measures would get a
  real baseline rather than a recalled one.
- **R4: The graph gets harder to read.** A conditional task graph is more
  complex than a fixed one, and every reader pays that including on one-file
  changes. D1 and criterion 1 are the mitigation.

## Open questions (resolved during implementation)

- ~~What does M0 actually measure, and from what surface?~~ **Four figures, all
  from artifacts that outlive the run**: the `units:` line in the brief's
  `## Roster` section against the number of implementation tasks actually
  created; peak simultaneous dispatched-and-unreported teammates; wall clock
  from `git log --format='%h %aI %s' <base>..<work-branch>`; and idle-implementer
  time, which is the figure that says whether Step 4's pipelining rule fired.
  The surface choice is doing real work: commit timestamps are measured, while
  dispatch and completion times are the lead's recollection unless written down,
  and the task list is documented-volatile in this same file. So item 7 requires
  each figure labelled `measured` or `recalled` and forbids blending them.
  M4 is therefore a real comparison on wall clock and a partly recalled one on
  concurrency, which is worth stating up front rather than discovering later.

- ~~Should Step 2 require the lead to state the unit split explicitly?~~ **Yes,
  as one line in the `## Roster` section Step 2 already mandates writing.** Not
  a new artifact and not a new decision: `units: 1 — one file, no split` records
  that no decision was needed, so D1's zero-effort path costs one sentence in a
  section already being written and criterion 1 still holds. The reason to
  require it is the reason Task #0 is a task: a split that lives only in the
  lead's head leaves nothing for a later reader to contradict, and it is
  invisible to the reflect pass, which is the only thing that measures whether
  any of this helped.
