---
name: clodex
description: Runbook for clodex (@bman654/clodex), which bridges Claude Code to OpenAI ChatGPT/Codex-plan and other OpenAI-compatible models and patches the Claude Code binary so those models appear in /model and work as subagents. Covers install, version checks and upgrades, provider sign-in, adding or removing a model (refresh catalog, favorite, alias, context window, patch), re-patching after a Claude Code or clodex update, restore and uninstall, and coexistence with tweakcc. Use when adding a new model to Claude Code via clodex, upgrading clodex, or re-patching after a Claude Code update. Invoke explicitly with /clodex, optionally followed by a model id or a task such as "upgrade".
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

## What to do on invocation

- **A model id** (for example `gpt-6.1-sol`): run [Add a model](#add-a-model).
- **`upgrade`, `update`, `check`**: run [Install, check, upgrade](#install-check-upgrade).
- **`remove <model>`**: run [Remove a model](#remove-a-model).
- **Nothing**: report status (below), then ask what to do.

Status:

```bash
clodex --version
clodex providers list
clodex models --list          # favorites and aliases only, not the catalog
jq '{claudeVersion, patchedAt}' ~/.clodex/patch-state.json
claude --version              # differs from claudeVersion -> re-patch needed
```

## Install, check, upgrade

Requires Node 22 or newer. There is no self-update or update-check command;
npm is the channel.

```bash
command -v clodex && clodex --version            # installed version
npm view @bman654/clodex version                 # latest published
npm install -g @bman654/clodex@latest            # install or upgrade
```

- A global npm install changes the user's machine. Ask before running it,
  unless the user's request already asked for the install or upgrade.
- After any install or upgrade, run `clodex patch` (see [Patch](#patch)): the
  new version may patch different sites.
- Before upgrading across a major version, read the release notes at
  `https://github.com/bman654/clodex/releases`.

First-time provider sign-in (ChatGPT/Codex plan):

```bash
clodex providers auth openai             # device code; --browser if device code is disabled
clodex providers list                    # confirm "auth: ... (OAuth)"
```

Use `clodex providers add` for an API key, OpenCode Go, or a custom
OpenAI-compatible server.

## Add a model

Model ids are provider model ids such as `gpt-6.1-sol`; the provider for the
ChatGPT plan is `openai-oauth`. Substitute the requested id for `MODEL` and the
provider for `PROVIDER` below.

1. **Refresh the catalog.** `clodex models` reads a cached catalog, and
   upgrading clodex does not refresh it, so a new model is missing until this
   runs:

   ```bash
   clodex providers refresh-models PROVIDER
   jq --arg m MODEL '.. | objects | select(.id? == $m)' ~/.clodex/providers.json
   ```

   Empty output means the provider does not offer that model to this account.
   Report that and stop.

2. **Add it as a favorite** (max 20). The supported path is interactive:
   the user runs `clodex models`, searches the id, and adds it. Suggest
   `! clodex models` so it runs in this session. Without a TTY, back up
   `~/.clodex/config.json` and append `{"providerId": "PROVIDER", "modelId":
   "MODEL"}` to its `favoriteModels` array with `jq`. This file format is
   clodex-internal. Confirm with step 5 that clodex accepted the edit.

3. **Alias** it so Claude Code and agent frontmatter can use the bare id:

   ```bash
   clodex models --alias MODEL=clodex:PROVIDER:MODEL
   ```

4. **Context window.** Stops are `standard` (provider default), `max`
   (ceiling), `default` (clear), or a token count such as `500k`. Read the
   model's `pricingBoundary` from `clodex models --json`: above it the provider
   may bill the whole request at a higher rate (on the ChatGPT plan, likely
   faster quota use). `standard` stays under it; `max` crosses it. Match the
   stop already used by the user's other favorites unless they say otherwise.

   ```bash
   clodex models --context MODEL=max --save
   ```

5. **Verify, then patch:**

   ```bash
   clodex models --json | jq --arg m MODEL '.[] | select(.modelId == $m) | {id, alias, context}'
   clodex patch
   ```

6. Tell the user to **restart Claude Code**; the running process keeps the
   old binary. The model is then in `/model` and usable as `model: MODEL` in
   the Agent tool and in skill or agent frontmatter.

## Remove a model

Remove the favorite interactively (`clodex models`), drop its alias with
`clodex models --unalias NAME`, clear a saved context stop with
`clodex models --context MODEL=default --save`, then `clodex patch` and
restart Claude Code.

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
  build yet: check for a clodex upgrade before anything else.
- **Editor extension mismatch.** When an editor's Claude Code extension is a
  different build than the patched CLI, `clodex patch` warns and prints
  alignment commands (for example `claude install <version>`). Relay them to
  the user; running them changes the installed Claude Code version, so ask
  first. Updating the extension instead is usually the better fix.

## Launch modes

```bash
clodex claude            # launch Claude Code through clodex (default: proxy mode)
clodex claude --endpoint # local Anthropic-format gateway via ANTHROPIC_BASE_URL
```

- **Proxy** (default): selective intercept of `api.anthropic.com`; Claude Code
  keeps its own Anthropic login, and only `clodex:` models are rerouted.
- **Endpoint**: all traffic goes through the local gateway.
- `--save-mode` persists the mode; `--trace` logs to `~/.clodex/logs/`.

## tweakcc coexistence

[tweakcc](https://github.com/Piebald-AI/tweakcc) also patches the Claude Code
binary in place. Each tool restores its own pristine backup before patching, and
neither knows about the other.

- Order: `clodex patch` first, then `tweakcc --apply`. tweakcc then snapshots
  the clodex-patched binary as its baseline. The reverse order wipes clodex.
- After every Claude Code update, re-run both in that order and confirm both
  patch sets survived.
- Let only clodex own the context window; do not also set tweakcc's context
  limit.

## Uninstall

```bash
clodex patch --restore
npm uninstall -g @bman654/clodex
```

Credentials stay in the OS credential store and config in `~/.clodex/` until
removed; ask before deleting either.
