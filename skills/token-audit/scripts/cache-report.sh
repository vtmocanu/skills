#!/usr/bin/env bash
# cache-report.sh — measure exact token flow for one Claude Code session log.
#
# Reads a session JSONL (default: the newest one for the current project) and
# emits EXACT token sums — the numbers the API itself logged, not estimates.
# Every assistant turn's usage block is summed across the four token fields;
# each is reported as a share of the grand total. Also reports the context
# size (input side) on the first and last turn, and the prompt-cache TTL the
# session actually used (5m vs 1h), so a caller can compare cron/loop
# intervals against a measured cache lifetime rather than an assumed one.
#
# Usage:
#   cache-report.sh                 # newest log for $PWD's project
#   cache-report.sh /path/to.jsonl  # a specific session log
#   cache-report.sh --list          # list this project's logs, newest first
#
# Requires: jq. Reads only; writes nothing.
set -euo pipefail

projdir() {
  # Claude Code names the project dir by replacing every character that is not
  # [A-Za-z0-9] in the absolute cwd with '-' (repeats are NOT collapsed, so a
  # leading '/' plus a dotfile like '/.bb' becomes '--bb'). Reproduce exactly.
  # tr '/.' would miss '_', spaces, '@', etc. and point at a nonexistent dir.
  local slug
  slug=$(printf '%s' "$PWD" | tr -c 'A-Za-z0-9' '-')
  printf '%s/.claude/projects/%s' "$HOME" "$slug"
}

newest_log() {
  local dir="$1"
  [ -d "$dir" ] || return 1
  ls -t "$dir"/*.jsonl 2>/dev/null | head -1
}

if ! command -v jq >/dev/null 2>&1; then
  echo "cache-report: jq not found on PATH" >&2
  exit 2
fi

DIR=$(projdir)

if [ "${1:-}" = "--list" ]; then
  if [ -d "$DIR" ]; then ls -t "$DIR"/*.jsonl 2>/dev/null; else echo "no project dir: $DIR" >&2; fi
  exit 0
fi

LOG="${1:-}"
if [ -z "$LOG" ]; then
  LOG=$(newest_log "$DIR" || true)
  if [ -z "${LOG:-}" ]; then
    echo "cache-report: no *.jsonl under $DIR" >&2
    echo "Pass a path explicitly, or run with --list. All projects:" >&2
    ls -1dt "$HOME"/.claude/projects/*/ 2>/dev/null | head -20 >&2
    exit 1
  fi
fi

if [ ! -f "$LOG" ]; then
  echo "cache-report: no such file: $LOG" >&2
  exit 1
fi

echo "log: $LOG"

jq -s '
  # Keep assistant turns that carry a usage block; retain the sidechain flag so
  # subagent turns can be handled separately.
  [ .[] | select(.type=="assistant" and (.message.usage != null))
    | {u: .message.usage, side: (.isSidechain // false)} ] as $all
  | ($all | length) as $turns
  | if $turns == 0 then
      {error: "no assistant turns with usage in this log"}
    else
      # Token sums cover ALL turns = total session spend, subagents included.
      ($all | map(.u))                             as $u
      # Context size is a main-thread property, so exclude sidechain turns for
      # first/last; fall back to all turns if the log is somehow all-sidechain.
    | ($all | map(select(.side | not) | .u))       as $main
    | ($main | length)                             as $mturns
    | (if $mturns > 0 then $main else $u end)       as $ctx
    | ($u | map(.cache_read_input_tokens // 0))     as $cr
    | ($u | map(.cache_creation_input_tokens // 0)) as $cc
    | ($u | map(.input_tokens // 0))                as $in
    | ($u | map(.output_tokens // 0))               as $out
    | ($cr | add) as $scr | ($cc | add) as $scc
    | ($in | add) as $sin | ($out | add) as $sout
    | ($scr + $scc + $sin + $sout) as $tot
    # Context size (input side) fed to a turn = cache_read + cache_creation + input.
    | (($ctx[0].cache_read_input_tokens // 0) + ($ctx[0].cache_creation_input_tokens // 0) + ($ctx[0].input_tokens // 0))    as $ctx_first
    | (($ctx[-1].cache_read_input_tokens // 0) + ($ctx[-1].cache_creation_input_tokens // 0) + ($ctx[-1].input_tokens // 0)) as $ctx_last
    # Which ephemeral cache bucket the session actually wrote to.
    | ([ $u[] | .cache_creation.ephemeral_1h_input_tokens // 0 ] | add) as $e1h
    | ([ $u[] | .cache_creation.ephemeral_5m_input_tokens // 0 ] | add) as $e5m
    | (if $e1h > 0 and $e5m == 0 then "1h"
       elif $e5m > 0 and $e1h == 0 then "5m"
       elif $e1h > 0 and $e5m > 0 then "mixed (1h+5m)"
       else "unknown (no ephemeral field)" end) as $ttl
    | def pct($x): if $tot == 0 then 0 else (($x * 1000 / $tot | round) / 10) end;
      {
        assistant_turns: $turns,
        subagent_turns:  ($turns - $mturns),
        cache_read_input_tokens:     {sum: $scr,  pct: pct($scr)},
        cache_creation_input_tokens: {sum: $scc,  pct: pct($scc)},
        input_tokens:                {sum: $sin,  pct: pct($sin)},
        output_tokens:               {sum: $sout, pct: pct($sout)},
        grand_total: $tot,
        context_first_turn: $ctx_first,
        context_last_turn:  $ctx_last,
        context_basis: (if $mturns > 0 then "main-thread turns" else "all turns (no main-thread turn found)" end),
        cache_ttl_in_use:   $ttl,
        note: "sums cover all turns (subagents included); context sizes exclude sidechain turns"
      }
    end
' "$LOG"
