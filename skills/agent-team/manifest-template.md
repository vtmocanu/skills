# Workflow manifest template

Copied verbatim into a repo's `.claude/agent-team.md` by `agent-team` **init**
(Mode 1, Step 5), then filled in with that repo's discovered values.

**This is the repo's manifest, which the LEAD reads and teammates never do.**
A rule that belongs to a teammate is unenforced here by construction — it goes
in that role's `prompt_body` in `roles.yaml`. A rule the lead must ACT on goes
in a Mode 3 step in `SKILL.md`. Both seams have been paid for: see SKILL.md's
notes on rules that leaked through this file to teammates, and on rules present
here that did not bind the lead either.

---

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

Default flow for a typical task. The authoritative task graph is `SKILL.md`
Mode 3 Step 2; this is a summary of it, never a second copy to maintain.
1. Spawn coder with the full task context. The coder runs `<test-command>`
   before reporting done. ONE coder per file-disjoint unit. Most tasks are
   one unit; the count comes from the split, not from this list.
2. As each unit reports done, spawn reviewer + auditor IN PARALLEL on THAT
   unit's diff + report. Do not hold them for the other units: a validator
   blocked on all of them waits on work it will never look at. Where there
   is more than one unit, add an integrated pass over the combined diff
   after the last unit lands, because cross-unit interaction is precisely
   what a per-unit review was not scoped to see.
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

**A test's FAILURE MESSAGE is an assertion too, and it is the one nobody
mutates.** A message that names a cause the check never measured is worse than a
vacuous test: a vacuous test merely fails to help, while a lying message actively
routes the next reader into the wrong subsystem. Ask of every diagnostic: could
this string be printed by a condition other than the one it names? (2026-07-21: an
e2e guard failed with "the worker claimed nothing (204)" when the actual cause was
a wrong jq path into a 200 response — `[ -n "$x" ]` cannot tell "no response" from
"wrong path", both give the empty string. It cost the lead an hour aimed at queue
contention. Fix: split the status check from the extraction so the two modes
produce two different messages.) Corollary for shell harnesses: `fail` called
inside `$( )` exits only the subshell and writes to stdout, which the substitution
*captures* — the run aborts under `set -e` with the diagnosis swallowed and nothing
printed at all. Set a global and return instead of printing.

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
- **The check can be as blind as the claim: a verification that cannot fail is not
  evidence.** A distinct failure from a wrong measurement, and worse, because
  re-running a blind instrument re-runs the blindness — "just double-check it" is
  the instinct this class defeats. `grep … | head -N` cannot show a summary line
  past N (→ a fabricated count); a page-width probe cannot show a truncated label
  (→ a "no regression" that regressed); `pgrep -f <name>` matches its own command
  line (→ a phantom process, or a deadlock); a stale git ref cannot show remote
  drift; a green CI *summary* cannot show that the phase you needed never ran.
  Defense: ask *what result could this instrument not produce?* — if the answer is
  "the one that would prove me wrong," it is not evidence. Prefer an instrument
  that holds an **identity** over one that holds a **pattern**: `kill -0 <pid>`
  over `pgrep -f`, `getElementById` over `querySelector`, `toBe(el)` over
  `textContent.toMatch`, `git grep <sha>` over a working-tree grep, a
  named-assertion count over an exit code. Naming the class buys no immunity — on
  the PRD that surfaced it the author hit it five times, once *during the shutdown
  check seconds after committing the note that says the author isn't immune*, each
  caught only because an identity-based check dissented from the blind one.
- **A constraint stated in a form cheaper to satisfy than to mean is an invitation
  to satisfy it.** "No horizontal scroll" was met while the lane label rendered
  zero characters; the intent was "the layout is not worse." A correct constraint
  measured with a blind instrument and a blind constraint measured well fail
  identically silently — they are two different questions. The writer of a
  dispatch owes the *intent* alongside the measurable proxy, and the receiver owes
  one question asked **before** the work, of the instruction itself: *what would
  make this true but still bad?* It is the only check in this section that
  prevents the work rather than catching it after.
- **A hostile fixture that cannot reach the sink it targets passes vacuously.** A
  payload clamped, trimmed, or flattened before it reaches the code under test
  proves nothing: a 63-rune XSS string cut at a 48-rune clamp, leading tabs eaten
  by `TrimSpace` before the column they were meant to break, a benign label making
  a sanitize-mutation a no-op. Caught only when the test fails for the *wrong*
  reason and someone asks why. Run a **positive control** — confirm the un-fixed
  code actually fails your check first — or "0 failures" is the blind-instrument
  result above wearing a green hat.
- **A number you did not see is a claim, not a measurement.** A figure read off a
  truncated window (`| head`), or a tally inherited across a handoff and
  incremented but never re-derived, is not evidence even when every digit in it is
  real — build the list first and let the count fall out of it, never the reverse.
  On one run both a coder's mutation count and the lead's running "claims caught"
  tally failed this way; the honest report is the corrections themselves, each
  recorded where it does work, not a total.

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

## Sweep per FACT after the last behavioural commit

**A batch's own findings falsify claims in code it never touched, and nothing
per-commit or per-file catches that, because the stale claim is not in the diff.**
After the last behavioural commit — before the final review wave, not after it —
grep for every *fact* the batch established and find every place that asserts
otherwise.

Validated 2026-07-27 on a batch that shipped **six code defects and ten prose
ones**. Every prose defect was a true statement a neighbouring commit falsified,
and none had an executable control: four sibling comments left asserting a
mechanism the fix had disproved (one directly contradicting another comment three
files away), an assertion that could not fail, a doc headline falsified by the
function one line beneath it, and a freshness claim true of only three rungs of a
six-rung fallback chain. The code was right every time. They cost four review
rounds and were found by four different agents.

Three things make the sweep work, each learned by its absence:

- **Sweep for the CLAIM, not the wording.** A grep for the phrase misses a
  sentence asserting the same thing in different words — one file carried the
  superseded measurement table without ever using the phrase being searched for.
- **The correction must state the mechanism, not just delete the false clause.**
  "Dropped the wrong claim" and "states the right one" are different edits, and
  only the second prevents the next round: a comment that is no longer false but
  no longer explains anything leaves the next reader to re-derive it and get it
  wrong. That happened three times to one paragraph in a single batch.
- **A CITATION is an assertion too, and `git log -S` is its control.** "Commit X
  fixed this" is checkable and almost never checked. The same two commits were
  transposed **twice** — one apart, touching the same field, differing only in
  *channel*, which was the very distinction the sentence existed to teach. A
  reader following it landed on a commit whose subject line contradicted the claim
  in its first six words, which discredits the true half along with the false one.

**Corollary for the lead:** when you dispatch a fix that names N sites, verify all
N landed. The one that got missed was missed because the lead checked the site it
had argued about and not the four it had listed in the same message.

## Two negative results from instruments that share an assumption are ONE negative result

**A search that comes back empty is evidence only if it could have come back
full.** Running a second search and getting empty again feels like corroboration
and usually is not: if both are shaped by the same guess about naming, they fail
together, and their agreement measures the guess rather than the code.

Measured 2026-07-27, and it nearly shipped a false security invariant. A field was
ruled safe on the premise that one query was the only one listing a table:

- the first grep searched for a substring the counterexample's name **does not
  contain** (the two names share no common substring, though they name the same
  concept), so it was structurally incapable of returning it — and its short
  output was read as an enumeration;
- the second searched a directory the relevant page does not live in, for a type
  name that differs from the one actually imported.

Two empty results, each shaped by a different naming guess, treated as agreement.
The premise was false: an admin route rendered every user's row, so the "safe"
field was the only cross-principal sink in the batch. It was caught because the
agent asked to *write the claim into a comment* checked it first — the last cheap
moment before a false invariant becomes code carrying two names.

**Enumerate from the schema object, not from a name you already know**: every
query touching the table, not every query whose name matches a string you have
already seen. The same reduction covers the sibling failures — a grep over
already-inventoried field names, a fixture built to demonstrate one state and read
as a search for its opposite, an assertion over one channel read as whole-component
coverage. **All four search a space defined by what you already found.**

**Corollary for the lead:** relaying a teammate's verified-sounding premise is not
verification. When a claim is about to be *written down* as an invariant — in a
comment, a spec, a doc — re-derive it yourself, however well-sourced. That is the
moment it stops being a finding and starts being something the next reader trusts
without checking.

## An assertion defines its CHANNEL

**"Nothing in this component carries X" and "nothing in this component's TEXT
carries X" are different claims, and the test that means the second reads like the
first.** A whole-subtree text assertion cannot see attribute values, a form
control's `value`, or the document title. The component is in scope; the channel is
not, and the assertion's own wording papers over the gap.

Measured 2026-07-27: nine tests asserting over rendered text all passed while
**four** untrusted values reached tooltip and accessible-name attributes
unstripped, across three rounds of review. Every one was found by a sweep or a
mutation control; **none by a test.** The demonstration is one line — revert the
attribute fix and only the new case reds, while the existing text-channel case for
the *same field* stays green.

Three habits follow:

- **Fix at the composition point, not the render sites.** One descriptor feeding
  five renderers gets one fix where it is composed. But state the coverage
  honestly: in that batch the comment claimed all five were covered when only two
  consumed that descriptor, and the "one place cannot drift out of step with four
  others" argument ran backwards — four constructors of the same shape already
  existed and the fix touched one.
- **A test that renders the wrong component passes forever.** One test rendered a
  component that does not render the field under test. It was green and worthless,
  and only its own control caught it. **Five files in that batch needed a
  component extracted to make the claim assertable at all** — if the value is not
  reachable by a test, that is a finding about the code's shape, not a licence to
  assert something adjacent.
- **A guarded render can make an assertion VACUOUS rather than weak.** Where the
  markup renders behind a truthiness guard and the fixture leaves the field empty,
  the subtree never mounts and a "no bad characters present" assertion passes over
  nothing. Require a **positive** assertion that the value is on screen. Note the
  two mechanisms differ: one branch did not mount at all, while a sibling mounted
  with a partial string, so only the positive assertion caught the second.

## Mutate at the CALL SITE, not in the shared helper

**Folding a mutation into a shared function proves the function is live. It does
not prove every caller routes through it** — and those are the two different
claims a control is usually asked to settle.

Measured 2026-07-27. A helper had two call sites; the control replaced its body
with the identity and two cases reddened, which read as proof the behaviour was
load-bearing. Both reddened cases exercised **the same call site**. The other arm
had no assertion at all, so a fix applied to only one of the two would have passed
that control unchanged — which is what happened, and it was caught by an audit
reading a comment rather than by any test.

**The discriminating instrument is per-call-site:** fold each site separately and
require **each** to red on its own, on disjoint sets. The tester who found this had
*already* used that instrument on eight render sites in the same batch; the shared
function simply made one fold look sufficient. As they put it: *"mutating the
shared helper is the composition-point mistake one level down — the same shape as
the finding itself, which is presumably why neither of us saw it from inside."* A
control written from inside an abstraction inherits that abstraction's blind spot.

## TYPECHECK the mutated tree before reading the test result

**A mutation that does not compile produces a run that says nothing, and "nothing"
is one glance from the finding you were hoping for.** Measured 2026-07-27, on the
last round of a long batch: a replacement string carried literal backslashes into
the mutation script, the mutant was a syntax error, the runner failed to *collect*
the file, and the run printed `Tests  no tests`.

The dangerous reading was right there — *"the mutation applied and no test went
red"* reads as *"this site is unguarded"*, which would have been a false
**Blocking** finding against a correct fix.

**It is the inverse of the silent no-op mutation** (a fold that matches nothing,
runs green, and understates). Both look like "the tests did not say what I
expected", and only the compiler separates them:

- run the typechecker **between mutate and run**;
- keep the pre-assert that the pattern was present and the post-assert that it is
  gone;
- add a post-assert that no stray escape reached the source, since the escaping bug
  is what produced the syntax error in the first place;
- **read the collection line, not just the tally** — "no tests" and "1 failed" are
  both "not the green I expected", and only one of them is a result.

**Read every red's arrival time and line number, not just the count.** A red at the
runner's timeout is a lookup failing, not an assertion firing: in that batch two
render tests reddened at a 5-second `findByText` timeout, so their real assertions
had never executed. After anchoring the awaits on something the mutation cannot
move, the same reds arrived at named lines in 2-4ms.

## Quality gates

One line per slot, in this order, each with the exact command (check-mode, and
suffixed `(rewrites files)` if only a fixing variant exists) or the literal
`none (gap)`. Per component in a monorepo. This block is the team's source of
truth for the gate; a slot omitted here is a slot nobody runs, and the lead
pastes it into every tester/reviewer/auditor dispatch since teammates
cold-start and never read this file themselves.

Once a teammate has surfaced a `none (gap)` slot, the lead appends a `noted`
marker — `dead code: none (gap, noted 2026-07-21)`. Roles are told to report a
gap only when its line carries no marker. Without this the instruction "report
it once per repo, not per change" is unfollowable: a fresh teammate has no
memory of prior sessions, so it either re-reports every wave or suppresses
forever.

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
