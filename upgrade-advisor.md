---
name: upgrade-advisor
description: Evaluates whether and how to upgrade a tool, framework, library, or dependency. Discovers the pinned version(s), finds the latest released and latest installable version, reads the changelog across the whole version delta, and grep-classifies each breaking change and deprecation against real codebase usage, so the report covers only what applies here plus features worth adopting. Handles security/CVE- and end-of-life-driven upgrades; emits a safe / stay-put / blocked / needs-work verdict, investigate-only by default. Also verifies an already-done upgrade by auditing the runtime's error surface, catching latent breakage the changelog delta cannot reveal. Use when the user asks to upgrade, bump, or update a tool or dependency, mentions its new release or latest version, asks whether an upgrade is safe or worth it, or just upgraded and wants to know what broke. Triggers include "upgrade", "bump", "update dependency", "breaking changes", "is it safe to upgrade", "just upgraded", "what broke after the upgrade".
---

# Upgrade Advisor

## Document Location

This document is located at: `~/stuff/gitrepos/gh/vtmocanu/skills/upgrade-advisor.md` (public repo: github.com/vtmocanu/skills)

> **Note**: This is the source of truth. The skill copy at `~/.claude/commands/dot-ai-upgrade-advisor/SKILL.md` is derived from this file. All edits should be made here. After editing, use `/dot-ai-skills` to regenerate; never copy files directly to `~/.claude/commands/`.

Evaluate an upgrade before doing it: what changed, what of that actually affects this project, and what is worth adopting. The output is a decision plus a checklist, not a blind version bump. Default to **investigate-only** unless the user asked you to apply the change.

**Two modes — read the request first.** "Should I upgrade X?" is the *evaluation* mode below. But "I've already upgraded X" is a **verification** request: the decision is made, and the question is what it broke. In that mode the deliverable is step 7 (audit the runtime), and steps 3-4 only serve to explain what you find. Do not hand back a purely changelog-derived verdict for an upgrade that already happened — the running system is right there, so use it.

## Workflow

Run these in order. Steps 1-2 and the changelog fetch in 3 are independent — issue those tool calls in parallel.

### 1. Discover the current version — don't assume it

Find where the version is actually pinned. It is rarely in an obvious single place, and released ≠ what this project runs. Check, in the project:

- **Language/tool managers**: `devbox.json`/`devbox.lock`, `.tool-versions` (asdf/mise), `.nvmrc`, `runtime.txt`, `flake.nix`.
- **Package manifests + lockfiles**: `package.json` + `package-lock.json`/`yarn.lock`/`pnpm-lock.yaml`, `go.mod`/`go.sum`, `pyproject.toml`/`requirements.txt`/`uv.lock`, `Cargo.toml`/`Cargo.lock`, `Gemfile.lock`, `pom.xml`, `build.gradle`.
- **Containers/CI**: `Dockerfile` `FROM`, `.github`/`.forgejo`/`.gitlab-ci` workflow pins, Helm `Chart.yaml`/`values.yaml`, action `uses:` SHAs/tags.
- **A running service/appliance pins nothing in the repo** — the version lives in the runtime, so ask it (`ha core info`, `/api/config`, `kubectl version`, `SELECT version()`). Beware reading *during* a restart: it answers with the old version until the new process is up, so confirm from two sources if the number looks stale.

Report the exact current version **and every file that pins it** — the same dependency is often pinned in several places (Dockerfile `FROM` + manifest + CI `uses:`) that can silently disagree; list them all and flag any drift. In a monorepo, note per-package pins that differ, since they may have to move together. If the repo has a remote, confirm the local checkout matches the remote pin (a local branch can be behind). The lockfile is authoritative over the manifest range.

### 2. Find the latest — and the latest *installable*

`latest released` and `latest this project can actually pull` are different numbers. Find both:

- **Latest released**: GitHub releases API (`/repos/OWNER/REPO/releases/latest`), or the ecosystem registry (npm `registry.npmjs.org/PKG`, PyPI `pypi.org/pypi/PKG/json`, crates.io, Docker tags, Maven Central).
- **Latest installable via THIS project's manager**: a nixpkgs/devbox pin can lag upstream by days-to-weeks; a corporate registry may mirror a subset; a Docker base may only publish certain tags. Verify the target resolves *before* recommending it (e.g. devbox: `search.devbox.sh/v2/resolve?name=X&version=Y`; nixpkgs: nixhub; npm: the registry `versions` map). **If the target is not installable yet, say so — the bump is blocked on the packager, not on the code**, and that is the whole answer.

**Two drivers change the target selection:**

- **Security/CVE-driven**: pin down the *fixing* version from the advisory first (GitHub Security Advisories / osv.dev, or the ecosystem scanner: `npm audit` / `pip-audit` / `govulncheck` / `cargo audit`). The target becomes the **smallest** version that clears the CVE — prefer a patch/backport over a major bump — and urgency rises, so more blast radius is acceptable.
- **End-of-life-driven**: check whether the *current* pin is EOL / out-of-support (endoflife.date) — being unmaintained is itself a reason to move, absent any needed feature — and whether the *target* is an LTS / still-maintained line vs a short-lived release. State the target's support window.

### 3. Read the changelog across the WHOLE delta

Do not read only the newest release. Walk every intermediate version between current and target — a breaking change can land in any of them. Fetch the official changelog / release notes and the project's own **migration/upgrade guide** if one exists. Prefer authoritative sources: the project's release notes and migration docs; a docs MCP such as context7 for library APIs when available; the source repo's `CHANGELOG`/`BREAKING` files. **Quote breaking changes and deprecations verbatim** — paraphrasing loses the exact symbol/flag/config key you need to grep for.

### 4. Cross-reference every change against actual usage — this is the core value

A breaking change you don't use is N/A. For each breaking change and deprecation, grep the codebase for the affected symbol, config key, CLI flag, API, or component. Classify each:

| Class | Meaning | Action |
|---|---|---|
| Breaking — **affects us** | We use the removed/changed thing | Must fix before upgrade; show the site(s) |
| Breaking — **behavioral** | Same API, changed default/semantics (e.g. a default timeout 30s→5s) | grep can't catch it — the symbol is unchanged; read the changelog's default/behavior-change notes and hand-check the call sites |
| Breaking — **N/A** | Not present anywhere (grep clean) | Note it as checked-and-clear — but grep-clean is high-confidence, not proof; flag anything referenced dynamically / via re-export / string-keyed config / a transitive dep as "likely N/A, verify" |
| Deprecation | Still works, warns; has a removal runway | Migrate opportunistically; record the removal version |
| Feature / improvement | New capability or perf/security fix | Flag if it lets us delete workarounds or gain for free |
| Refactor opportunity | Our code can simplify given the new version | Propose it, don't auto-apply |

Listing the N/A items explicitly matters: it's the difference between "I checked, nothing hits us" and "I skimmed the headlines."

### 5. Check downstream / transitive compatibility

The tool doesn't upgrade in isolation. Verify the target version against everything that depends on it: theme/plugin `min_version` and peer-dependency ranges, framework support matrices, the toolchain's own version floor, sibling pins that must move together. A target that breaks a required plugin is blocked even if the tool itself is clean.

### 6. Weigh blast radius before recommending *apply*

Match caution to reversibility. Hard-to-recover targets (OTA-only firmware, production DB engines, a base image baked into many downstream builds) get **build/compile/test-first, then apply**. Cheap-to-revert targets (a dev CLI, a lockfile bump behind CI) can move directly. State which regime applies.

### 7. Audit the runtime — the changelog cannot tell you what is *already* broken

Steps 3-4 derive the grep list from the **delta**, so the delta is a hard ceiling on what they can find. A symbol removed in a version you upgraded *through* is invisible to them, yet may still be live in your code — because a removed API only fails when its code path actually **runs**, so a rarely-hit path (a dawn-only job, an error branch, a seasonal task) stays broken and silent for months. Grep-clean across the delta therefore does **not** mean working.

So read the system's own error surface, not only its release notes:

- **Structured diagnostics beat log tails**, and are usually one call: Home Assistant `system_log/list`, `django-admin check`, `govulncheck ./...`, `npm ls`, build-time deprecation warnings, a `/health` or diagnostics endpoint. A log tail shows a window; the error surface shows every distinct fault with a count.
- **Per-unit execution status where it exists** — a component can be failing while the app itself is green and every smoke test passes (HA automation traces expose `script_execution: error`).

**Baseline before you upgrade** whenever you can, and re-read after. Without a baseline you cannot separate *the upgrade broke this* from *this was already broken*, and you will misattribute in both directions. If all you have is logs, timestamps do the same job: compare against the upgrade/restart moment.

**A log window that contains the restart will fabricate a sustained rate.** The upgrade restarts the process, the process re-syncs its state on start, and that burst lands inside whatever window you just asked for — so `logs --since=15m` straight after an upgrade, divided by 15 minutes, invents a steady rate that does not exist. Before you propose *any* suppression (a log-pipeline drop rule, a raised log level, an alert silence), get the **shape**, not an average: bucket per minute and compare against a pre-change baseline. Burst-then-quiet and genuinely-sustained look identical in a total, and demand opposite decisions — the burst is usually a one-off cost per restart and worth nothing to suppress, while a permanent carve-out risks silently swallowing real errors later.

Measured example: a cert-manager `1.20.3 → 1.21.0` audit reported "~5 lines/sec sustained" of a known-benign warning, computed from `kubectl logs --since=15m` — a window that happened to span the pod start. The per-minute shape told the real story: **4,746 lines in the start minute, 7 the next, ~0 thereafter**, against a pre-upgrade baseline of 3,475 bytes/10min and a post-upgrade steady state of 4,515 bytes/10min. Real cost: ~3.3 MB per restart, not a rate. The "sustained" reading had nearly justified a permanent drop rule for a problem that did not exist.

Measured example: an HA `2026.6.4 → 2026.7.2` audit came back grep-clean across the whole delta, the Repairs list was empty, and every dashboard rendered — verdict "clean upgrade", which was true and useless. One `system_log/list` then showed `extra keys not allowed @ data['kelvin']`: `light.turn_on`'s `kelvin` parameter had been removed back in **2026.3**, and five call sites had been dead for ~3 months. Nothing in the 2026.7 delta could ever have surfaced it. The same call also exposed two more automations broken on entities that had never existed.

## Output

Lead with the verdict, then the evidence:

- **Verdict** — one of: `already current` / `safe bump` / `stay put (installable & clean, not worth the churn)` / `blocked (reason)` / `needs work (N items)` / `clean, but N pre-existing faults found` (step 7 turned up breakage this upgrade did not cause — say so plainly rather than letting "clean" imply "working").
- **Versions** — current (+ source file) → latest released → latest installable.
- **Breaking changes vs. our usage** — the table from step 4; verbatim quotes for anything that affects us, and the N/A list.
- **Deprecations** — with removal runway.
- **Benefits / refactors** — features worth adopting, workarounds we can now drop.
- **Downstream compat** — plugin/theme/peer constraints checked.
- **Plan** — a concrete checklist (compile-first vs. direct, fallback if it fails, sibling pins to move, docs/lockfile to regen). Offer to open a tracking issue or apply the change; don't apply unprompted.

## Traps (non-obvious — the steps above cover the rest)

- **grep-clean is high-confidence, not proof.** Dynamic access, re-exports, string-keyed config, wrapper layers, and transitive deps hide usages a symbol grep won't see. Flag those as "likely N/A, verify" rather than a hard clear.
- **Behavioral changes keep the same symbol.** A changed default or semantics applies to you even though grep finds your call site unchanged, so a pure symbol grep passes it through. Read the changelog's default/behavior-change notes and hand-check the call sites.
- **Not upgrading is a valid outcome.** Installable + grep-clean but no fix you need, no feature you'd use, and non-trivial blast radius → recommend `stay put` and say why. Don't manufacture a reason to bump.
- **A thin changelog isn't a clean one.** If release notes are sparse or the project tagged without them, fall back to the compare view (`github.com/OWNER/REPO/compare/vA...vB`) or the commit log — breaking changes sometimes live only in commits.
- **Deprecation ≠ removal.** It's a warning with a runway — record the version it's removed in, migrate on your schedule, don't panic-fix.
