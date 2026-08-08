# Retired skills

Skills in this folder are **retired**: no longer maintained and not installed by any
generator. They are kept here for reference and history rather than deleted outright.

They live under `retired/` on purpose. Skill generators discover skills at the
repository **root** (a root `<name>/SKILL.md` folder or, for dot-ai, a root `<name>.md`
file). Nesting a skill under `retired/<name>/SKILL.md` takes it out of that set, so
neither the dot-ai `--repo` generator nor `npx skills add` picks it up as an active
skill. To bring one back, move its folder back to the repository root.

## Contents

The `cmux` family, which controlled [cmux](https://github.com/manaflow-ai/cmux)
(topology/routing, browser automation, customization, diagnostics, keyboard shortcuts,
markdown viewer, settings, and workspace):

| Retired skill | What it did |
|---|---|
| `cmux` | Control cmux topology and routing: windows, workspaces, panes/surfaces, focus, moves, reorder, identify, trigger flash. |
| `cmux-browser` | Browser automation with cmux: open sites, interact with pages, wait for state, extract data. |
| `cmux-customization` | Customize cmux: `cmux.json` actions, custom commands, layouts, buttons, Command Palette, shortcuts, notifications, browser routing, presets. |
| `cmux-diagnostics` | cmux diagnostics and health checks for hooks, notifications, session restore, settings, socket access, CLI control. |
| `cmux-keyboard-shortcuts` | Customize, rebind, unbind, reset, audit, or template cmux keyboard shortcuts. |
| `cmux-markdown` | Open markdown files in a formatted viewer panel with live reload. |
| `cmux-settings` | View and edit cmux settings in `~/.config/cmux/cmux.json`. |
| `cmux-workspace` | Work inside the current cmux workspace and terminal. |
