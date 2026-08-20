---
name: token-audit
description: Audits a Claude Code setup for token waste and reports only, changing no file or setting. Measures each cost driver with shell and file tools instead of estimating, covering CLAUDE.md sizes and @imports, MCP server and tool counts, whether tool deferral is active, a proxy that silently disables it, model and effort and mid-session switches, output-reducing hooks, per-agent model inheritance, cron and loop intervals versus the measured cache TTL, and the newest session log's token split. Emits one severity-ranked table plus the single highest-leverage fix. Use when the user asks to audit token or context waste, cut token cost, explain a low cache-hit rate, or diagnose context bloat. Triggers include "token waste", "token audit", "context bloat", "cache hit rate", "why is my context so big", "cut token cost".
---

# Token Waste Audit

## Document location

Source of truth: `~/stuff/gitrepos/gh/vtmocanu/skills/skills/token-audit/SKILL.md` (public repo: github.com/vtmocanu/skills). The installed copy at `~/.claude/skills/token-audit/` is derived by `npx skills`; edit the source, then `npx skills update`. Never edit the installed copy.

This skill has bundled scripts. Invoke them from the skill's own base directory (the `Base directory for this skill:` line printed when the skill loads), e.g. `<skill dir>/scripts/cache-report.sh`. On a normal global install that path is `~/.claude/skills/token-audit/scripts/`.

## Purpose and hard rules

Diagnose what is spending tokens in this setup. **Report only. Change no file and no setting** — no edits, no config writes, no fixes, even if one is obvious.

Non-negotiable rules, from the audit contract:

- **Measure, do not estimate.** Run a command or read a file for every number. Exact token counts from the session log and from `/context` are authoritative — the API logged them. Byte counts are exact; when converting bytes to tokens, label it approximate and prefer `/context`.
- **Write `UNKNOWN`, never a guess.** If a value cannot be measured (no permission, tool unavailable, data absent), the cell is `UNKNOWN` with the reason — not an adjective, not a plausible number.
- **Evidence is a number or a file path, never an adjective.** "17253 bytes" or `~/.claude/settings.json`, not "large" or "several".

## Slash commands you cannot run yourself

`/context` and `/usage` are UI commands; a tool call cannot invoke them. They carry authoritative live numbers (real per-category context tokens, plan/usage limits). **Ask the user to run `/context` and `/usage` and paste the output**, then continue. Do not block the whole audit waiting — run every shell measurement first, fold the pasted numbers in when they arrive, and mark anything that depended on them `UNKNOWN (awaiting /context)` if they never come.

## The seven measurements

Run the independent ones in parallel. Each produces evidence for the final table.

### 1. MEMORY — launch-loaded CLAUDE.md, imports, and rules

Run the bundled scanner from the directory you are auditing:

```
<skill dir>/scripts/memory-scan.py
```

It enumerates everything Claude Code loads at launch, matching CC's real rules: managed/enterprise CLAUDE.md if present, user memory (`~/.claude/CLAUDE.md`), the project chain (`CLAUDE.md` + `CLAUDE.local.md` up the ancestor tree, plus `.claude/CLAUDE.md` at the cwd), and unconditional `*.md` under `.claude/rules/` and `~/.claude/rules/`. It follows `@import` lines (bare `@README`, `@dir/file.md`, `~/…`, absolute) recursively, capped at CC's real 4-hop limit, skipping code spans/fenced blocks so an example `@path` is not counted. `paths:`-scoped rules load on demand, so they are listed but kept out of the launch total. It prints exact bytes/lines and an approx-token count (bytes/4) per file. **Flag any single file over 5k tokens and any launch total over 10k tokens.** Reconcile the approximation against the `/context` "Memory files" figure once pasted; if they disagree, trust `/context`.

### 2. TOOLS — MCP servers, tool counts, deferral, and any proxy

- **Configured MCP servers**: parse each config source; a server can be defined in any of them:
  ```
  jq -r '.mcpServers | keys[]?' ~/.claude.json 2>/dev/null
  jq -r '.mcpServers | keys[]?' ./.mcp.json ~/.claude/settings.json .claude/settings.json .claude/settings.local.json 2>/dev/null
  ```
  Session-connected servers (claude.ai connectors: Gmail, Slack, IBKR, etc.) are not in these files. Count them from the MCP tool names visible in your own tool list (`mcp__<server>__<tool>`), grouped by `<server>`. Report tools-per-server as a count.
- **Tool deferral ACTIVE or NOT — state it plainly.** Deferral is active when MCP/other tools are presented deferred and reached through `ToolSearch` (a system reminder lists deferred tool names and their schemas are not preloaded). Not active when every tool schema is loaded up front. Decide from your own tool surface this session and say `ACTIVE` or `NOT ACTIVE`.
- **Proxy / gateway — say so loudly if found.** Routing through a proxy silently turns deferral off and nothing warns you. Check env and settings:
  ```
  env | grep -iE 'ANTHROPIC_BASE_URL|ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_BEDROCK|ANTHROPIC_VERTEX|_PROXY|GATEWAY'
  jq -r '.env // {} | to_entries[] | "\(.key)=\(.value)"' ~/.claude/settings.json .claude/settings.json .claude/settings.local.json 2>/dev/null
  ```
  Any `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` (or a Bedrock/Vertex/gateway var) = a proxy is in play → RED, because deferral is likely off regardless of what step 2 observed.

### 3. MODEL — current model, effort, and mid-session switches

- Report the model and effort **and where each is set**. Sources, in override order: `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` env, `~/.claude/settings.json` `model`, project `.claude/settings.json`/`.local.json` `model`, and any `/model` runtime choice (the live model is in your environment block). Effort likewise from settings/env.
  ```
  jq -r '{model, env}' ~/.claude/settings.json .claude/settings.json .claude/settings.local.json 2>/dev/null
  env | grep -iE 'ANTHROPIC_MODEL|ANTHROPIC_SMALL_FAST_MODEL|MAX_THINKING|REASONING'
  ```
- **Flag any mode that changes model automatically mid-session** — a plan-model alias like `opusplan` (Opus to plan, a smaller model to execute), or any harness setting that swaps models. Every switch invalidates and rebuilds the whole prompt cache. If the model is a fixed id and no auto-switch alias is set, say so.

### 4. HOOKS — output-reducing PreToolUse hooks

List PreToolUse hooks and judge whether any rewrite noisy commands to cut output (append `--quiet`/`-q`, `--reporter=dot`, `2>/dev/null`, `| tail`, pytest `-q`, etc.):

```
jq -r '.hooks.PreToolUse[]? | .matcher as $m | .hooks[]? | "\($m)\t\(.command // .type)"' ~/.claude/settings.json .claude/settings.json .claude/settings.local.json 2>/dev/null
```

**If there are none, say so explicitly** — unfiltered test/build output lands in context verbatim and is re-sent for the rest of the session. Absence is itself the finding.

### 5. SUBAGENTS — explicit model vs inherited

List every agent definition and report, per agent, whether frontmatter pins a `model:` or inherits the main session's model:

```
for f in ~/.claude/agents/*.md .claude/agents/*.md; do
  [ -f "$f" ] || continue
  m=$(sed -n '/^---$/,/^---$/{s/^model:[[:space:]]*//p;}' "$f" | head -1)
  printf '%s\t%s\n' "$f" "${m:-INHERITS}"
done
```

An agent with no `model:` inherits the session model (so an Opus session runs that subagent on Opus). Report the split; a fleet of inheriting agents on an expensive session model is the cost to name.

### 6. SCHEDULED WORK — intervals vs the measured cache TTL

Enumerate every recurring job and its interval, then compare each interval against the **measured** prompt-cache TTL (step 7 reports it — `5m` or `1h`; do not assume). **Flag every interval longer than the TTL** — those miss cache on every fire.

- Claude Code scheduled agents / routines (cron-scheduled): use the `CronList` tool (load via `ToolSearch("select:CronList")`) if present, else check the schedule config the `schedule`/`loop` skills manage.
- System schedulers:
  ```
  crontab -l 2>/dev/null
  ls ~/Library/LaunchAgents /Library/LaunchAgents /Library/LaunchDaemons 2>/dev/null   # macOS launchd
  ```
- Any `/loop` job running this session and its interval.

For each: interval, TTL, and `MISS` if interval > TTL.

### 7. CACHE — token split of the newest session log

Run the bundled reporter (defaults to the newest log for the current project):

```
<skill dir>/scripts/cache-report.sh          # or: cache-report.sh /path/to/session.jsonl
<skill dir>/scripts/cache-report.sh --list    # list this project's logs, newest first
```

It sums, across every assistant turn, `cache_read_input_tokens`, `cache_creation_input_tokens`, `input_tokens`, `output_tokens`, reports each as a percentage of the grand total, and gives the context size (input side) on the first and last turn plus the cache TTL actually in use. These are the API's own logged counts — exact, not estimated. Feed the TTL into step 6. The token sums include subagent (sidechain) turns as total session spend; the first/last context sizes exclude them (`subagent_turns` and `context_basis` in the output say which), so the context figures reflect the main thread, not a subagent.

Read the split: healthy sessions are dominated by `cache_read` (a high read share means the cache is working). A large `cache_creation` share relative to `cache_read` means the cache is being rebuilt repeatedly — churn worth explaining (model switch, expired TTL, edited early context). Report the numbers; name the driver only if another measurement supports it.

## Output

Emit exactly one table, sorted by cost, highest first:

```
FINDING | SEVERITY | EVIDENCE | WHAT IT IS COSTING ME
```

- **SEVERITY** is `RED`, `AMBER`, or `GREEN`.
- **EVIDENCE** is a number or a file path — never an adjective.
- **WHAT IT IS COSTING ME** is the concrete token/latency cost, tied to the evidence.
- Anything unmeasurable is a row with `UNKNOWN` in EVIDENCE and the reason.

Then one final line, and nothing else after it: the single highest-leverage change to make. One line.

Do not append a summary, next steps, or an offer to fix. The table and the one line are the whole deliverable.

## Traps

- **Estimating memory tokens as if exact.** bytes/4 is an approximation; label it and defer to `/context`. Never present the divided number as a measured token count.
- **Counting only configured MCP servers.** `~/.claude.json` misses claude.ai connectors surfaced only in the live tool list — count both, or the tool total is wrong.
- **Assuming the cache TTL.** It is `5m` or `1h` depending on the account/session; the session log records which (`ephemeral_1h_input_tokens` vs `ephemeral_5m_input_tokens`). Measure it in step 7 before judging step 6 intervals.
- **A proxy masks deferral state.** If `ANTHROPIC_BASE_URL`/`ANTHROPIC_AUTH_TOKEN` is set, report deferral as compromised regardless of what the live tool surface looks like.
- **Running the audit is not fixing it.** Change nothing. If the user wants a fix afterward, that is a separate, explicit request.
