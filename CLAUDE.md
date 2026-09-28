# CLAUDE.md

## `agent-team/roles.yaml` has an external consumer: uzi

`skills/agent-kit/agent-team/roles.yaml` is read by a **different, public** repo,
so a structural change here can silently break it:

**uzi** (https://github.com/vtmocanu/uzi) ships a subset of these roles as builtin
agents. It vendors a distilled `name -> version` snapshot and runs a weekly bot
that re-reads this file to detect drift, parsing only each role's `name:` and
`version:` fields (and the top-level `roles:` list shape). It does NOT list which
roles it ships here (that stays in uzi, so this note never goes stale).

- **Bumping a `version:` is fine** and is the intended signal: uzi's bot opens a
  manifest-bump PR that reddens uzi's drift test until a maintainer copies the new
  body. Since uzi PRD #1849, uzi's builtins copy the published `product-agents/`
  bodies **verbatim** (uzi-only rules live in uzi's worker prompt), and a uzi
  admin can also sync `product-agents/` straight into a live instance.
- **Keep every body runtime-neutral.** It must read correctly on a laptop and
  on a uzi worker: name "the scratch directory your runtime provides" rather than
  a path, allow a detached checkout only "where your runtime permits one", and
  never claim an ignored file cannot be staged. uzi's
  `api/internal/agenttmpl/scratch_guidance_test.go` lists the phrases a body must
  not contain.
- **Adding/removing a role needs no uzi change** (uzi tracks a fixed subset and
  adds builtins by hand).
- **Renaming/restructuring `name:`/`version:` (or the `roles:` shape) breaks uzi's
  parser.** Update uzi in the same effort: `api/internal/agenttmpl/library/`
  (vendored manifest + runbook), `scripts/refresh-role-manifest.sh` (the bot's
  parser), and `api/internal/agenttmpl/builtins.go` (the `version:` frontmatter
  parser).

Rule of thumb: a role's **content** is local to this repo; a change to the
**shape** of `name:`/`version:` is a cross-repo change that must touch uzi too.
