---
name: clodex
description: Runbook for clodex (@bman654/clodex), which bridges Claude Code to OpenAI ChatGPT/Codex-plan and other OpenAI-compatible models and patches the Claude Code binary so those models appear in /model and work as subagents. Covers install, version checks and upgrades, provider sign-in, adding or removing a model (refresh catalog, favorite, alias, context window, patch), re-patching after a Claude Code or clodex update, restore and uninstall. Use when adding a new model to Claude Code via clodex, upgrading clodex, or re-patching after a Claude Code update. Invoke explicitly (/clodex in Claude Code, $clodex in Codex), optionally followed by a model id or a task such as "upgrade".
disable-model-invocation: true
---

# clodex

[clodex](https://github.com/bman654/clodex) (`@bman654/clodex` on npm) bridges
Claude Code to non-Anthropic models: the ChatGPT/Codex-plan OAuth session
(`openai-oauth`), an OpenAI API key (`openai`), OpenCode Go, or any
OpenAI-compatible server (`custom-<name>`). `clodex patch` edits the installed
Claude Code binary so clodex favorites pass model validation, appear in
`/model`, report their real context window, and work as Agent-tool subagents
and in skill or agent `model:` frontmatter.

Invocation: `/clodex [ARGS]` in Claude Code, `$clodex [ARGS]` in Codex.

## What to do on invocation

- **A model id** (for example `gpt-6.1-sol`): run [Add a model](#add-a-model).
- **`check`**: run [Check](#check) and stop.
- **`install`, `upgrade`, `update`**: run [Install or upgrade](#install-or-upgrade).
- **`remove MODEL`**: run [Remove a model](#remove-a-model).
- **Nothing**: run [Status](#status), then ask what to do.

## Status

Read-only.

```bash
clodex --version
clodex providers list
clodex models --list          # favorites and aliases only, not the catalog
jq '{claudeVersion, patchedAt}' ~/.clodex/patch-state.json
claude --version              # differs from claudeVersion -> re-patch needed
```

## Check

Read-only. Report the installed and latest versions, then stop.

```bash
command -v clodex && clodex --version    # no output -> report "not installed"
npm view @bman654/clodex version         # latest published
```

## Install or upgrade

Requires Node 22 or newer. clodex has no self-update; npm is the channel.

1. Run [Check](#check). Before crossing a major version, read
   `https://github.com/bman654/clodex/releases`.
2. A global npm install changes the user's machine. Ask first, unless the
   user's request already asked for the install or upgrade.

   ```bash
   npm install -g @bman654/clodex@latest
   ```

3. Run `clodex patch` (see [Patch](#patch)); a new version may patch
   different sites. Then restart Claude Code.

First-time provider sign-in (ChatGPT/Codex plan):

```bash
clodex providers auth openai             # device code; --browser if device code is disabled
clodex providers list                    # confirm "auth: ... (OAuth)"
```

Use `clodex providers add` for an API key, OpenCode Go, or a custom
OpenAI-compatible server.

## Interactive commands

`clodex models` (favorites manager) and `clodex providers auth` need a TTY.
In Claude Code, suggest `! clodex models` so it runs in this session. In Codex,
or wherever tool execution lacks a TTY, ask the user to run it in their own
terminal and say when done.

## Add a model

Substitute the requested model id for `MODEL`, its provider id for `PROVIDER`
(`openai-oauth` for the ChatGPT plan; `clodex providers list` shows ids), and
the chosen context stop for `STOP`.

1. **Refresh the catalog.** `clodex models` reads a cached catalog, and
   upgrading clodex does not refresh it, so a new model is missing until this
   runs. Read the refresh output: on a discovery failure clodex keeps the old
   cache or a seed list, which is not proof of what the account offers.

   ```bash
   clodex providers refresh-models PROVIDER
   jq --arg p PROVIDER --arg m MODEL \
     '.providers[] | select(.id == $p) | {fetchedAt: .modelsCache.fetchedAt,
      found: any(.modelsCache.models[]?; .id == $m)}' ~/.clodex/providers.json
   ```

   `providers.json` is clodex-internal. `found: false` after a successful
   refresh means the provider's catalog does not list `MODEL` for this
   account; after a failed refresh, report the refresh error instead. Stop
   either way.

2. **Add it as a favorite** (max 20). Supported path: the user runs
   `clodex models`, searches the id, and adds it (see
   [Interactive commands](#interactive-commands)).

   Fallback when no one can run it interactively: edit the clodex-internal
   `~/.clodex/config.json`. The edit validates the schema, skips a duplicate,
   enforces the 20 limit, keeps unrelated fields, and replaces the file only
   on success:

   ```bash
   cp ~/.clodex/config.json ~/.clodex/config.json.bak
   jq --arg p PROVIDER --arg m MODEL '
     if (.favoriteModels | type) != "array" then error("unexpected config schema")
     elif any(.favoriteModels[]; .providerId == $p and .modelId == $m) then .
     elif (.favoriteModels | length) >= 20 then error("already 20 favorites")
     else .favoriteModels += [{providerId: $p, modelId: $m}] end
   ' ~/.clodex/config.json > ~/.clodex/config.json.tmp \
     && mv ~/.clodex/config.json.tmp ~/.clodex/config.json
   ```

   On a `jq` error, delete `config.json.tmp`, report it, and stop.

3. **Alias** it so Claude Code and agent frontmatter can use the bare id:

   ```bash
   clodex models --alias MODEL=clodex:PROVIDER:MODEL
   ```

4. **Context window.** Stops: `standard` (provider default), `max`
   (ceiling), `default` (clear a saved stop), or a token count such as
   `250k`. Read the resolved metadata first:

   ```bash
   clodex models --json | jq --arg p PROVIDER --arg m MODEL \
     '.[] | select(.providerId == $p and .modelId == $m) | {context, pricingBoundary}'
   ```

   When `pricingBoundary` is set, a window above it lets the provider bill
   the whole request at a higher rate; `clodex models --context` warns when
   the chosen stop crosses it. Whether `standard` or `max` crosses it depends
   on the model and provider (API-key models may cross it at `standard`).
   Default to the stop the user's other favorites use; tell the user when it
   crosses the boundary.

   ```bash
   clodex models --context MODEL=STOP --save
   ```

5. **Verify** (exits non-zero unless every value matches), then patch:

   ```bash
   clodex models --json | jq -e --arg p PROVIDER --arg m MODEL --arg s STOP \
     'any(.[]; .providerId == $p and .modelId == $m and .alias == $m and .context.stop == $s)'
   clodex patch
   ```

6. Tell the user to **restart Claude Code**; the running process keeps the
   old binary. The model is then in `/model` and usable as `model: MODEL` in
   the Agent tool and in skill or agent frontmatter.

## Remove a model

Clear the context stop **before** dropping the alias (a bare name resolves
through the alias):

```bash
clodex models --context MODEL=default --save
clodex models --unalias MODEL
```

Then remove the favorite interactively (`clodex models`), run `clodex patch`,
and restart Claude Code.

## Patch

```bash
clodex patch            # idempotent; no-op until config or Claude Code version changes
clodex patch --trace    # per-site OK/SKIP/FAIL
clodex patch --restore  # put back the pristine Claude Code binary
```

- Re-run `clodex patch` after **every** Claude Code update, every clodex
  upgrade, and every favorite, alias, or context change. Then restart Claude
  Code.
- Success line: `clodex patch: N applied, 0 skipped, 0 failed`. A `FAIL` or
  `SKIP` usually means this clodex does not support the installed Claude Code
  build yet: run [Check](#check) for a newer clodex first.
- **Editor extension mismatch.** When an editor's Claude Code extension is a
  different build than the patched CLI, `clodex patch` warns and prints
  alignment commands (for example `claude install <version>`). Relay them to
  the user; running them changes the installed Claude Code version, so ask
  first. Updating the extension instead is usually the better fix.
- **Other binary patchers** (for example tweakcc) restore their own backup
  before patching. clodex writes its pristine backup into tweakcc's backup
  path, so a later `tweakcc --apply` restores pristine and drops the clodex
  patches, and a later `clodex patch` drops tweakcc's. Running both is not a
  supported combination. For extra edits on top of clodex, use clodex local
  patches (`clodex patch --enable-local-patches`, `~/.clodex/local-patches.mjs`,
  trusted JavaScript run with full user permissions; see the clodex README).

## Launch modes

```bash
clodex claude            # launch Claude Code through clodex (default: proxy mode)
clodex claude --endpoint # local Anthropic-format gateway via ANTHROPIC_BASE_URL
```

- **Proxy** (default): selective intercept of `api.anthropic.com`; Claude Code
  keeps its own Anthropic login, and only `clodex:` models are rerouted.
- **Endpoint**: all traffic goes through the local gateway.
- `--save-mode` persists the mode; `--trace` logs to `~/.clodex/logs/`.

## Uninstall

```bash
clodex patch --restore
npm uninstall -g @bman654/clodex
```

Credentials stay in the OS credential store and config in `~/.clodex/` until
removed; ask before deleting either.
