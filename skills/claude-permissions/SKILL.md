---
name: claude-permissions
description: Manage Claude Code permissions via Dippy config files (global and project-level) and settings.json (for non-Bash/non-MCP permissions like Read, WebFetch, Skill). Use when user asks to "permit", "allow", "whitelist", or "add permission" for a command or tool. Also use when troubleshooting "permission denied" errors, reviewing current permissions, or cleaning up stale entries.
---

# Claude Code Permissions Management

## Document Location

This document is located at: `~/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/SKILL.md` (public repo: github.com/vtmocanu/skills)

> **Note**: This is the source of truth. The installed copy at `~/.claude/skills/claude-permissions/SKILL.md` is derived from this file by the `npx skills` package manager; edit here, then run `npx skills update` to re-pull it. Never edit the installed copy.

> **Dippy docs**: Configuration syntax and behavior are documented at <https://github.com/ldayton/Dippy/wiki>. If you encounter syntax you're unsure about, fetch the relevant wiki page to verify before making changes. Key pages: [Configuration](https://github.com/ldayton/Dippy/wiki/Configuration), [MCP Tools](https://github.com/ldayton/Dippy/wiki/MCP-Tools), [File Editing](https://github.com/ldayton/Dippy/wiki/File-Editing), [Afterthoughts](https://github.com/ldayton/Dippy/wiki/Afterthoughts), [Handler Model](https://github.com/ldayton/Dippy/wiki/Handler-Model), [Security Model](https://github.com/ldayton/Dippy/wiki/Security-Model).

## Entry Point

When invoked without specific instructions, ask what the user wants to do:
1. Permit a new command or MCP tool? (Dippy config)
2. Permit a new Read/WebFetch/Skill entry? (settings.json)
3. Review current permissions?
4. Troubleshoot a permission error?
5. Clean up stale permissions?
6. Something else?

## Architecture Overview

Permissions are split across two systems:

| Permission type | Managed by | Config files |
|---|---|---|
| **Bash commands** | Dippy | `~/.dippy/config` (global), `.dippy` (project) |
| **MCP tools** | Dippy | `~/.dippy/config` (global), `.dippy` (project) |
| **File redirects** | Dippy | `~/.dippy/config` (global), `.dippy` (project) |
| **Read, WebFetch, WebSearch, Skill** | settings.json | `~/mackup/confs/claude/settings.json` (global), `.claude/settings.local.json` (project) |

Dippy runs as a PreToolUse hook (configured in settings.json) for both `Bash` and `mcp__.*` matchers. A PostToolUse hook enables afterthoughts.

> **CRITICAL: Dippy is invoked through a wrapper, not directly.** The settings.json hook command is `~/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/dippy-with-auto-fallback.sh`, not bare `dippy`. The wrapper changes how `ask` verdicts behave **in auto mode** (the permission mode that autonomous runs and spawned agents/teammates use). See [Auto-mode fallback wrapper](#auto-mode-fallback-wrapper) below. This is the single most surprising part of the setup: in auto mode a plain `ask` rule does **not** prompt the human.

### Auto-mode fallback wrapper

The wrapper reads `permission_mode` from the hook payload and branches (this is the behavior in the default **ACTIVE** state; see [Three states](#three-states-active--allow-only--disabled) for the two marker-file states that pre-empt it):

- **Non-auto modes** (`default`, `acceptEdits`, `plan`, etc.): the payload is handed straight to `dippy`. Normal behavior, `ask` rules surface an interactive prompt to the user.
- **`auto` mode**: the wrapper runs `dippy`, inspects the decision, and forwards **only**:
  - `allow` / `deny` verdicts (the whitelist + blocklist are always enforced), and
  - `ask` verdicts **whose `permissionDecisionReason` contains the literal token `[ASK]`**.

  Any other `ask` (no `[ASK]` marker) is **dropped** (the wrapper emits nothing and `exit 0`s), which hands the decision to auto-mode's own classifier, that typically runs routine commands **without prompting the human**.

**Consequence**: in auto mode, only `ask` rules tagged with `[ASK]` in their message escalate to the user. Everything else dippy would have asked about is silently auto-handled. So the `[ASK]` prefix is a real, load-bearing convention, not decoration: use it on any operation you want a human prompt for even during autonomous/agent runs.

| Dippy verdict | Reason has `[ASK]`? | Non-auto mode | Auto mode |
|---|---|---|---|
| `allow` | n/a | runs, no prompt | runs, no prompt |
| `deny` | n/a | blocked | blocked |
| `ask` | yes | **user prompted** | **user prompted** |
| `ask` | no | **user prompted** | dropped, auto-classifier decides (usually runs, no prompt) |

Worked example from the current config: `ask git push "[ASK] Confirm push target"` (has `[ASK]`) prompts the human even in auto mode, but `ask git commit "Confirm commit"` and `ask git add "Confirm staging"` (no `[ASK]`) are silently handled by the auto-classifier in auto mode, no prompt. When authoring a new `ask` rule, decide deliberately whether it needs `[ASK]`: write/destructive operations that must always reach a human should carry it.

### Three states: ACTIVE / ALLOW-ONLY / DISABLED

Dippy has no enable/disable flag of its own (`dippy --help`, v0.2.7). The state lives in two marker files that the wrapper checks per invocation:

| State | Marker | What the wrapper forwards | Net effect |
|---|---|---|---|
| **ACTIVE** (default) | none | `allow`, `deny`, and `ask` (plain `ask` filtered in auto mode, per the table above) | Full ruleset, full guardrails |
| **ALLOW-ONLY** | `~/.dippy/ALLOW_ONLY` | `allow` only | Whitelist skips the host's auto classifier; **everything else** is decided by it |
| **DISABLED** | `~/.dippy/OFF` | nothing, for every event | Dippy bypassed end to end |

`OFF` wins if both markers exist. `~/.dippy` is a symlink into the mackup repo, so the markers land at `confs/.dippy/`; both are gitignored on purpose (machine-local state, not synced config).

No restart is needed for any transition: Claude Code caches the hook *command* from settings.json at startup, but the *script* is re-read on every invocation, so a switch applies to the running session immediately. (Editing the hook entries in settings.json, by contrast, does require a restart.)

On this Mac, prefer `mackup/scripts/dippy-toggle.sh` (on `PATH` via `~/scripts`) over touching the markers by hand. It reports state, keeps the two markers mutually exclusive, and warns if `settings.json` has stopped routing hooks through `dippy-with-auto-fallback.sh` or if the wrapper it points at is a stale clone with no `ALLOW_ONLY` branch (in either case the marker is a no-op):

```bash
dippy-toggle.sh              # status (default)
dippy-toggle.sh on           # ACTIVE      - full dippy (clears both markers)
dippy-toggle.sh allow-only   # ALLOW-ONLY  - whitelist only
dippy-toggle.sh off          # DISABLED    - bypass dippy
dippy-toggle.sh toggle       # flip on <-> off
dippy-toggle.sh cycle        # rotate active -> allow-only -> off -> active
```

#### ALLOW-ONLY: whitelist-only, a noise filter

The wrapper runs dippy, forwards the verdict only when it is `allow`, and emits nothing otherwise:

```bash
[ -f "${HOME}/.dippy/ALLOW_ONLY" ] && forward only `allow`
```

A hook `allow` short-circuits the host's permission flow, so whitelisted commands never reach the auto classifier — that is the point of the state. Everything not on the whitelist falls through to whatever the host would have done: the classifier in `auto` mode, an ordinary prompt in the other modes.

**`deny` rules and `[ASK]` escalations do NOT fire in this state.** Verified against dippy 0.2.7 on 2026-08-04:

| Command | ACTIVE verdict | ALLOW-ONLY: what Claude sees |
|---|---|---|
| `cat /etc/hosts` | `allow` | `allow` — classifier skipped |
| `kubectl get pods` | `allow` | `allow` — classifier skipped |
| `helm install foo bar` | `deny` ("Use GitOps…") | nothing — classifier decides |
| `brew install foo` | `ask` `[ASK]` | nothing — classifier decides |
| `some-unknown-tool --wat` | `ask` | nothing — classifier decides |

PostToolUse afterthoughts still fire in ALLOW-ONLY (they are guidance text, not a permission decision, and the event check runs before the filter).

#### DISABLED: a safety-off switch, not a noise filter

While `~/.dippy/OFF` exists the wrapper emits nothing and `exit 0`s for every event, so:

- **The `deny` blocklist is not enforced** — `rm -rf`, `git push --force`, `deny-redirect` secret-path guards, `deny-mcp` rules all stop firing.
- `allow` rules stop firing too, so in **non-auto** modes routine commands start prompting (more prompts, not fewer). In **auto** mode everything falls to auto-mode's own classifier, which usually runs commands without prompting.
- Afterthoughts go silent too, since the kill-switch fires before the event check.

Prefer narrower alternatives when they fit:

| Goal | Better tool than the kill-switch |
|---|---|
| Keep the fast-path allows but stop all dippy prompts | `dippy-toggle.sh allow-only` (note: drops `deny` too) |
| Stop prompts in one repo | `set default allow` in that repo's `.dippy` (global `deny` rules still fire) |
| Stop prompts for one command | `allow <cmd>` rule in the project `.dippy` |
| Swap the whole ruleset for a run | `DIPPY_CONFIG=/path/to/other-config claude` |

Verify the current state:

```bash
# which state?  (or just: dippy-toggle.sh status)
ls -la ~/.dippy/OFF ~/.dippy/ALLOW_ONLY 2>/dev/null

# end-to-end check — a denied command (empty = bypassed or allow-only; JSON deny = active)
printf '%s' '{"permission_mode":"default","tool_name":"Bash","tool_input":{"command":"helm install foo bar"}}' \
  | ~/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/dippy-with-auto-fallback.sh

# end-to-end check — a whitelisted command (JSON allow = active or allow-only; empty = bypassed)
printf '%s' '{"permission_mode":"default","tool_name":"Bash","tool_input":{"command":"cat /etc/hosts"}}' \
  | ~/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/dippy-with-auto-fallback.sh
```

> **Note: Native settings.json capabilities** (for context, not to use): settings.json supports wildcards at any position since v2.1.0 (Jan 2026), has allow/ask/deny directives, per-project overrides via `settings.local.json`, and MCP tool permissions. **Dippy's unique advantages**: guidance messages on ask/deny rules, last-match-wins ordering (vs deny>ask>allow fixed priority), plain text config with comments, and file redirect controls (`deny-redirect`).

## Key Locations

| File | Scope | Path | Manages |
|------|-------|------|---------|
| **Global Dippy config** | All projects | `~/mackup/confs/.dippy/config` (symlinked to `~/.dippy/config`) | Bash, MCP, redirects |
| **Project Dippy config** | Single project | `.dippy` in project root | Bash, MCP overrides |
| **Global settings.json** | All projects | `~/mackup/confs/claude/settings.json` | Read, WebFetch, Skill, hooks |
| **Project settings.local.json** | Single project | `.claude/settings.local.json` | Read, WebFetch overrides |

**Dippy config precedence** (highest to lowest):
1. `$DIPPY_CONFIG` env var (if set)
2. `.dippy` in project root (searches upward like `.git`)
3. `~/.dippy/config` global

**Within a config file**: Last match wins. Broad allows first, specific denies after.

**Project `.dippy` merges with global** `~/.dippy/config`. Rules from both files are evaluated together with last match wins. A project rule can override a global rule for the same command pattern.

## Dippy Config Format

A ready-to-adapt starter config ships with this skill as [`config.example`](config.example) — generic safe-defaults (read-only allows, write/destructive `ask`/`deny`, secret-write `deny-redirect` guards, the no-interpreter rule, and the auto-mode `[ASK]` convention). Copy it to `~/.dippy/config` (or a project `.dippy`) and tailor it to your toolchain.

### Directives Reference

| Directive | Syntax | Behavior |
|-----------|--------|----------|
| `allow` | `allow <pattern>` | Auto-approve matching commands |
| `ask` | `ask <pattern> "message"` | Prompt user for approval |
| `deny` | `deny <pattern> "message"` | Block with reason message |
| `allow-redirect` | `allow-redirect <path-pattern>` | Permit file writes to path |
| `deny-redirect` | `deny-redirect <path-pattern> "message"` | Block file writes to path |
| `allow-mcp` | `allow-mcp <tool-pattern>` | Auto-approve MCP tool |
| `ask-mcp` | `ask-mcp <tool-pattern> "message"` | Prompt for MCP tool |
| `deny-mcp` | `deny-mcp <tool-pattern> "message"` | Block MCP tool |
| `after` | `after <pattern> "message"` | Post-execution feedback (needs PostToolUse hook) |
| `after-mcp` | `after-mcp <pattern> "message"` | Post-execution MCP feedback |
| `alias` | `alias <source> <target>` | Map wrapper scripts to canonical names |
| `set default` | `set default allow` / `set default ask` | Default for unknown commands |
| `set log` | `set log <path>` | Enable audit logging |
| `set log-full` | `set log-full` | Include full command text in logs |

### Pattern Matching

- **Prefix match** (default): `allow git status` matches `git status`, `git status -s`, etc.
- **Exact match**: Append `|` anchor: `allow git status|` matches only the literal command
- **Wildcards**: `*` (anything incl. spaces), `?` (one char), `[abc]` (char class)
- **Path patterns** (redirects): `**` recursive, `*` single directory level

> **GOTCHA: Patterns with glob characters (`*`, `?`, `[`) lose implicit prefix matching.** Dippy only adds a trailing ` *` for prefix matching when the pattern has NO glob characters. If your pattern contains any glob char, it must match the **entire** command string via `fnmatch`. For example, `ask tea issue* close` matches `tea issues close` but NOT `tea issues close 42`.
>
> **v0.2.7 fix**: Trailing `*` now matches empty strings (bare commands). So `ask tea issue* close *` matches both `tea issues close` and `tea issues close 42`. Always add trailing ` *` to glob patterns that should match with or without extra args.
>
> **Corollary for `curl` patterns:** Always use trailing `*` after the flag you're matching: `ask curl * -X POST *` catches both `curl url -X POST` and `curl url -X POST -d '{}'`. Without trailing `*`, the pattern only matches when POST is the last token.
>
> **GOTCHA: pattern tokens align with command tokens, so a path rule needs a `-*` slot for the flags.** This is the single biggest source of rules that look right and never fire. `ask rm /*.git/*` matches `rm /repo/.git/config` but **NOT** `rm -rf /repo/.git/config` — the `-rf` occupies a token the pattern has no slot for. Write both forms, or lead with `-*`:
>
> ```
> ask rm /*.git/*     "deletes git internals"   # rm <path>
> ask rm -* /*.git/*  "deletes git internals"   # rm -rf <path>
> ```
>
> **Corollary — a path pattern token must be absolute.** A token *without* a `/` (`*.git`, `*objects`) is a plain glob that spans the rest of the command. A token *with* a `/` is matched against one argument, and a relative one can never match an absolute argument. Inside such a token `*` does cross slashes, so it matches at any depth. Verified against v0.2.7 on 2026-08-02 via an isolated `DIPPY_CONFIG`, target `rm -rf /Users/me/repo/.git/objects`:
>
> | Pattern | Matches? | Why |
> |---|---|---|
> | `ask rm -* /*.git/*` | yes | absolute path token, `*` crosses `/` |
> | `ask rm -* /*/.git/*` | yes | same |
> | `ask rm *.git` | yes | no slash → plain glob, spans the command; only when `.git` ends it |
> | `ask rm *.git/*` | **no** | has a slash → path token, but relative |
> | `ask rm */.git/*` | **no** | same |
> | `ask rm /*.git/*` | **no** | absolute, but no `-*` slot for `-rf` |
> | `ask rm **/.git/*` | **no** | `**` is redirect-pattern syntax only |
>
> Redirects are a different matcher and unaffected: `deny-redirect **/.env*` works as documented.
>
> **Test every rule you write, in isolation:** `printf 'set default allow\nask rm <pattern> "HIT"\n' > /tmp/p.dippy` then pipe a hook payload through `DIPPY_CONFIG=/tmp/p.dippy dippy --claude`. `set default deny` is **not** valid syntax (only `allow`/`ask`) and silently yields an empty ruleset, which looks exactly like "`DIPPY_CONFIG` is ignored"; a nonexistent config path also falls back to the global file without warning.
>
> **SECURITY: Never `allow` interpreter commands.** Rules like `allow bash`, `allow python3`, `allow node` are prefix matches that auto-approve `bash -c '...'`, `python3 -c '...'`, `node -e '...'`, bypassing ALL inner command rules. Dippy does not trace into `-c`/`-e` arguments (single-layer execution). Remove these and let them fall through to `set default ask`.

### MCP Tool Naming

MCP tools follow: `mcp__<server>__<action>`. Examples:
- `allow-mcp mcp__grafana__*` - all Grafana tools
- `deny-mcp mcp__*__delete_*` - all delete actions across servers
- `allow-mcp mcp__context7__query-docs` - specific tool

### Comments

Full-line (`# comment`) and inline (`allow git  # comment`) supported.

## settings.json Format (Read/WebFetch/Skill only)

Permissions live in `permissions.allow` (array of strings):

```jsonc
{
  "permissions": {
    "allow": [
      "Read(*)",                       // allow reading any file
      "Read(~/**)",      // path-specific
      "WebSearch",                     // no pattern needed
      "WebFetch",
      "WebFetch(domain:github.com)",   // domain-scoped
      "Skill(docx)"                    // specific skill
    ]
  }
}
```

| Tool | Pattern syntax | Example |
|------|---------------|---------|
| `Read` | `Read(glob)` - file path glob | `Read(~/**)` |
| `WebFetch` | `WebFetch(domain:host)` or just `WebFetch` | `WebFetch(domain:example.com)` |
| `WebSearch` | `WebSearch` | `WebSearch` |
| `Skill` | `Skill(name)` | `Skill(docx)` |

> **Important**: Do NOT add `Bash(...)` or `mcp__*` entries to settings.json. Those are managed exclusively by Dippy.

## Workflow: Permit a Bash Command

**Critical rule: NEVER run the command, only update permissions.**

1. **Analyze** the command the user wants permitted
2. **Determine scope**: global (`~/.dippy/config`) or project (`.dippy`)?
   - Default to global if unclear
3. **Check for duplicates** before adding:
   - Read the target Dippy config
   - Check if the command is already covered by a broader rule (e.g., `allow git` already covers `git status`)
   - Check if a more specific rule already exists that would conflict
   - If duplicate found, inform user and skip
4. **Decompose** chained commands: pipes create separate commands, each needs its own rule
5. **Apply safety defaults** (see Safety section below)
6. **Add** the rule in the correct section of the config (organized by category)
7. **Show** the user exactly what was added and where
8. **Ask** if they want to test the newly permitted command

### Duplicate-Checking Logic

Before adding any rule:
1. **Global covers it?** If `allow git` is in global config, don't add `allow git status` anywhere
2. **Project already has it?** Don't add a rule that already exists in the project `.dippy`
3. **Broader project rule?** If project has `allow kubectl`, don't add `allow kubectl get`
4. **Would a project rule shadow a global deny?** If global has `deny git push --force`, adding `allow git push --force` to project `.dippy` would override it. Warn the user.

## Workflow: Permit an MCP Tool

1. **Identify** the tool name: `mcp__<server>__<action>`
2. **Determine scope**: global or project
3. **Check for duplicates** (same logic as Bash commands, using `allow-mcp`/`deny-mcp`)
4. **Add** the `allow-mcp` rule
5. **Verify** the `mcp__.*` PreToolUse hook matcher exists in settings.json (required for Dippy to intercept MCP calls)

## Workflow: Permit Read/WebFetch/Skill

These still use settings.json since Dippy doesn't handle them:

1. **Determine scope**: global (`~/mackup/confs/claude/settings.json`) or project (`.claude/settings.local.json`)
2. **Read** the target settings file
3. **Add** the permission entry to `permissions.allow`
4. **Validate JSON**:
   ```bash
   jq . <settings-file> > /dev/null && echo "JSON valid"
   ```
5. **Show** the user what was added

## Safety: Read-Only Defaults

By default, only permit read operations. For commands with write effects, ask the user to confirm.

### Auto-permit (read-only, safe to `allow`)

| Category | Commands |
|----------|----------|
| Kubernetes | `kubectl get/describe/logs/explain/events/top/diff/cluster-info/api-resources` |
| Git | `git status/log/diff/fetch/remote/branch` (listing) |
| Helm | `helm template/show/list/get/search/history/pull` |
| Flux | `flux get/check/diff/logs/build` |
| Docker | `docker ps/images/logs/inspect/info` |
| Infra | `tofu plan/show/state` (via infisical wrapper) |
| CLI tools | `ls`, `cat`, `jq`, `grep`, `rg`, `fd`, `tree`, `eza`, `curl -s -X GET` |

> **Never auto-permit interpreters**: `bash`, `python3`, `node`, `sh`, `ruby`, `perl`. Prefix-matching these auto-approves `-c`/`-e` flags, bypassing all inner command rules. Let them fall through to `set default ask`.

### Require confirmation (write/destructive, use `deny` or `ask`)

- `kubectl apply/delete/edit/patch/rollout restart/scale/drain`
- `git push/commit/rebase/reset --hard/clean -f`
- `helm install/upgrade/uninstall/rollback` (for GitOps workflows, prefer `deny` with guidance: `deny helm install "Use GitOps: create/edit the HelmRelease in your GitOps repo instead"`)
- `flux suspend/resume/delete`
- `docker rm/rmi/system prune`
- `tofu apply/destroy`
- `rm -rf`, `rm -r`, `sudo`, `chown`
- `curl` with POST/PUT/PATCH/DELETE methods, `-d`/`--data`, `-F`/`--form`, `-T`/`--upload-file`
- **Important**: Use `*` wildcard to catch write flags regardless of position, with trailing `*` to match extra args: `ask curl * -X POST *` (not `ask curl -X POST`). The URL often comes before the method flag, and additional flags may follow.

When a command has write effects, present options:
> "This command can modify state. Recommended:
> 1. Add as `ask` rule (prompts before execution, safer)
> 2. Add as `allow` rule (auto-approves, needs your confirmation)
> 3. Add as `deny` with guidance message (blocks with reason)"

## Workflow: Review Permissions

```bash
# Show global Dippy config
cat ~/.dippy/config

# Show project Dippy config (if exists)
cat .dippy 2>/dev/null || echo "No project .dippy"

# Show settings.json permissions (Read/WebFetch/Skill)
jq '.permissions.allow' ~/mackup/confs/claude/settings.json

# Show project settings.local.json
jq '.permissions.allow' .claude/settings.local.json 2>/dev/null || echo "No project settings"

# List all project .dippy files across repos
fd -H -t f '^\\.dippy$' ~/stuff/gitrepos/wxs/
```

## Workflow: Clean Up Stale Permissions

1. Read the Dippy config (global and/or project)
2. Identify rules for tools/commands no longer used
3. **Ask user** before removing any entries
4. Remove confirmed entries
5. For settings.json changes, validate JSON after editing

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Bash command prompts unexpectedly | Missing `allow` rule in Dippy config | Add rule to `~/.dippy/config` or `.dippy` |
| Command denied with message | Matched a `deny` rule | Check rule ordering (last match wins) |
| MCP tool prompts unexpectedly | Missing `allow-mcp` rule | Add `allow-mcp` rule to Dippy config |
| MCP rules ignored entirely | Missing `mcp__.*` hook matcher | Add PreToolUse matcher in settings.json |
| Permission added but still denied | Rule ordering: a later `deny` overrides your `allow` | Move your `allow` after the `deny`, or make the `deny` more specific |
| Works in one project, not another | Project `.dippy` rule may override a global rule (last match wins) | Check both global and project configs for conflicting rules |
| Read/WebFetch denied | Missing entry in settings.json (not Dippy) | Add to `permissions.allow` in settings.json |
| JSON parse error after settings edit | Malformed JSON | Run `jq .` to find syntax errors |
| Afterthought not firing | Missing PostToolUse hook | Add PostToolUse Bash matcher in settings.json |
| No rules fire at all: `deny` ignored, guidance messages gone | Kill-switch left on — `~/.dippy/OFF` exists, wrapper exits before running dippy | `dippy-toggle.sh on` (or `rm ~/.dippy/OFF`; see [Three states](#three-states-active--allow-only--disabled)); takes effect immediately, no restart |
| `allow` rules fire but `deny` and `[ASK]` do not, and afterthoughts still work | ALLOW-ONLY state left on — `~/.dippy/ALLOW_ONLY` exists, wrapper forwards only `allow` | `dippy-toggle.sh on` restores full guardrails; `dippy-toggle.sh status` reports the state |
| `dippy-toggle.sh allow-only` reports ALLOW-ONLY but `deny` rules still block | `settings.json` points at a wrapper clone predating the `ALLOW_ONLY` branch (added 2026-08-04) | Update your clone of `dippy-with-auto-fallback.sh`; `dippy-toggle.sh status` warns about this automatically |
| Afterthought not firing **in auto mode only** | Wrapper predating the `hook_event_name` check: its auto-mode filter forwards only `allow`/`deny`/`[ASK]`-tagged `ask`, and dippy emits afterthoughts as **plain text** with no `permissionDecision`, so they were dropped. Fixed 2026-07-26 — the wrapper now pipes any non-`PreToolUse` event straight to `dippy`, so pointing PostToolUse at either the wrapper or bare `dippy` works | Update your clone of `dippy-with-auto-fallback.sh` |
| Rule with glob chars doesn't match commands with extra args | Patterns with `*`/`?`/`[` lose implicit trailing ` *` prefix matching; must match entire command | Add explicit trailing ` *` to your glob pattern (v0.2.7+ matches bare commands too) |
| `bash -c` / `python3 -c` bypasses rules | `allow bash` prefix-matches all `bash -c '...'` commands; Dippy doesn't trace into `-c` args | Remove `allow bash`/`allow python3`/`allow node`; let them fall to `set default ask` |

## Hook Configuration Reference

The following hooks must exist in `~/mackup/confs/claude/settings.json` for Dippy to function. The PreToolUse `Bash` + `mcp__.*` matchers invoke the **auto-fallback wrapper** (`dippy-with-auto-fallback.sh`, bundled with this skill), not bare `dippy`, so that auto-mode `ask` handling works as described in [Auto-mode fallback wrapper](#auto-mode-fallback-wrapper). The PostToolUse hook (afterthoughts) calls `dippy` directly.

> **Hook command paths must be absolute.** Claude Code does NOT expand `~` in hook `command` strings — the `~/...` paths shown below are for readability; in your actual `settings.json` use the fully expanded path (e.g. `/home/you/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/dippy-with-auto-fallback.sh`). Point the hook at your local clone of this repo, not at the installed `~/.claude/skills/claude-permissions/` copy (that directory is overwritten on every `npx skills update`).

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "~/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/dippy-with-auto-fallback.sh" }
        ]
      },
      {
        "matcher": "mcp__.*",
        "hooks": [
          { "type": "command", "command": "~/stuff/gitrepos/gh/vtmocanu/skills/claude-permissions/dippy-with-auto-fallback.sh" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          { "type": "command", "command": "dippy" }
        ]
      }
    ]
  }
}
```

The wrapper ships with this skill as `./dippy-with-auto-fallback.sh`. **Read that file directly** when you need its exact logic — it is the source of truth for the auto-mode behavior summarized in [Auto-mode fallback wrapper](#auto-mode-fallback-wrapper) above. To use it, clone this repo locally and point both PreToolUse hook commands at your clone's copy (absolute path, per the note above).

> **Note**: The `mcp__.*` matcher uses regex (not glob). Other PreToolUse hooks (zellaude, clawd, dot-agent-deck) run alongside the wrapper but are unrelated to permissions.
