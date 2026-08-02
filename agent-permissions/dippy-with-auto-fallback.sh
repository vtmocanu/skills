#!/usr/bin/env bash
# In auto mode: run dippy and forward only explicit allow/deny decisions
# (whitelist + blocklist), letting auto-mode's classifier handle anything
# dippy would ask on. In other modes: always hand stdin to dippy.
set -u

# Kill-switch: `touch ~/.dippy/OFF` bypasses dippy entirely (no allow/deny/ask
# decision is emitted, so the host's own permission handling takes over).
# `rm ~/.dippy/OFF` re-enables. Checked per invocation, no restart needed.
[ -f "${HOME}/.dippy/OFF" ] && exit 0

payload=$(cat)
mode=$(printf '%s' "$payload" | jq -r '.permission_mode // "default"' 2>/dev/null)
event=$(printf '%s' "$payload" | jq -r '.hook_event_name // "PreToolUse"' 2>/dev/null)

# The auto-mode filter below only makes sense for PreToolUse permission
# verdicts. Everything else (PostToolUse afterthoughts, which dippy emits as
# plain text, not JSON) must pass through untouched, or the filter swallows it.
if [ "$event" != "PreToolUse" ]; then
  printf '%s' "$payload" | dippy
  exit 0
fi

if [ "$mode" = "auto" ]; then
  output=$(printf '%s' "$payload" | dippy)
  decision=$(printf '%s' "$output" | jq -r '.hookSpecificOutput.permissionDecision // ""' 2>/dev/null)
  reason=$(printf '%s' "$output" | jq -r '.hookSpecificOutput.permissionDecisionReason // ""' 2>/dev/null)
  case "$decision" in
    allow|deny) printf '%s' "$output" ;;
    ask)
      case "$reason" in
        *"[ASK]"*) printf '%s' "$output" ;;
        *)         : ;;
      esac
      ;;
    *) : ;;
  esac
  exit 0
fi

printf '%s' "$payload" | dippy
