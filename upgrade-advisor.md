---
name: upgrade-advisor
description: Evaluates whether and how to upgrade a tool, framework, library, or dependency. Discovers the pinned version(s), finds the latest released and the latest actually-installable version, reads the changelog across the whole version delta, and grep-classifies each breaking change and deprecation against real codebase usage so the report covers only what applies here — plus the features and refactors worth adopting. Handles security/CVE- and end-of-life-driven upgrades; emits a safe / stay-put / blocked / needs-work verdict, investigate-only by default. Use when the user asks to upgrade, bump, or update a tool or dependency, mentions its new release or latest version, or asks whether an upgrade is safe or worth it. Triggers include "upgrade", "bump", "update dependency", "breaking changes", "is it safe to upgrade".
---

# Upgrade Advisor

## Document Location

This document is located at: `~/stuff/gitrepos/gh/vtmocanu/skills/upgrade-advisor.md` (public repo: github.com/vtmocanu/skills)

> **Note**: This is the source of truth. The skill copy at `~/.claude/commands/dot-ai-upgrade-advisor/SKILL.md` is derived from this file. All edits should be made here. After editing, use `/dot-ai-skills` to regenerate; never copy files directly to `~/.claude/commands/`.

Evaluate an upgrade before doing it: what changed, what of that actually affects this project, and what is worth adopting. The output is a decision plus a checklist, not a blind version bump. Default to **investigate-only** unless the user asked you to apply the change.

## Workflow

Run these in order. Steps 1-2 and the changelog fetch in 3 are independent — issue those tool calls in parallel.

### 1. Discover the current version — don't assume it

Find where the version is actually pinned. It is rarely in an obvious single place, and released ≠ what this project runs. Check, in the project:

- **Language/tool managers**: `devbox.json`/`devbox.lock`, `.tool-versions` (asdf/mise), `.nvmrc`, `runtime.txt`, `flake.nix`.
- **Package manifests + lockfiles**: `package.json` + `package-lock.json`/`yarn.lock`/`pnpm-lock.yaml`, `go.mod`/`go.sum`, `pyproject.toml`/`requirements.txt`/`uv.lock`, `Cargo.toml`/`Cargo.lock`, `Gemfile.lock`, `pom.xml`, `build.gradle`.
- **Containers/CI**: `Dockerfile` `FROM`, `.github`/`.forgejo`/`.gitlab-ci` workflow pins, Helm `Chart.yaml`/`values.yaml`, action `uses:` SHAs/tags.

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

## Output

Lead with the verdict, then the evidence:

- **Verdict** — one of: `already current` / `safe bump` / `stay put (installable & clean, not worth the churn)` / `blocked (reason)` / `needs work (N items)`.
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
