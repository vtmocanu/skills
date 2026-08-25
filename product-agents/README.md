# product-agents/ (generated)

**Do not edit these files by hand.** Every `*.md` here is generated from
`skills/agent-kit/agent-team/roles.yaml` by
`skills/agent-kit/agent-team/scripts/publish_roles.py`.

## What this is

A tail-free, product-roster set of Claude Code subagent Markdown files: one
`<role>.md` per role in `roles.yaml`, carrying the role's generic `prompt_body`
with **no** `## For this repo` tail.

This is the publishing half of an integration with a downstream runtime (uzi,
PRD #602): uzi points a configurable agent-source folder at `product-agents/`
(the agreed default) and reads these files into its agent store. uzi runs none
of the tooling here; it only reads the emitted `.md`. The uzi-side wiring (the
source-folder setting and how the generic roster reconciles with uzi's own
adapted roles) is coordinated separately in PRD #602, not by this repo.

Each file is frontmatter (`name`, `version`, `description`, `tools`, `model` in
that order; `tools`/`model` omitted when empty, `version` omitted when absent)
followed by one blank line and the body. The shape matches uzi's frontmatter
parser exactly.

## Roster

Every role in `roles.yaml` is published. A role opts out with `publish: false`
in `roles.yaml` (absent field means published). There is no role list in the
generator, so adding a role to `roles.yaml` publishes it automatically.

## Regenerating / the drift check

```sh
python3 skills/agent-kit/agent-team/scripts/publish_roles.py          # rewrite this folder
python3 skills/agent-kit/agent-team/scripts/publish_roles.py --check  # verify, exit 1 on drift
```

CI (`.github/workflows/publish-roles.yml`) regenerates on every PR and push to
`main` and fails if this folder is out of sync with `roles.yaml`, so a
`roles.yaml` edit that forgets to regenerate reddens the build.
