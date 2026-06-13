---
name: cmux-diagnostics
description: "Run end-user cmux diagnostics. Use when cmux hooks, notifications, session restore, settings, browser automation, socket access, CLI control, or agent resume behavior is not working, or when the user asks for a cmux health check, doctor report, or support-safe debug summary."
---

## Document Location
This document lives in [github.com/vtmocanu/skills](https://github.com/vtmocanu/skills) at `cmux-diagnostics/SKILL.md`.

> **Note**: This is the source of truth. Generated copies in your agent's skills directory (e.g. Claude Code's `~/.claude/commands/`) are derived from this file via `dot-ai skills generate --repo https://github.com/vtmocanu/skills`. Edit here and regenerate; never edit generated copies. Upstream source: `manaflow-ai/cmux` (skills/cmux-diagnostics/); re-sync from upstream rather than authoring from scratch.

# cmux Diagnostics

Use this skill to collect and interpret support-safe cmux diagnostics for end users. Default to read-only checks. Do not dump hook config files, session stores, prompt logs, tokens, or environment secrets.

## Quick Report

Run the bundled read-only diagnostic script first:

```bash
# Bundled with this skill (the dot-ai generator rewrites ./scripts to the absolute install path)
./scripts/cmux-diagnostics
```

Use `--include-context` only when workspace names, cwd paths, and current cmux identifiers are relevant to the user-reported issue:

```bash
./scripts/cmux-diagnostics --include-context
```

## What to Check

1. CLI and socket health:

   ```bash
   command -v cmux
   cmux ping
   cmux capabilities --json
   ```

   If socket commands fail, check whether the agent is running inside a cmux terminal and whether socket automation is enabled.

2. Settings health (invokes the sibling `cmux-settings` skill's bundled helper — its install location depends on the host: `~/.claude/commands/dot-ai-cmux-settings/scripts/cmux-settings` under dot-ai/Claude Code, `~/.agents/skills/cmux-settings/scripts/cmux-settings` under the upstream cmux installer, `~/.codex/skills/cmux-settings/scripts/cmux-settings` under `skills.sh`):

   ```bash
   CMUX_SETTINGS_HELPER="$(command -v cmux-settings 2>/dev/null \
     || { for p in \
       "$HOME/.claude/commands/dot-ai-cmux-settings/scripts/cmux-settings" \
       "$HOME/.agents/skills/cmux-settings/scripts/cmux-settings" \
       "$HOME/.codex/skills/cmux-settings/scripts/cmux-settings"; do
         [ -x "$p" ] && { printf '%s' "$p"; break; }
       done; })"
   "$CMUX_SETTINGS_HELPER" validate
   "$CMUX_SETTINGS_HELPER" get terminal.autoResumeAgentSessions
   ```
   If `terminal.autoResumeAgentSessions` is false, cmux restores panes but will not automatically resume saved agent sessions.

3. Hook installation:

   ```bash
   cmux hooks setup --agent codex
   cmux hooks setup --agent opencode
   cmux hooks setup
   ```

   Only run install or uninstall commands after the user agrees. `cmux hooks setup` installs supported agents found on PATH and skips missing agents.

4. Session restore evidence:

   ```bash
   ls -lh ~/.cmuxterm/*-hook-sessions.json 2>/dev/null
   ```

   Missing session stores usually means the agent has not run inside cmux since hooks were installed, hooks are disabled, or the agent integration does not support resume capture.

5. Notification path:

   ```bash
   cmux notify "cmux diagnostic test"
   ```

   Use this only when the user is ready for a visible test notification.

## Interpretation

- `cmux` not found: the CLI is not installed or not on PATH for this shell.
- `cmux ping` fails: app is not reachable through the current socket path, the app is closed, or automation access is disabled.
- No `CMUX_WORKSPACE_ID` or `CMUX_SURFACE_ID`: the command is probably running outside a cmux terminal. Some hooks intentionally no-op outside cmux.
- Hook config exists but no session store: run one supported agent inside cmux after installing hooks, then re-check.
- Session store exists but restore does not launch agents: check `terminal.autoResumeAgentSessions` and whether the saved executable still exists on PATH.
- Settings validation fails: fix the config first. Invalid config can make later symptoms misleading.

## Rules

- Stay read-only until the user asks to fix something.
- Never print raw hook files, session JSON, prompt logs, shell history, tokens, or API keys.
- Summarize file presence, size, modified time, and marker presence instead of contents.
- Prefer narrow fixes such as `cmux hooks setup --agent codex` over reinstalling every integration.
- After a fix, rerun the diagnostic script and report the changed lines.
