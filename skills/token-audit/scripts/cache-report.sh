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
  # Claude Code names the project dir by replacing every '/' and '.' in the
  # absolute cwd with '-'. Reproduce that to locate the current project's logs.
  local slug
  slug=$(printf '%s' "$PWD" | tr '/.' '--')
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
  # Keep only assistant turns that carry a usage block.
  [ .[] | select(.type=="assistant" and (.message.usage != null)) | .message.usage ] as $u
  | ($u | length) as $turns
  | if $turns == 0 then
      {error: "no assistant turns with usage in this log"}
    else
      # Per-turn field extraction with safe defaults.
      ($u | map(.cache_read_input_tokens // 0))     as $cr
    | ($u | map(.cache_creation_input_tokens // 0))  as $cc
    | ($u | map(.input_tokens // 0))                 as $in
    | ($u | map(.output_tokens // 0))                as $out
    | ($cr | add) as $scr | ($cc | add) as $scc
    | ($in | add) as $sin | ($out | add) as $sout
    | ($scr + $scc + $sin + $sout) as $tot
    # Context size (input side) fed to a turn = cache_read + cache_creation + input.
    | ($cr[0]  + $cc[0]  + $in[0])  as $ctx_first
    | ($cr[-1] + $cc[-1] + $in[-1]) as $ctx_last
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
        cache_read_input_tokens:     {sum: $scr,  pct: pct($scr)},
        cache_creation_input_tokens: {sum: $scc,  pct: pct($scc)},
        input_tokens:                {sum: $sin,  pct: pct($sin)},
        output_tokens:               {sum: $sout, pct: pct($sout)},
        grand_total: $tot,
        context_first_turn: $ctx_first,
        context_last_turn:  $ctx_last,
        cache_ttl_in_use:   $ttl
      }
    end
' "$LOG"
