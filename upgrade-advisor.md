---
name: upgrade-advisor
description: Evaluates whether and how to upgrade a tool, framework, library, or dependency. Discovers the currently pinned version, finds the latest (and the latest actually-installable) version, reads the changelog across the whole version delta, and reports which breaking changes and deprecations actually touch this codebase (by grepping for real usage) plus the features and refactors worth adopting. Produces a safe / blocked / needs-work verdict with a concrete checklist. Use when the user asks to upgrade, bump, or update a version, mentions a new release or the "latest version", or asks whether a dependency upgrade is safe or beneficial, even if they do not name this skill. Triggers include "upgrade", "bump", "update dependency", "new release", "latest version", "breaking changes", "is it safe to upgrade".
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

Report the exact current version **and the file it came from**. If the repo has a remote, confirm the local checkout matches the remote pin (a local branch can be behind). The lockfile is authoritative over the manifest range.

### 2. Find the latest — and the latest *installable*

`latest released` and `latest this project can actually pull` are different numbers. Find both:

- **Latest released**: GitHub releases API (`/repos/OWNER/REPO/releases/latest`), or the ecosystem registry (npm `registry.npmjs.org/PKG`, PyPI `pypi.org/pypi/PKG/json`, crates.io, Docker tags, Maven Central).
- **Latest installable via THIS project's manager**: a nixpkgs/devbox pin can lag upstream by days-to-weeks; a corporate registry may mirror a subset; a Docker base may only publish certain tags. Verify the target resolves *before* recommending it (e.g. devbox: `search.devbox.sh/v2/resolve?name=X&version=Y`; nixpkgs: nixhub; npm: the registry `versions` map). **If the target is not installable yet, say so — the bump is blocked on the packager, not on the code**, and that is the whole answer.

### 3. Read the changelog across the WHOLE delta

Do not read only the newest release. Walk every intermediate version between current and target — a breaking change can land in any of them. Fetch the official changelog / release notes and the project's own **migration/upgrade guide** if one exists. Prefer authoritative sources: the project's release notes and migration docs; a docs MCP such as context7 for library APIs when available; the source repo's `CHANGELOG`/`BREAKING` files. **Quote breaking changes and deprecations verbatim** — paraphrasing loses the exact symbol/flag/config key you need to grep for.

### 4. Cross-reference every change against actual usage — this is the core value

A breaking change you don't use is N/A. For each breaking change and deprecation, grep the codebase for the affected symbol, config key, CLI flag, API, or component. Classify each:

| Class | Meaning | Action |
|---|---|---|
| Breaking — **affects us** | We use the removed/changed thing | Must fix before upgrade; show the site(s) |
| Breaking — **N/A** | Not present anywhere (grep clean) | Note it as checked-and-clear (proves diligence) |
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

- **Verdict** — one of: `already current` / `safe bump` / `blocked (reason)` / `needs work (N items)`.
- **Versions** — current (+ source file) → latest released → latest installable.
- **Breaking changes vs. our usage** — the table from step 4; verbatim quotes for anything that affects us, and the N/A list.
- **Deprecations** — with removal runway.
- **Benefits / refactors** — features worth adopting, workarounds we can now drop.
- **Downstream compat** — plugin/theme/peer constraints checked.
- **Plan** — a concrete checklist (compile-first vs. direct, fallback if it fails, sibling pins to move, docs/lockfile to regen). Offer to open a tracking issue or apply the change; don't apply unprompted.

## Hard-won rules

- **Released is not installable.** Always confirm the project's own package manager can resolve the target before recommending it. A just-cut upstream release the packager hasn't picked up yet = blocked-on-packager, and that's the answer — don't send the user chasing a bump they can't make.
- **Read the whole version delta**, not just the newest release notes. Breaking changes hide in intermediate versions.
- **Grep before you worry.** Most breaking changes won't touch a given project. Turn the changelog into a filtered list of what actually applies here; that filtering is the value you add.
- **Quote breaking changes verbatim.** Keep the exact flag/key/symbol so the grep is precise.
- **Deprecation ≠ removal.** It's a warning with a runway — record the version it's removed in, migrate on your schedule, don't panic-fix.
- **Check the dependents**, not just the thing being upgraded (theme min_version, plugin peers, toolchain floor).
- **Blast radius sets the method.** Irreversible target → test-first. Cheap target → direct with CI as the net.
- **Investigate by default.** Produce the verdict and plan; apply only when asked.
