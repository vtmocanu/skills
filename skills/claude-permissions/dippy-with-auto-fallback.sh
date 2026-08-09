#!/usr/bin/env bash
# In auto mode: run dippy and forward only explicit allow/deny decisions
# (whitelist + blocklist), letting auto-mode's classifier handle anything
# dippy would ask on. In other modes: always hand stdin to dippy.
#
# Three states, selected by marker files under ~/.dippy (see dippy-toggle.sh):
#   (no marker)   full dippy, minus the auto-mode ask filter described above
#   ALLOW_ONLY    whitelist-only: forward `allow` and nothing else
#   OFF           dippy bypassed entirely
# OFF wins if both markers exist.
set -u

# Kill-switch: `touch ~/.dippy/OFF` bypasses dippy entirely (no allow/deny/ask
# decision is emitted, so the host's own permission handling takes over).
# `rm ~/.dippy/OFF` re-enables. Checked per invocation, no restart needed.
[ -f "${HOME}/.dippy/OFF" ] && exit 0

payload=$(cat)
mode=$(printf '%s' "$payload" | jq -r '.permission_mode // "default"' 2>/dev/null)
event=$(printf '%s' "$payload" | jq -r '.hook_event_name // "PreToolUse"' 2>/dev/null)

# The filters below only make sense for PreToolUse permission verdicts.
# Everything else (PostToolUse afterthoughts, which dippy emits as plain text,
# not JSON) must pass through untouched, or the filter swallows it. Afterthought
# guidance is not a permission decision, so ALLOW_ONLY leaves it alone too.
if [ "$event" != "PreToolUse" ]; then
  printf '%s' "$payload" | dippy
  exit 0
fi

# Whitelist-only: `touch ~/.dippy/ALLOW_ONLY`. Forward dippy's `allow` verdicts
# so whitelisted commands (cat, echo, kubectl get, ...) short-circuit the host's
# permission flow and never reach the auto classifier. Emit nothing for
# everything else -- deny and ask included -- so every non-whitelisted command
# is decided by the classifier (auto mode) or the normal prompt (other modes).
# This is deliberately a noise filter, NOT a safety layer: the `deny` blocklist
# and the `[ASK]` escalations do not fire in this state.
if [ -f "${HOME}/.dippy/ALLOW_ONLY" ]; then
  output=$(printf '%s' "$payload" | dippy)
  decision=$(printf '%s' "$output" | jq -r '.hookSpecificOutput.permissionDecision // ""' 2>/dev/null)
  [ "$decision" = "allow" ] && printf '%s' "$output"
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
