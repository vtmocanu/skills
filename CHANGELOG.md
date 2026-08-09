# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.33.4] - 2026-08-09

### Changed

- README: each install path (agent-kit, all skills) is now its own visible section, with the install command and skill table shown outright; only the auto-update hook sits in a collapsible. Skill tables are no longer hidden behind the expander.

## [0.33.3] - 2026-08-09

### Changed

- README: made the install choice impossible to miss. Both one-line install commands (agent-kit vs the full catalog) now show up front, above the collapsibles; each collapsible holds only that path's auto-update hook and skill list.
- README: dropped the `*` vendored markers from the skill tables and their inline legend for readability; the full vendoring attribution stays in Credits.

## [0.33.2] - 2026-08-09

### Changed

- README: reworked into two self-contained collapsible sections (agent-kit-only vs the full catalog), each with its own install command, SessionStart hook, and skill list, over a shared notes block. Trimmed for terseness.

### Fixed

- README: the subset-install example used comma-separated skill names (`-s a,b`); npx `-s` does not split on commas, so it is now space-separated (`-s a b`).

## [0.33.1] - 2026-08-09

### Changed

- README: organized install steps and the skill tables by audience (the `agent-kit` bundle vs the full catalog), and documented two SessionStart hook variants (whole-repo `--skill '*'` vs the `skills/agent-kit` subpath) so bundle-only users get a clear hook.

## [0.33.0] - 2026-08-09

### Changed

- Grouped `agent-team` and the ten `prd-*` skills together under a new `skills/agent-kit/` container so the whole set installs in one command via a subpath: `npx skills add vtmocanu/skills/skills/agent-kit -a claude-code -g -y`. Invocation names are unchanged (they come from frontmatter, not the path). Anything added under `skills/agent-kit/` later joins the bundle automatically. Updated the CI test path, the historical-corpus test's rename-aware roles.yaml walk, the validator docstring, and the README accordingly.

## [0.32.0] - 2026-08-09

### Changed

- `skills`: documented that `npx skills update` (and any `add` without `-a`) reinstalls each changed skill to every *detected* agent (detection is just "the agent's config dir exists") and cannot be scoped, so a SessionStart hook whose every `add` is `-a claude-code` still leaks copies to other agents through the chained `update`; the only fix is to make the other agents undetectable. Added an "Enable / disable an installed skill" section covering the four-state `skillOverrides` control (`on` / `name-only` / `user-invocable-only` / `off`), why `off` beats `permissions.deny: ["Skill(name)"]`, and the name-collision caveat (never install a skill whose `name:` duplicates a built-in such as `claude-api`, or an `off` override would disable the built-in). Noted `npx skills add <source> -l` (list a source without installing, to audit it or spot new upstream skills) and that `--skill` is include-only with no exclude flag.

## [0.31.0] - 2026-08-09

### Added

- `generate-cicd` and `generate-dockerfile` skills, vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) `shared-prompts/` (MIT, Copyright (c) 2025 Viktor Farcic). Copied verbatim with the dot-ai `category` frontmatter dropped and a provenance line added. `projectSetup` is a stateful dot-ai MCP tool rather than a prompt, so it cannot be vendored as a standalone skill.

## [0.30.0] - 2026-08-08

### Removed

- `git-worktrees` skill (added in 0.29.0). Its value for the maintainer was marginal once the bare-clone layout was de-emphasized: normal `git worktree add`/`remove` are standard git, and the aliases plus the bare-clone rules already live in the user's gitconfig and global instructions. `prd-worktree` (a concrete PRD action with a bundled script) is unaffected.

## [0.29.0] - 2026-08-08

### Added

- `git-worktrees` skill, migrated from an internal catalog and generalized for public use. Short-lived **normal** worktrees are the default (raw `git worktree add`, or the optional `git new-wt` alias); the **bare-clone-with-child-worktrees** layout is kept as an opt-in advanced section rather than the default. Ships both `git new-wt` and `git clone-wt` as `~/.gitconfig` alias snippets, sanitized of internal URLs, paths, and tool names.

## [0.28.0] - 2026-08-08

### Added

- `prd-worktree` skill, vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) `.claude/skills/dot-ai-worktree-prd/` (MIT, Copyright (c) 2025 Viktor Farcic) and renamed from `worktree-prd` to fit the `prd-*` family. Creates a git worktree for PRD work with a descriptive branch name; ships its bundled `create.sh`, referenced via the `<this skill's directory>/…` base-directory idiom so it resolves under npx.
- README: an example installing just the PRD workflow plus the agent team via `-s`.

### Changed

- `prd-full`: updated its cross-reference from `/worktree-prd` to `/prd-worktree`.

## [0.27.0] - 2026-08-08

### Added

- `prd-create`: two uzi options, shown only when the `uzi` CLI or the `uzi-cli` skill is available. **Option 3** commits and pushes the PRD, then seeds it to the uzi factory from a locally-authored plan (`uzi run create --plan-file`) so the worker implements it directly. **Option 4** starts a run and lets uzi plan, then watches for the plan gate (`awaiting_approval`), shows the plan, and approves or rejects it on the user's call.

### Changed

- `prd-start`, `prd-full`: replaced dot-ai's `{{prdNumber}}` / `{{mode}}` prompt templating with Claude Code argument tokens (`$ARGUMENTS`, `$1`, `$2`), so the vendored skills receive invocation arguments under npx / Claude Code.

## [0.26.0] - 2026-08-08

### Added

- `prd-*` PRD-workflow skills (`prd-create`, `prd-start`, `prd-next`, `prd-update-progress`, `prd-update-decisions`, `prd-done`, `prd-full`, `prd-close`, `prds-get`), vendored from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) `shared-prompts/` (MIT, Copyright (c) 2025 Viktor Farcic). Copied largely verbatim, converted to the folder `SKILL.md` layout with the dot-ai `category` frontmatter dropped and a provenance line added to each. See the README Credits section for attribution.

### Changed

- `skills`: document that an unquoted `: ` (colon-space) in a frontmatter `description` makes the `npx skills` YAML parser silently skip the skill (`mapping values are not allowed here`); reword or double-quote the description.
- README: lead with the `npx skills` install flow (all skills, a specific subset via `-s`, and the auto-update `SessionStart` hook), and move the dot-ai path into a collapsed Legacy section.

## [0.25.0] - 2026-08-08

### Changed

- `reflect`, `done`, `upgrade-advisor`: converted from flat `<name>.md` files to `<name>/SKILL.md` folders so the `npx skills` package manager discovers them (npx reads only folder skills; dot-ai continues to accept both forms).
- `agent-team`: normalized bundled-file references (`roles.yaml`, `manifest-template.md`, `scripts/sync.py`, `scripts/test_sync.py`) from bare-relative paths to the `<this skill's directory>/...` base-directory idiom, so they resolve when the skill is installed via `npx skills`, which copies each skill folder verbatim with the working directory set to the consumer repo rather than the skill directory.
- Migrated the skills' source-of-truth and regeneration notes from the dot-ai pipeline (`dot-ai skills generate`, `/dot-ai-skills`, `~/.claude/commands/`) to the `npx skills` model (`npx skills update`, `~/.claude/skills/`), matching the move of this catalog onto the npx package manager (`agent-team`, `agent-permissions`, `done`, `upgrade-advisor`).

### Removed

- Retired the `cmux` skill family (`cmux`, `cmux-browser`, `cmux-customization`, `cmux-diagnostics`, `cmux-keyboard-shortcuts`, `cmux-markdown`, `cmux-settings`, `cmux-workspace`) into `retired/`. They are no longer discovered as active skills; see `retired/README.md`.

### Fixed

- `done`: the frontmatter `description` contained an unquoted `: ` (colon-space), which the `npx skills` YAML parser rejects ("mapping values are not allowed here"), so npx silently skipped the skill while dot-ai's more lenient parser had accepted it. Reworded to remove the colon-space, and updated the invocation hint from `/dot-ai-done` to `/done`.

## [0.24.0] - 2026-08-08

### Added

- `agent-team`: new **skill-reviewer** role (v1). Reviews an added or changed skill (`<name>/SKILL.md` plus supporting files) against skill-authoring best-practices and the repo's linter, read-only, reporting findings by severity. Auto-picked when a repo is a skill catalog (`triggers_on: **/SKILL.md`); wired into Step 2 role-picking and Step 3 tail guidance.

## [0.23.0] - 2026-08-08

### Added

- `skills`: new skill for authoring, linting, and publishing Claude Code skills with the `npx skills` package manager. Covers the edit-source-not-installed-copy rule (owner vs consumer paths), folder `SKILL.md` layout and base-directory references, frontmatter and description limits, design principles, the `$1`/`$2` code-block substitution gotcha, agnix linting, and the add/update/remove scopes plus lockfile locations.

## [0.22.0] - 2026-08-07

### Added

- `agent-team`: **architect (v4)** gains a re-checkable-PRD-claims block (cite files by path plus a searchable symbol rather than a line number alone, mark "file X already exists" as verified at authoring time, and show the exhaustive search behind any "field read nowhere else" claim), a milestone-gating self-check (a milestone that "lands this run" must not depend on a gate that is itself deferred), and a capability probe before writing gated-contingency prose. It also absorbs the **pre-approval discipline** promoted up from the uzi product builtins: write the durable ADR or design doc only once the decision is taken, never before the plan is approved.
- `agent-team`: **fact-checker (v7)** gains two verification techniques. Mutation-test a regression test at the call site (reintroduce the defect, confirm the new test reddens for the stated reason, then restore the tree and show it clean), and verify an external standard, spec, or normative citation against its **source text** rather than the document that cites it (recomputing any claimed number from raw inputs). Plus a note to delete scratch artifacts fetched outside the worktree.
- `agent-team`: the lead now escalates an **unsatisfiable acceptance criterion** to the user before authoring a full multi-milestone plan, rather than burying the deviation inside the plan and submitting it (SKILL.md, Step 2).

## [0.21.0] - 2026-08-06

### Added

- `agent-team`: **`scripts/sync.py`, the only executable this skill currently ships.** The load-time staleness pass is prescribed as mandatory and compares the generic BODY as well as the `version:`, because `roles.yaml` allows one bump per release rather than one per edit, so a body change can ship without an increment and is invisible to a version-keyed comparison by construction. That check had no tooling, so every session either re-derived an 11-file body comparison by hand or wrote a throwaway to do it. `check` classifies each file as `ok` / `STALE` / `MODIFIED` (equal version, content differs) / `CUSTOM` / `LEGACY` / `BAD-FM` / `ERROR`, and `apply` performs the Mode 2 Step 5 merge: generic body replaced from the library, `version:` rewritten, `## For this repo` tail preserved byte-exact — verified by re-reading the file and comparing BYTES, which is what makes the claim survive a CRLF input. Exit codes separate a finding (1) from the instrument failing (2), so an unreadable library cannot be read as drift that was never measured. Every apply that replaces a body writes a `.pre-sync` backup beside the file and names it on stdout.
- `agent-team`: **`apply` refuses rather than overwriting** a role with no library entry, a role whose name would resolve outside the agents directory, a symlinked agent file, a frontmatter a strict parser rejects, a body that differs at EQUAL version (the axis-3 local modification; `--force`). A body carrying lines the library lacks is DROPPED with a warning rather than refused (see below). The last is about the body and not about whether the file has a tail: a file whose body is purely a stale copy has nothing to preserve and syncs normally, tail or no tail. Every guard runs over every named role before the first byte is written, so a guard refusal never leaves a partial batch.
- `agent-team`: **an unparseable frontmatter is now a reported status (`BAD-FM`), not a crash and not an `ok`.** Claude Code's loader tolerates an unquoted `description` containing `: `, which is illegal YAML; a stricter downstream parser rejects it, so the copy that works in use is the one hiding the defect. The script recovers what it can by regex, reports the parse error, and suppresses the `description`/`tools`/`model` verdicts it can no longer derive rather than emitting confident findings ("tools differ, file 0") produced by its own failed read.
- `agent-team`: **`scripts/test_sync.py`, 45 stdlib-`unittest` regressions**, most pinning a defect review found in a draft of `sync.py`. Both validators independently promoted "462 lines of new executable with zero automated coverage" as the mechanism that would let the next regression ship, and they were right within the hour: the suite caught two defects introduced by the fixes themselves (`BAD-FM` outranked by `STALE`, so the row said "run apply" for a file `apply` refuses; and disabling newline translation broke frontmatter detection on CRLF files outright), then a third that only a realistic fixture could reach.

- `agent-team`: **`test_sync.py` gains four INVARIANTS and a HISTORICAL CORPUS** (62 tests). Every other test in the file pins a known defect, which means every fixture was authored by someone who already knew what they were looking for — the failure mode the file exists because of, since two independent reviewers and the author all built append-shaped fixtures for a defect that only appears in replace-shaped input. The invariants (apply is idempotent; check is clean after apply; the tail is byte-identical across apply; apply on an already-current file is a byte no-op) come from the contract rather than from anyone's model of the input. The corpus generates a roster from each of the last eight `roles.yaml` revisions and asserts it syncs with the tail intact — real inputs, no imagination, and the instrument that produced every finding in this review that no authored fixture reached. Both are mutation-checked: reverting the `adds` predicate, the tail preservation, or idempotence reddens them.

- `upgrade-advisor`: **new "bundled alerting rule / health check" change class, a Step-7 measured example, and a Trap** — an upgrade whose payload includes default-on alert rules (or repairs a dead one) must have each rule dry-run against live signal *during* the eval. A rule that was absent or dead fires the instant it activates on a pre-existing latent condition, so a "safe bump that adds monitoring" is not automatically a quiet one. The worked example is the certmon `4.1.0 → 4.2.0` bump: 18 alerts fired on `broken_symlink`/`out_of_scope_symlink` errors that had been emitted continuously on 4.1.0 too — the old `X509ExporterReadErrors` alert was simply dead (renamed metric), so nothing had ever paged, and the eval waved the new alerts through as "net positive, minimal fatigue."

### Fixed

Everything below was found by review of the first draft, before merge. Each has a named test.

- **`--agents`/`--library` before the subcommand were accepted and silently discarded.** `parents=[common]` on a subparser re-applies its own default over the value given earlier, so `--agents X apply role` wrote to `./.claude/agents` — a tree the operator never named — destroyed a repo-owned line there, left X untouched, and exited 0. `--library <broken> check` reported the roster CLEAN. Fixed with `argparse.SUPPRESS` on the subparser copies. The defect was introduced by the fix for a usage error, and traded a loud failure for a silent wrong-target write.
- **`apply` destroyed inline hand-tuning in any file that HAD a tail.** The guard was gated on `agent.tail is None`, so two files identical but for a tail got opposite treatment: the tail-less one refused, the other silently rewritten. `body_delta`'s own docstring stated the principle unconditionally; only the code was conditional. The guard is now about the body and `--force-inline` is a separate flag from `--force`, so deciding the equal-version question for one role no longer disarms the content-destroying guard for the others.
- **A non-integer `version:` produced a duplicate YAML key and never converged.** `version: "1"` missed the `^version: *\d+ *$` replace, fell through to the insertion path meant for pre-versioning files, and PyYAML's last-wins resolution then kept the OLD value. Three `apply` rounds reported `version -> 2` and left the file reading 1 each time. The post-write verification checked the tail and the body and never the version — the one thing that branch exists to write.
- **The tail was not preserved byte-exact on CRLF input, and the verification was built so as not to see it.** `read_text`/`write_text` normalize newlines, and the check compared two already-normalized strings. Files are now read and written with translation off, the comparison is on bytes, the synced body is emitted in the file's own convention, and comparisons are newline-agnostic so a CRLF file does not read as having replaced its whole body.
- **A library-supplied role name could write outside the agents directory** (`name: ../CLAUDE-notes`); membership in the library was doing duty as a path check. Targets are now resolved and required to sit directly in the agents directory, and symlinked agent files are refused rather than written through.
- **The write was non-atomic** — `mode="w"` truncates first, so a failure partway through left half a role prompt with the tail gone. Now a sibling temp file plus `os.replace`.
- **`body_delta` miscounted any line beginning `++` or `--`,** because it filtered unified-diff headers by prefix. The library's own auditor and reviewer bodies each carry a line starting `--oneline -3`, so the deltas this skill reported for them were 28 when the truth was 29; worse, a repo-only line beginning `++` could drive `adds` to zero and disarm the inline-tuning guard entirely. Counted from `SequenceMatcher` opcodes now.
- **One malformed file aborted the whole roster scan**, printing zero rows with an exit code meaning "drift found". Per-file errors are now an `ERROR` row; the other rows still print, and the run exits 2 because for that file nothing was measured.
- **Exit-code contract violations:** an empty agents directory, a malformed library entry, `roles:` as a mapping, and a non-UTF-8 agent file all exited 1 (drift) rather than 2 (instrument failure). `apply` could also return 1 — documented as "nothing applied" — after writing files; the version stamp now runs in the guard phase, so a guard refusal never leaves a partial batch; a write error returns 2 with the file named.
- **Three rounds of review moved one threshold and broke the untested side of it each time, so the guard is gone and an unconditional UNDO replaces it.** Round 1 gated the refusal on the file having no tail, so two files identical but for a tail got opposite treatment. Round 2 de-gated it, and counting a `replace` opcode toward "lines the library lacks" made every library REWORDING look like repo content: refusing fired on 17 of 51 real library edits in this repo's history (18 of 52 if the commit that introduced per-role versioning is counted; this figure excludes it) and on **11 of 11 roles** for the bump that fixed an unreachable report recipient, which is the release this skill cites as motivating the body diff. Round 3 counted `insert` only — and that is blind to a human EDITING a library line in place, because it produces the same `replace` opcode as the library rewording it. Measured over every historical `roles.yaml` revision: of 119 (role, release) pairs whose body differs, the insert-only warning fired on **zero**, while 99 carried a `replace` that would have been dropped in silence. A compensating control with a measured firing rate of zero on real data is not a control.

  No predicate can separate "the library reworded this" from "a human edited this" — the opcodes are identical. So `apply` now writes a `.pre-sync` backup whenever it replaces a body at all, names it on stdout beside that role's success line, and says so in the closing message. There is deliberately no distinct exit status: once the predicate was corrected the condition fires on every ordinary sync, and a nonzero status meaning "it worked" is worse than no signal. The one guard that remains is the equal-version refusal, where the difference is genuinely unexplained.
- **`apply` now writes a `.pre-sync` backup, names the drop on stdout, and exits 3 — because the recovery instruction it gave was false in ordinary repo states.** The warning said to check `git diff`. With the roster committed and the hand-tuning added since, `git diff` shows exactly one deletion, the version line, which is the signature the drop-free closing message calls the all-clear: the prescribed check returns "clean" in the one case where content was destroyed permanently. Measured unrecoverable from git in 2 of 3 ordinary states, including "the roster was generated by `init` and not yet committed", which every consumer passes through. Separately, eleven files could each lose a repo-specific line while every stdout line read `preserved` and the status read 0, so the machine-readable channel could not distinguish the outcomes at all. A backup asks the operator for no decision, which is the property that made refusing fail.
- **The atomic-write fix had silently removed a protection:** `open(path, "w")` needs write permission on the FILE and `os.replace` only on the DIRECTORY, so a `chmod -w` file that used to be refused was being overwritten and its mode widened to 0644. `apply` now refuses an unwritable target, and the temp file carries the original's mode across the replace. The temp name comes from `mkstemp` rather than the target's name, so two agents applying the same role in one repo cannot truncate each other.
- **`cmd_diff` never got the path containment `cmd_apply` did** — a hostile `--library` declaring `name: ../PRIVATE-NOTES` made `diff` read and PRINT a file outside the agents directory, which for an agent is a disclosure into its context and its report.
- **The `is_symlink` guard was dead code** (`Path.resolve()` follows symlinks, so it never fired) and the case it missed — a symlink pointing INSIDE the agents directory — rewrote another role's file under the requested role's name. Tested on the unresolved path now.
- **`stamp_version` stripped the CR from the version line on CRLF files**, the same class as the defect it was written beside, and invisible to all three post-write checks because none of them compares frontmatter bytes. `agent.newline` is also derived from the first line terminator rather than from "a CRLF appears anywhere", so a stray CRLF in the tail no longer converts the whole body.
- **A library value that is present but null or wrong-typed** (an emptied block scalar, an ordinary `roles.yaml` slip) crashed with exit 1 — "drift found" for an unreadable library. Types are validated next to the presence check. Console output is also pinned to UTF-8, since a C locale made printing a row raise and exit 1.
- **"exit 1 always means the tree is untouched" was too strong.** The guard-phase claim holds, but an uncaught `OSError` mid-batch is also exit 1, because that is Python's code for a traceback. Write errors are caught and returned as 2 with the file named, and the docstring no longer says "always".
- **Smaller:** `apply` no longer proceeds on a `BAD-FM` file whose frontmatter a strict parser rejects; `MISSING COORDINATION TOOLS` is drift even when the file matches a library that is also missing them; a `CUSTOM` file no longer holds the mandatory load-time check permanently red; `diff` and `apply` agree on an unknown role; the tail marker is matched as a heading line with an optional suffix (`## For this repo (uzi)`), which an exact match had reported as tail-less across 10 of 11 files in a live roster; and the "not compared" note names the actual parse error instead of saying "unparseable" about a file with no frontmatter at all.

## [0.20.0] - 2026-08-04

Covers three streams that had accumulated on `main` since 0.19.0: the 9-role reflect pass, the per-agent context-budget work, and a new reflect pass from a 6-milestone, 11-role run. Most role bodies moved, so a consumer regenerating from this release should expect `/agent-team update` to report its roster stale. Net `version:` deltas from 0.19.0: coder 5 to 6, reviewer 5 to 7, auditor 5 to 7, tester 6 to 8, documenter 3 to 4, fact-checker 5 to 6, spec-keeper 2 to 3.

### Added

- `agent-team`: **a gate slot now records the ENVIRONMENT it runs in**, read off the CI job's image plus what is absent there, and the `tester` states the environment behind every figure it reports. Where CI runs the same command in a different image the tester runs it there too and diffs the test NAME SETS rather than the counts. A suite-level skip never registers its inner tests at all, so the count does not drop, it becomes a different count: one run had 86 tests that were never registered in CI while every local run showed them green, and no gate went red. A repo with no way to enumerate what ran is now reported as a gap, because an unenumerable gate cannot be diffed.
- `agent-team`: **the pre-release summary must list the values this change writes into a live system that no gate can check**: usernames, service accounts, endpoints, ports, secret key names. The literal strings, with the file and the system each addresses. This class is invisible to the entire gate by construction, since the repo holds no instrument that can reach the system a value refers to, and the user is the only cheap one. The matching `fact-checker` rule treats a config value destined for a deployment artifact as a claim about a live system and reports it UNVERIFIABLE with the operator check named, rather than skipping it as configuration. Motivating case: a service account copied from a spec's illustrative example passed six milestones and a validator wave green, then returned HTTP 401 on the first live call. The account was real and belonged to a different system.
- `agent-team`: **crossings are now recorded in the brief** (`crossed: <role> — <item> — recovered by standalone re-send`) and a send is gated on having seen the worker's idle notification, a property of the recipient, rather than only on the message carrying one item, a property of the payload. The one-item rule was justified by six sessions of measurement, but nothing writes crossings down, so the evidence could only ever accumulate what someone happened to remember: one run produced 2216 lines of log and zero crossing records, equally consistent with the rule working and with crossings going unlogged. Every recorded recovery names an idle worker; the item count came along for the ride.
- `agent-team`: **a retirement sweep is two passes** in both `documenter` and `spec-keeper`. Pass 1 finds the token, pass 2 opens every hit and asks whether the sentence is still true. Output is a per-site verdict (`updated` / `correct as history` / `already accurate`), never a count, because a count reads identically whether pass 2 happened or not. A carried-forward item names the fact that changed rather than the token, since "sweep for `OLD_VAR`" survives its own completion: the grep comes back empty, the item is ticked, and every sentence that assumed the old behaviour without naming it stands untouched.

- `agent-team`: Mode 1 Step 1 **measures the root `CLAUDE.md` and reports the number**. It is loaded in full every session and a subagent is its own session, so the file is paid per teammate spawned and occupies that share of every teammate's window. Past the documented under-200-lines bar, Step 1 proposes a split into path-scoped `.claude/rules/*.md` as one more item in the Step 4 proposal. Measured 2026-08-04 on a four-toolchain repo: 578 lines / 124,514 bytes became 186 / 40,453, so an eight-agent wave stopped carrying about 170k tokens of startup preamble. `Explore` and `Plan` are documented exceptions that skip `CLAUDE.md` entirely.
- `agent-team`: the same step requires the move be **proven byte-exact by reassembly diff**, and warns that fidelity is the easy half: byte-exact extraction silently falsifies every sentence describing POSITION ("below", "above", "this file", "stated in four places"), plus inbound references naming a deleted heading. Measured on that repo, zero content lost and 26 sentences made false, 14 in one workflow document nobody swept. It also records why a rule whose failure is irreversible must stay in the root: a path-scoped rule fires on a file READ, so it arrives after the decision to act.
- `agent-team`: Step 1 gains a **dead-weight and too-large-to-read probe**, with an executable threshold (roughly 10% of the smallest context window on the team) rather than a judgement.
- `agent-team`: Mode 3 Step 4 gains **paste the pointers you already found, labelled `exhaustive` or `starting point`**. Validators cold-start, so N of them re-derive one search. The label is the check that makes it a step rather than a caution, and it guards the real hazard: the SET of locations is itself a claim, so naming four files when the defect is in a fifth makes every validator inherit the omission at once.

### Changed

- `agent-team`: **a dispatch PASTES the tree evidence instead of stating it.** Mode 3 Step 4 previously asked the lead to say whether a writer was live in the pinned worktree; it now requires the pasted output of `git -C <worktree> status --short`, `git -C <worktree> log --oneline -3` and `git worktree list`. Producing that output is the check, whereas writing the sentence is compatible with never having looked, and the two cost the same. `reviewer` and `auditor` carry the receiving half: a dispatch must open with that evidence, and where it is absent the validator derives it and reports that it was missing. Enforcement sits in the role bodies because the lead has no role file and nothing else constrains what it asserts. One run produced six wrong lead claims about tree and commit state, four of them exactly this paste, including a worktree described as clean while a writer was live in it and a `git reset --hard` run in a worktree with an assigned writer.
- `agent-team`: **a carried-forward finding is re-derived at the new SHA starting with the LOW ones** (`reviewer`, `auditor`). Severity ranks consequence-if-true, not chance-still-true, so working top-down re-derives the items least likely to have been fixed while the ones a coder swatted in passing keep riding along.
- `agent-team`: **on validator disagreement, each gets the other's demonstration and owes a measurement back, not a position**, folded into Step 4's existing synthesize step. A ruling that adopts neither is a normal outcome: two validators can both measure correctly and disagree because they measured different things. Adjudicating from the two reports alone reduces to trusting whichever is written more confidently.
- `agent-team`: **a worklist item quotes the finding's demonstration and names its author**, with the lead's paraphrase on a separate labelled line. Collapsed into one sentence, the worker cannot separate what was measured from what was inferred, and a paraphrase silently upgrades: "this may not hold when X" becomes "this is broken", the coder fixes the lead's version, and the original finding is never addressed.
- `agent-team`: **parallel implementers are named per UNIT** (`coder-m2`), not per role. The `-N` collision suffix encodes spawn order and nothing else, while a unit name makes roster line, worktree, branch and merge target one identity, and survives a recycle.
- `agent-team`: Mode 4 reflect item 7 sources the implementation-task count from the brief rather than `TaskList`. The task list is documented-volatile and reflect runs at the very end, which is when it has been observed empty; a `TaskList` returning nothing yields "0 implementation tasks", a false measurement rather than a missing one.

- `agent-team`: **the recycle rules no longer depend on a number nobody can read.** Both the "Mandatory pre-dispatch check" (`>30%`) and the force-recycle (`>85%`) gated on a teammate's context budget, which the same section already said has no native readout. Verified against the docs 2026-08-04: there is no live readout, which is a conclusion drawn from the absence of any documented live-query path rather than a quotation (an earlier draft of this entry carried a quote attributed to the monitoring docs that appears nowhere in them; it was a fetch tool's summary mistaken for its source); `TaskGet`/`TaskList` expose no token fields and `TaskOutput` is deprecated and steers you away from a local agent's `.output` (the SKILL.md text quotes it); and OpenTelemetry cannot even separate teammates, since `agent.name` replaces user-defined names with `custom` and every agent-team role is user-defined. A percentage is also model-relative, because a subagent's window is sized by its own model. The pre-dispatch check now counts tasks dispatched since spawn, which the lead has in its own record, and the force-recycle now triggers on a `compact_boundary` in the teammate's transcript, which is the harness stating the fact rather than the lead estimating it. Recorded honestly: the count is a proxy and will misjudge a teammate whose single task was enormous, but it replaces a check nobody could perform at all.
- `agent-team`: the same section records that **subagents auto-compact**, so a teammate does not fall off a cliff at a threshold; it compacts and continues. Recycling is therefore about the relevance of carried context, not about rescuing a session from overflow, which is a weaker and more accurate rationale than the one the `>85%` rule stated.

### Previously unreleased: the 9-role reflect pass (landed on `main` after 0.19.0)

- `agent-team`: `coder` v5 to v6. A "because" clause is a claim about code you have not run: either run the mechanism and report the result, or cut the clause. Adds the copy-sweep, since a correction is not finished while its duplicates stand. The rule already existed in `reviewer`, `auditor` and `fact-checker`, three readers, and was absent from the only role that writes comments.
- `agent-team`: `reviewer` and `auditor` v5 to v6. Build only from a tree you control at a known SHA, even when you write nothing, and re-run the whole batch on finding one contaminated result. Of four agents on one branch, only the one whose body carried this rule complied; the other three each measured a mid-edit tree.
- `agent-team`: `reviewer`, `auditor` and `tester` gain the owned-responder control. When the instrument is a server or socket another process could own, the control must prove the responder is yours: a failed bind plus a stale listener returns a uniform clean result across every cell, which reads exactly like the whole class being rejected by the guard.
- `agent-team`: Step 1 gains a fetch before recording the branch point (one brief asserted a base that was four merges stale, costing 48 minutes of re-validation); Step 3's design wave gains the fact-checker, with why a citation pass cannot substitute for it; and the on-load staleness pass now diffs bodies rather than version numbers alone, after a version-only check reported all-clear on a tree where 9 of 11 bodies had drifted.

## [0.19.0] - 2026-08-03

Workflow-only. **No role body changed and no role `version:` moved**, so a consumer regenerating from this release will NOT see its roster reported stale: `/agent-team update` stays quiet and the change is in how the lead drives the team, not in what the teammates are told.

### Added

- `agent-team`: Mode 3 Step 4 now covers dispatching a validator at an **uncommitted working tree**, which the neighbouring "pin review scope to explicit commit SHAs" rule silently did not. There is no SHA to pin, so the validator checks a tree the author is still editing and its findings land against a state that no longer exists. Snapshot first (`git stash create` yields a throwaway commit object without touching the tree), dispatch at the snapshot, and name it in the prompt. The cost is paid twice: findings cannot be sorted into live-versus-already-fixed, and the validator loses the ability to claim independence, since a fact-checker that re-derives a mechanism before reading your assertion has corroborated you while one that reads it first can only agree. Which of those you get becomes an accident of timing rather than a property of the check. Validated 2026-08-03 on a PRD edit rewritten twice under two running validators: the fact-checker had to segregate its verdicts into EARLY (correct at the state it read, target since deleted) versus current, and observed that its strongest finding "only counts as corroboration because I happened to reach it before the file changed under me".
- Repository: `.github/CONTRIBUTING.md` documents this repo's own `prds/` convention (naming, required sections, milestone-checkbox discipline), which no document described. Note that adding `prds/` matches the `agent-team` `architect` role's `triggers_on`, so a team generated for this repository now selects an architect.

### Changed

- `agent-team`: **Mode 3 Step 2's task graph now follows the decomposition instead of hardcoding one implementation task.** The graph the lead builds was serial, and it disagreed with `roles.yaml`'s parallel coder contract, with Step 3's "Parallel same-repo waves" section, and with Step 4's pipelining rule 116 lines further down. A task graph is executed rather than read, so the serial one won. Implementation is now one task per file-disjoint unit, each validator task blocks on its own unit rather than on all implementation, and a multi-unit run adds an integrated pass over the combined diff. A single-unit task produces exactly the previous graph with no extra decision, and the split rules are referenced in `roles.yaml` and Steps 3 and 4 rather than copied into Step 2. The lead states the unit split as one line in the brief's `## Roster` section.
- `agent-team`: **Mode 4 reflect now returns concurrency and wall-clock numbers** (item 7), read from the brief's roster lines and `git log` on the work branch, with every figure labelled measured or recalled. Reflect already produced dated session observations; it produced no timing data at all, so no workflow change to the team's parallelism could be compared against anything.
- `agent-team`: `manifest-template.md`'s default flow matches the new graph and points at Mode 3 Step 2 as authoritative rather than restating it. The release steps and their user-confirmation gate are unchanged.
- `agent-team`: **only the read-only wave may overlap the lead's integration gate**, never a second implementation unit, and the contention cost is now counted against the gate rather than only against the clock. A sibling project running this same fan-out shape measured one suite at 36.1s to 79.8s on a constant test tally, and 33.8s to 89.6s on another, pushing into timeouts that had already been raised because of contention. A gate that reddens intermittently is weaker verification than a slow one, because the documented human response is to re-run and the retry destroys the evidence. Mode 4 reflect item 7 gains a gate-flakiness figure so a run that got faster while acquiring a flaky gate cannot read as an improvement.

## [0.18.0] - 2026-08-03

Seventeen commits had accumulated on `main` since 0.17.0 without a release, so this entry covers the whole backlog. Every role body moved; a consumer regenerating from this release should expect an `/agent-team update` to report most of its roster stale.

Note on version numbers: `roles.yaml` states one bump per release, not one per edit. Because these landed as seventeen separate commits directly on `main` with no release between them, several roles incremented four or five times for what a consumer sees as one body change. The numbers below are the net delta from 0.17.0. They are not renumbered: a downstream file already stamped at the higher number would otherwise read as ahead of the library.

### Added

- `agent-team`: **`manifest-template.md`**, a new sibling file holding the workflow-doc template that init copies into a repo's `.claude/agent-team.md`. It was inline in `SKILL.md`, which made that file a third longer for readers who never execute the template. The template also gained five verification sections earned on one batch that shipped six code defects and ten prose ones, where every prose defect was a true statement a neighbouring commit falsified and none had an executable control: sweep per fact after the last behavioural commit; two negative results from instruments that share an assumption are one negative result; an assertion defines its channel; mutate at the call site, not in the shared helper; and typecheck the mutated tree before reading the test result.
- `agent-team`: **quality-gate discovery** across eight slots (format, lint, typecheck, test, dead code, coverage, security scan, pre-commit), mining CI job definitions rather than trusting a root config nothing invokes. Commands are recorded in check mode only, with a `(rewrites files)` suffix where no check variant exists, and task-runner targets are opened and read because a fixing flag hides inside them. A slot with no check is the literal `none (gap)` rather than an omission. The workflow doc's single "Lint command" line becomes a `Quality gates` block the lead pastes into every validator dispatch. Before this, lint was mentioned three times in 648 lines, all inside the coder, and had exactly one self-reporting owner and no verifier.
- `agent-team`: a **design-critique wave before the implementer spawns** (Mode 3 Steps 2 and 3). Reviewer, auditor and architect get the brief alone and must, for every mechanism it asserts, name the file that implements it and quote the line. The design freezes when the coder spawns; a later design change is a new wave, not a message. The tester now spawns after the coder's first commit rather than at kickoff, so its harness is built against a tree that is not moving.
- `agent-team`: **Mode 4 reflect now covers `SKILL.md`**, not only `roles.yaml`. The largest cost in the run that motivated it was a step in the wrong order, which no role-body edit could fix.
- `agent-permissions`: a **kill switch**. `touch ~/.dippy/OFF` bypasses dippy entirely, per invocation, with no restart: the wrapper emits no decision and the host's own permission handling takes over. `rm ~/.dippy/OFF` re-enables.

### Changed

- `agent-team`: **every role body moved.** Net version deltas from 0.17.0: coder 1 to 5, reviewer 1 to 5, auditor 1 to 5, fact-checker 1 to 5, tester 2 to 6, documenter 1 to 3, release 1 to 3, web-ux 1 to 3, architect 2 to 3, researcher 1 to 2, spec-keeper 1 to 2. The load-bearing ones: all dispatched roles now treat an instruction that quotes a file or says a fix "did not land" as a claim about a tree that has been changing, and open the file at HEAD before acting; the tester learns that a green suite is not evidence a property is pinned, that a fold is a write and belongs in a detached worktree, and that presence checks are monotone under insertion; reviewer, auditor and fact-checker require a demonstration before Blocking or REFUTED, with sub-bar items in a mandatory separate list; the coder stages and commits by explicit path and never `git add -A`; and the releaser learns that a release may extend past the tag into a GitOps deploy, and that it is stateful across delegations.
- `agent-team`: **Mode 3 Step 4 gains a dispatch invariant: one actionable item per message.** Mailboxes are read between turns, so delivery races a turn boundary and the loss is per item; N items are N independent chances with no observable difference between partial and full absorption. Six sessions of prose about crossing had not moved the number. The brief becomes the spec: corrections amend it and messages only name the section that moved.
- `agent-team`: **the lead does not author the control for its own claim.** Naming a fold class in the brief makes three validators one instrument, and an acceptance criterion written by the party being tested can demand a red its own fix makes impossible. Both happened in one session.
- `agent-team`: the **severity-bar trigger becomes a command rather than a judgement.** `git diff --numstat` the deliverable between findings rounds; if executable content did not move, the round produced no behavioural change and blocking now requires a demonstration.
- `agent-team`: **ref-moving commands are never the lead's in a worktree with a live writer**, and a push publishes the SHA that was gated rather than the branch that points at it (`git push origin <sha>:refs/heads/<branch>`). A branch name resolves at push time; a gate result is bound to a SHA.
- `agent-team`: **verify the reported SHA, never `HEAD`**, plus the early-versus-stale distinction for a probe that disagrees with a report. Never instruct a worker to edit a script that is currently executing, and treat a refusal as correct.
- `agent-team`: **Mode 2 detects local modification at equal version, and custom roles absent from the library**, neither of which a version comparison sees. Every such row now carries a backport verdict (keep local, or propose for `roles.yaml`) with a reason: library to repo was fully specified, repo to library had no mechanism at all.
- `agent-team`: **Mode 3 Step 1 decides where the brief lives, in the precheck**, preferring a tracked path outside an ignored directory. Step 2 says how to close a skipped task, since `TaskUpdate` has no "closed with a reason" state, and writes the roster sweep into the durable brief as well as the task list, which a recorded session found empty at cleanup.
- `agent-team`: **every `SendMessage` instruction now names `main`.** The bodies said "report via SendMessage to the team lead" at 18 sites and never named a reachable address, so the model picked one; a downstream vendor measured 18 working against 8 failing across three real runs.
- `agent-team`: `SKILL.md` net 631 to 727 lines despite the template moving out to its own file.
- `agent-permissions`: the auto-mode filter now applies only to `PreToolUse` verdicts. `PostToolUse` afterthoughts are plain text rather than JSON, so the filter was swallowing them; non-`PreToolUse` events pass through untouched.

### Fixed

- `agent-team`: the `release` role's `triggers_on` had no GitLab pattern, so a repo whose only release signal is a tag-gated `.gitlab-ci.yml` never matched and the role was silently left out of the roster at init. Added `.gitlab-ci.yml` and `.gitlab/**` alongside the existing Forgejo and GitHub workflow globs. Found on a repo that publishes images, a Helm chart and a CLI on `$CI_COMMIT_TAG`, documents the full tag-then-bump-`targetRevision` GitOps procedure, and still had no releaser: exactly the two-step flow the role's body was written for. No `version:` bump, because `triggers_on` is read fresh from the library at init/update and is never stamped into a generated `.claude/agents/<role>.md`; bumping would flag every existing `release.md` as stale for a no-op body replacement.
- `agent-team`: a `--repo` regenerate reports success over a no-op unless the cache is refreshed. `dot-ai skills generate --repo <url>` rewrites every generated file from the server's cached clone: fresh mtimes, `Skills generated successfully`, exit 0, and the old content. Mode 4 now says to run `dot-ai prompts refresh` first and to verify by content, since a success line cannot distinguish a fresh render from a cached one.
- `agent-team`: fixed self-contradicting watcher guidance. The "validated watcher shape" recommended `pgrep -f <script>`, a pattern match the same file warns against; a harness that re-execs under `env -i` drops the path prefix and makes such a watcher fire a phantom "finished". Now watches by identity (`kill -0 <pid>`).
- `agent-permissions`: documented why path rules silently never fire in dippy v0.2.7. Pattern tokens align with command tokens, so `ask rm /*.git/*` matches `rm <path>` but not `rm -rf <path>`, which needs a `-*` slot; and a pattern token containing a slash is matched against a single argument and must be absolute, so `*.git/*` can never fire while `/*.git/*` matches at any depth. Also records that `set default deny` is not valid syntax and that an unparseable or missing config falls back to the global file in silence.
- `agent-permissions`: kill-switch documentation now points at `scripts/dippy-toggle.sh`, which also flags the case where `settings.json` stopped routing hooks through the wrapper, making the `OFF` marker inert.

## [0.17.0] - 2026-07-20

### Changed

- `agent-team`: **tester role gains `Edit, Write`** (version 1 to 2). The tester body already prescribes test-authoring ("add tests that exercise the new behavior", "write a new test", "Commit a deliberately-failing test on its own"), and its "read-only by default" clause forbids only *external* mutations (push, merge, PR comments, workflow_dispatch), not local file writes; but the tool allowlist omitted `Edit, Write`, so the tester could only author via Bash heredocs, against the allowlist's intent. Added the tools so the tester authors RED tests as the body already describes. Surfaced on a PRD run where authoring an e2e test leg had to be routed to a coder because the read-only tester could not write files.
- `agent-team`: **architect gains a seam-completeness review lens** (version 1 to 2). Section C (PRD writing/review) now requires: when a milestone is a SEAM that later milestones consume (for example "this pre-lands the interface that makes M2 and M3 file-disjoint"), specify EVERY field, prop, and interface member each downstream milestone will read from it; an incomplete seam is not "done", it leaks work back as authorized edits into a supposedly-frozen file. Motivated by a run where a seam milestone was declared done while under-provisioning props the next milestone needed.

### Added

- `agent-team`: new run-mode gotcha on process ownership. Do NOT tell a teammate to kill a process you have not confirmed is theirs (match the shell-snapshot path, redirected log path, and cwd against the worker's own); a teammate refusing to touch an unowned process is correct behavior, not obstruction. A stray run in a separate compose project that self-tears-down is harmless.

## [0.16.0] - 2026-07-19

### Removed

- `agent-team`: retired the entire cmux/tmux pane-layout path: the `scripts/layout-team-panes.sh` helper, the per-spawn-wave layout routine, pane/surface identification and pane-title correlation, the `tmux capture-pane` monitoring, and the cmux presence-detection notes. The skill no longer assumes teammates are visible tmux panes.

### Changed

- `agent-team`: Mode 3 now leads with the **native background model** end to end. Teammates run as background agents (the Agent tool's default) and drive the flow via automatic completion/idle notifications plus `SendMessage`/`Task*`; the lead never polls and never reads a teammate's `.output` transcript. Pre-shutdown "is it mid-work?" checks use `TaskList` plus a status question plus `git status` on the worktree, instead of pane capture. Wedged-teammate recovery is `TaskStop({task_id})` instead of `tmux kill-pane`. Context-budget monitoring drops the pane-statusline `% of 1000k` readout (no native equivalent) for the task-boundary-count heuristic. Net: about 50 lines and one bundled script lighter.

## [0.15.1] - 2026-07-19

### Changed

- `agent-team`: rewrote Mode 3 (run) for the **implicit-team API**. Claude Code exposes one implicit team per session, so `team_name` is deprecated/ignored and `TeamCreate`/`TeamDelete` are absent (confirmed 2.1.178 through 2.1.215, where the Agent tool documents `team_name` as "Deprecated; ignored. The session has a single implicit team"). Spawns now use `Agent({name, subagent_type, model, prompt})`, coordination is `SendMessage` plus the `Task*` tools, and a teammate is retired with a graceful `shutdown_request`; there is nothing to create or delete. The old `TeamCreate`/`team_name`/`TeamDelete` flow is preserved as a one-paragraph "older build" fallback in Gotchas, instead of being the main prose with a band-aid gotcha telling readers to remap it.

## [0.15.0] - 2026-07-19

### Added

- `agent-team`: **per-role versioning + staleness detection.** Each role in `roles.yaml` now carries a `version:` integer, and generated `.claude/agents/<role>.md` files stamp it in frontmatter (verified empirically: Claude Code 2.1.215 loads and spawns a subagent whose frontmatter carries the custom `version:` key). On load in a repo that already has a team, the skill reports which agents trail the library; `update` mode diffs versions and replaces only the generic body. Repo-specific tuning now lives in a dedicated `## For this repo` tail that update/sync never overwrites, so the generic body is replaceable wholesale.
- `agent-team`: new **`reflect` mode** (`/agent-team reflect`). Spawns a read-only reviewer over the session's agents to propose refactors, new roles, and version bumps; findings only, gated on user confirmation. Offered (not auto-run) at the end of a substantial run, since Claude Code exposes no session-end hook.
- `agent-team`: **downstream builtin sync.** When `roles.yaml` itself changes, `update` mode now proposes propagating the generic-body change to any downstream app that vendors these role bodies as built-in templates, preserving each copy's local `## For this repo` tail and any app-owned roles. A downstream copy whose parser rejects unknown frontmatter keys carries the new body but needs its own change to store the `version:` stamp.

### Changed

- `agent-team`: backported a downstream project's parallel-mode `coder` contract into the library `coder` body (file-scope hard boundary; no `git commit` in parallel mode; the lead integrates, commits, and runs the repo-wide gate), reconciled with the clean-tree gate. Folded into `coder` `version: 1`.

## [0.14.0] - 2026-07-17

### Added

- `upgrade-advisor`: new **step 7 — audit the runtime**, and a matching verification mode. Steps 3-4 derive the grep list from the version *delta*, so the delta is a hard ceiling on what they can find: a symbol removed in a version you upgraded *through* is invisible to them, yet stays live in your code because a removed API only fails when its code path actually **runs** (a rarely-hit path fails silently for months). Real case that prompted this: an HA `2026.6.4 → 2026.7.2` evaluation was grep-clean across the whole delta with an empty Repairs list, so the verdict was "clean upgrade" — true and useless, because `light.turn_on`'s `kelvin` parameter had been removed back in **2026.3** and five call sites had been dead ~3 months. One `system_log/list` surfaced it plus two more broken automations. Step 7 says to read the system's own error surface (structured diagnostics over log tails; per-unit execution status where it exists, since a component can fail while the app stays green and smoke tests pass) and to **baseline before upgrading**, because without a baseline "the upgrade broke this" and "this was already broken" are indistinguishable. Also: new verdict `clean, but N pre-existing faults found` (none of the existing five fit that result, and "clean" wrongly implies "working"); the intro and `description` now name the two modes, so "I've already upgraded X" routes to verification rather than a changelog-only answer; and step 1 notes that a running service pins nothing in the repo (ask the runtime, and beware reading during a restart — it answers with the old version).
- `agent-team`: **re-derive the claim at the moment you assert it**, written into the workflow-doc template so `init` puts it in every repo. A PRD run lost ~6 correction rounds to one failure repeated 9x across every role including the lead: a claim that was true when checked, asserted later without re-checking. The logic was right nearly every time; the story rotted. Corollaries: a comment is an assertion and deserves a test's mutation; presence is not efficacy (two validators can both be right and appear to conflict because they asked different questions); the experiment that justifies a choice usually also bounds it; it hides in the artifacts with no gate on them. Plus the lead's share (relay findings as claims to check, not facts to apply) and the crossing gotcha's cheap fix: verify the artifact immediately before dispatching, not before composing.

## [0.13.1] - 2026-07-16

### Changed

- `upgrade-advisor`: review pass (skill-expert agent). Added a `stay put` verdict (not upgrading is a valid outcome — don't manufacture a reason to bump); handle security/CVE-driven upgrades (target = smallest version clearing the advisory, via GHSA/osv.dev/`npm audit`/`govulncheck`/`cargo audit`) and end-of-life/LTS as first-class drivers; step 1 now reports *every* file that pins the dependency and flags drift (Dockerfile + manifest + CI, monorepo per-package pins); classification table gains a **behavioral** row (changed default/semantics that grep can't catch) and the N/A row now caveats that grep-clean is high-confidence, not proof (dynamic/re-export/string-keyed/transitive usages). Replaced the redundant "Hard-won rules" (restated the workflow) with a tighter "Traps" section of non-obvious pitfalls (grep false-negatives, behavioral changes, do-nothing-is-valid, thin-changelog → compare-view/commit-log fallback). Trimmed the description.

## [0.13.0] - 2026-07-16

### Added

- `upgrade-advisor`: new flat skill. Evaluates whether and how to upgrade a tool, framework, library, or dependency. Discovers the currently pinned version (and the file it comes from), finds both the latest released and the latest *installable* version (a packager can lag upstream — that gap is itself the answer when it blocks the bump), reads the changelog across the whole version delta (not just the newest release), then grep-classifies each breaking change and deprecation against actual codebase usage so the report is filtered to what really applies here. Covers downstream/transitive compatibility (theme min_version, plugin peers, toolchain floor) and matches caution to blast radius (irreversible target → test-first). Output is a safe / blocked / needs-work verdict plus a checklist; investigate-only by default.
- `agent-team`: new `architect` role in the role library, informed by a survey of MetaGPT, Anthropic's planner guidance, and Claude Code subagent collections (wshobson ship-mate, VoltAgent). Three dispatch shapes: pre-implementation design with a named-section output contract (Approach with rejected alternatives, File map, Contracts with mermaid where prose is ambiguous, Risks, Handoff with acceptance criteria, Open questions), post-implementation architectural-fit review, and PRD writing/review (architecture sections plus parallelism-friendly milestone decomposition when writing; feasibility, hidden coupling, independent shippability when reviewing). Halt-and-escalate triggers (external API contracts, data-affecting schema changes, auth-model changes, scope creep, insufficient info); deliverables-not-path principle (no pseudo-code diffs); writes design docs/ADRs only, never source code. SKILL.md wired: include rule (design surface incl. prds/, or multi-component repo), prompt tuning, strong model tier, dispatch guidance (design before coder on non-trivial tasks, fit-pass with the review wave, PRD flows).

### Changed

- `agent-team`: guardrails adopted from vfarcic/dot-agent-deck's applied config. `coder`: clean-tree gate before reporting done (`git status` verified, never done with uncommitted changes) and an anti-test-gaming rule (make tester-authored failing tests pass by changing production code only; a wrong test gets reported, not edited). `tester`: test-authoring discipline (extend > modify > new; assert on observable end-state so tests survive refactors; RED tests must fail for the right reason, commit them on their own, and report the exact failure signature). `release`: bounded review-wait (gate settles on CI green + reviewer posted, or a ~5-minute window elapsing; advisory comments summarized to the lead, changes-requested is a stop).

### Fixed

- `agent-team`: `roles.yaml` was not parseable as strict YAML (tester `description` contained an unquoted `: `); quoted it. Never surfaced because the skill reads the file as text, but the file should be honest YAML.

## [0.12.0] - 2026-07-09

### Added

- `done`: new flat skill, moved here from a private skills repo. End-of-session wrap-up prompt: checks `git status` (plus unpushed commits via `git log @{u}..`) across every working directory touched in the session, reviews the session for unfinished work, and reports a plain verdict on whether the session can be closed. Read-only by contract; it never commits, pushes, or edits anything to perform the check. Contained no private references beyond its own source-of-truth path, which was rewritten for this repo.

## [0.11.3] - 2026-07-05

### Changed

- `agent-team`: model guidance rewritten — the role file's `model:` frontmatter DEFINES a role's tier but is not reliably honored at spawn (observed: a `model: sonnet` documenter ran on the parent session's model); the lead must ENFORCE it by passing the Agent tool's `model` parameter on every spawn/respawn. Version-pinned auto-mode model lists (Sonnet 4.6/Opus 4.6/4.7) replaced with durable tier guidance (strong tier for reasoning-heavy roles, mid tier for mechanical, smallest tier banned) plus "verify the current auto-mode list in the Claude Code docs" — the old list predated the Claude 5 family.
- `agent-team`: new gotcha — `.claude/agents/<role>.md` files added mid-session are spawnable immediately, no session restart needed.

## [0.11.2] - 2026-07-05

### Added

- `agent-team`: `web-ux` role gains "agent-browser operational notes" — hard-won CLI gotchas from the role's first production run: absolute paths for screenshots (relative writes land in agent-browser's own cwd), `JSON.stringify` in `eval` (bare objects return `{}`), scoped-snapshot refs over `find role --name` (accessible-name collisions), refs staling on navigation, `drag` with temp `data-*` tagging for selector-less cards, inner-overflow scrolling via `scrollLeft`, IPv6-only dev-server binds (`localhost` vs `127.0.0.1`), and full-page `open` resetting SPA/mock state.

## [0.11.1] - 2026-07-05

### Changed

- `agent-team`: `web-ux` role gains a MUTATION SAFETY block — a real browser means real side effects, so the role must never perform destructive or state-mutating actions (delete buttons, merges, sends, payments) against a real backend unless the dispatch states the user explicitly permitted that exact action. Target priority: mock/demo build → disposable dummy-data stack → real stack read-only (destructive controls exercised only up to the confirmation step). Mutation-only-provable flows are reported not-validated with a proposal to spin up a mock instance. SKILL.md's dispatch guidance updated to prefer mock/isolated targets and relay the proposal/permission ask to the user.

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
