#!/usr/bin/env bash
# In auto mode: run dippy and forward only explicit allow/deny decisions
# (whitelist + blocklist), letting auto-mode's classifier handle anything
# dippy would ask on. In other modes: always hand stdin to dippy.
set -u
payload=$(cat)
mode=$(printf '%s' "$payload" | jq -r '.permission_mode // "default"' 2>/dev/null)

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
