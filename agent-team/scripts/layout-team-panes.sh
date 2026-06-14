#!/usr/bin/env bash
# layout-team-panes.sh — normalize the cmux pane layout for an agent-team run:
# team-lead on the LEFT half, all teammate panes as equal HORIZONTAL strips on the
# RIGHT half, any non-agent bystander panes stacked in the LEFT column under the
# lead. Idempotent; a clean no-op when not under the cmux claude-teams launcher.
#
# Usage:
#   layout-team-panes.sh <lead-surface-ref> [teammate-surface-ref ...]
#
# Args are cmux surface refs (e.g. surface:92). The orchestrator gets the lead with
#   cmux identify --json | jq -r .caller.surface_ref
# and the teammate surfaces from the pane.list surfaces that APPEARED with this
# spawn wave (snapshot surface refs before spawning, diff after). Any surface that
# is neither the lead nor a listed teammate is treated as a bystander -> left column.
#
# Exit codes:
#   0  laid out OK (or already canonical, or a clean no-op outside cmux / no teammates)
#   2  usage error (no lead surface given)
#   3  LAYOUT-MISS: could not reach the canonical shape. A pane.list snapshot is
#      saved under ~/.claude/cmux-layout-misses/ for /dot-ai-reflect to fold the new
#      edge case into THIS script (see the agent-team SKILL.md "self-improving" note).
#
# bash 3.2-safe (macOS default): no mapfile, no associative arrays, no `set -u`
# (empty-array expansion is intentional and must not abort).
set -o pipefail

CMUX=$(command -v cmux || echo /Applications/cmux.app/Contents/Resources/bin/cmux)

# Detect the launcher by its $TMUX socket, NOT `command -v cmux`: the claude-teams
# shim keeps the real cmux CLI off PATH, so a `command -v` gate silently skips the
# whole fix. No-op cleanly when not under cmux.
case "${TMUX:-}" in
  *cmux-claude-teams*) : ;;  # under the cmux claude-teams launcher — proceed
  *) echo "layout-team-panes: not under cmux claude-teams (TMUX=${TMUX:-unset}) — no-op"; exit 0 ;;
esac
if [ ! -x "$CMUX" ]; then
  echo "layout-team-panes: cmux CLI not found at $CMUX — no-op"
  exit 0
fi

LEAD_SURF=${1:-}
if [ -z "$LEAD_SURF" ]; then
  echo "layout-team-panes: usage: $0 <lead-surface> [teammate-surface ...]" >&2
  exit 2
fi
shift
TEAMMATES=("$@")
# Nothing to place on the right -> nothing to do (the script's job is teammate panes).
if [ "${#TEAMMATES[@]}" -eq 0 ]; then
  echo "layout-team-panes: no teammate surfaces given — no-op"
  exit 0
fi
TEAMMATES_STR=" ${TEAMMATES[*]} "
is_teammate() { case "$TEAMMATES_STR" in *" $1 "*) return 0;; esac; return 1; }

WS=$("$CMUX" identify --json | jq -r '.caller.workspace_ref')
WSJSON=$(jq -nc --arg ws "$WS" '{workspace_id: $ws}')
plist() { "$CMUX" rpc pane.list "$WSJSON"; }
pane_of() {
  # `first(...) // empty` returns only the first match without a `head -1` pipe
  # (which would SIGPIPE jq under `set -o pipefail`).
  plist | jq -r --arg s "$1" \
    'first(.panes[] | select((.selected_surface_ref==$s) or (any(.surface_refs[]?; .==$s))) | .ref) // empty'
}

LEAD_PANE=$(pane_of "$LEAD_SURF")
if [ -z "$LEAD_PANE" ]; then
  echo "layout-team-panes: lead surface $LEAD_SURF not in workspace — no-op" >&2
  exit 0
fi

# Snapshot every surface present at START. Anything that appears later is a shell
# cmux auto-respawned into a pane we emptied (a stray) -> closed after reshape.
START_SURFACES=""
while IFS= read -r s; do
  [ -z "$s" ] && continue
  START_SURFACES="$START_SURFACES $s"
done < <(plist | jq -r '.panes[].selected_surface_ref')
# A failed/empty snapshot must NOT proceed: close_strays would then treat every
# live surface as a stray and close it. Fail closed to a no-op instead.
if [ -z "$START_SURFACES" ]; then
  echo "layout-team-panes: could not read pane.list (empty surface snapshot) — no-op" >&2
  exit 0
fi

# Bystanders = every start surface that is neither the lead nor a teammate.
BYSTANDERS=()
for s in $START_SURFACES; do
  [ "$s" = "$LEAD_SURF" ] && continue
  if is_teammate "$s"; then continue; fi
  BYSTANDERS+=("$s")
done

# Capture cmux op stderr (invalid_state / "would leave the source pane empty" / etc.)
# so a failed reshape is diagnosable in the LAYOUT-MISS snapshot instead of vanishing
# down /dev/null. Removed on exit.
OPLOG=$(mktemp 2>/dev/null || echo "/tmp/layout-team-panes.$$.log")
trap 'rm -f "$OPLOG"' EXIT

# Canonical-shape check: exact pane count (no strays), lead on the left (x~0, not
# full width), every teammate pane in the right column (x ~= lead.x+lead.w).
verify() {
  local snap cw lx lw boundary t tx want got
  snap=$(plist) || return 1
  want=$((1 + ${#TEAMMATES[@]} + ${#BYSTANDERS[@]}))
  got=$(jq '.panes | length' <<<"$snap")
  [ "$got" = "$want" ] || return 1
  cw=$(jq -r '[.panes[] | (.pixel_frame.x + .pixel_frame.width)] | max' <<<"$snap")
  lx=$(jq -r --arg p "$LEAD_PANE" '.panes[]|select(.ref==$p)|.pixel_frame.x' <<<"$snap")
  lw=$(jq -r --arg p "$LEAD_PANE" '.panes[]|select(.ref==$p)|.pixel_frame.width' <<<"$snap")
  { [ -n "$cw" ] && [ -n "$lx" ] && [ -n "$lw" ]; } || return 1
  awk -v lx="$lx" -v lw="$lw" -v cw="$cw" 'BEGIN{exit !(lx<20 && lw<cw*0.75)}' || return 1
  boundary=$(awk -v lx="$lx" -v lw="$lw" 'BEGIN{print lx+lw}')
  for t in "${TEAMMATES[@]}"; do
    [ -z "$t" ] && continue
    tx=$(jq -r --arg s "$t" '.panes[]|select(.selected_surface_ref==$s)|.pixel_frame.x' <<<"$snap")
    [ -z "$tx" ] && return 1
    awk -v tx="$tx" -v b="$boundary" -v cw="$cw" 'BEGIN{exit !(tx>b-25 && tx<b+25 && tx>cw*0.4)}' || return 1
  done
  # Bystanders must sit in the LEFT column (x in the left half), not the right stack.
  for t in "${BYSTANDERS[@]}"; do
    [ -z "$t" ] && continue
    tx=$(jq -r --arg s "$t" '.panes[]|select(.selected_surface_ref==$s)|.pixel_frame.x' <<<"$snap")
    [ -z "$tx" ] && return 1
    awk -v tx="$tx" -v cw="$cw" 'BEGIN{exit !(tx < cw*0.4)}' || return 1
  done
  return 0
}

# Close any surface NOT present at start: a shell cmux respawned into a pane we
# emptied during the collapse. `close-surface` removes it without leaving a new
# stray (verified). Bounded passes in case a close cascades.
close_strays() {
  local pass s found
  # Never run against an empty start set ("close everything not in {}" = close all).
  [ -z "$START_SURFACES" ] && return 0
  for pass in 1 2 3; do
    found=0
    while IFS= read -r s; do
      [ -z "$s" ] && continue
      case " $START_SURFACES " in
        *" $s "*) : ;;
        *) "$CMUX" close-surface --surface "$s" >/dev/null 2>>"$OPLOG"; found=1 ;;
      esac
    done < <(plist | jq -r '.panes[].selected_surface_ref')
    [ "$found" -eq 0 ] && break
  done
}

# The cmux pane tree is eventually-consistent: a structural op (move-surface /
# split-off) is not reflected in pane.list instantly. Fire-and-continue races the
# next op against stale state and derails the whole reshape. These helpers poll
# until the op is observed — each pane_of is an RPC round-trip, which is the
# implicit pacing (no sleep). Bounded so a genuinely-stuck op still terminates.
SETTLE_TRIES=24
# Wait until surface $1 is reported in pane $2 (a move-surface landed).
confirm_in_pane() {
  local s=$1 want=$2 i=0 p
  while [ "$i" -lt "$SETTLE_TRIES" ]; do
    p=$(pane_of "$s")
    [ "$p" = "$want" ] && return 0
    sleep 0.05 2>/dev/null || true  # real pacing even if pane.list is cache-fast
    i=$((i + 1))
  done
  return 1
}
# Wait until surface $1 lives in a pane OTHER than $2 (a split-off gave it its own
# pane); echo that new pane ref. Returns 1 if it never separated.
wait_own_pane() {
  local s=$1 not=$2 i=0 p=""
  while [ "$i" -lt "$SETTLE_TRIES" ]; do
    p=$(pane_of "$s")
    if [ -n "$p" ] && [ "$p" != "$not" ]; then echo "$p"; return 0; fi
    sleep 0.05 2>/dev/null || true  # real pacing even if pane.list is cache-fast
    i=$((i + 1))
  done
  echo "$p"; return 1
}

# --- cmux pane-model semantics (validated 2026-06-14 against the upstream source,
# github.com/manaflow-ai/cmux, plus the live CLI). Notes for future reflect passes:
#  * split-off is SOURCE-RELATIVE (a local bonsai-tree split, not a root anchor) and
#    REFUSES a single-surface source ("would leave the source pane empty";
#    invalid_state). This reshape stays safe via the move-THEN-split order below:
#    each split-off fires from a pane holding >=2 tabs at that moment.
#  * move-surface tolerates a single-surface source and auto-closes the emptied one;
#    a split-off/drag that empties a pane instead leaves a placeholder cmux replaces
#    with a fresh terminal (the "stray" close_strays cleans up).
#  * pixel_frame geometry updates on a DEFERRED pass (membership is synchronous), so
#    verify() (geometry-based) is POLLED; confirm_in_pane/wait_own_pane key off
#    membership and mainly serve as pacing.
#  * Better primitives for a future rewrite: swap-pane (reorder without the empty-pane
#    dance), join-pane (one-call collapse), new-pane / new-split --panel (build the
#    right column as panes, then move-surface in — move has no single-surface guard).
#    Kept collapse+split-off for now: validated, and it spawns fewer strays.
reshape() {
  local s head i prev
  # 1. collapse teammates + bystanders into the lead pane as tabs (intermediate;
  #    the panes-not-tabs rule is about the END state). Confirm each move landed
  #    before the next op.
  for s in "${TEAMMATES[@]}" "${BYSTANDERS[@]}"; do
    [ -z "$s" ] && continue
    "$CMUX" move-surface --surface "$s" --pane "$LEAD_PANE" >/dev/null 2>>"$OPLOG"
    confirm_in_pane "$s" "$LEAD_PANE"
  done
  # 2-3. anchor the right half with the first teammate, stack the rest DOWN. Always
  #      split-off RIGHT from the lead first; a naive split-off down inside a column
  #      would build one full-width stack including the lead. Wait for each split to
  #      yield its own pane before splitting the next off it.
  "$CMUX" split-off --surface "${TEAMMATES[0]}" right >/dev/null 2>>"$OPLOG"
  head=$(wait_own_pane "${TEAMMATES[0]}" "$LEAD_PANE")
  # Anchor split never separated into its own pane: bail rather than issue ops
  # against an empty pane ref. verify() then fails closed -> LAYOUT-MISS capture.
  if [ -z "$head" ] || [ "$head" = "$LEAD_PANE" ]; then close_strays; return 1; fi
  i=1
  while [ "$i" -lt "${#TEAMMATES[@]}" ]; do
    prev=$head
    "$CMUX" move-surface --surface "${TEAMMATES[$i]}" --pane "$head" >/dev/null 2>>"$OPLOG"
    confirm_in_pane "${TEAMMATES[$i]}" "$head"
    "$CMUX" split-off --surface "${TEAMMATES[$i]}" down >/dev/null 2>>"$OPLOG"
    head=$(wait_own_pane "${TEAMMATES[$i]}" "$prev")
    [ -z "$head" ] && { close_strays; return 1; }  # split never settled; bail to MISS
    i=$((i + 1))
  done
  # 4. bystanders: drop each as a strip in the LEFT column under the lead.
  for s in "${BYSTANDERS[@]}"; do
    [ -z "$s" ] && continue
    "$CMUX" split-off --surface "$s" down >/dev/null 2>>"$OPLOG"
    wait_own_pane "$s" "$LEAD_PANE" >/dev/null
  done
  # 5. close respawned strays, then equalize + focus the lead.
  close_strays
  "$CMUX" rpc workspace.equalize_splits "$WSJSON" >/dev/null 2>>"$OPLOG"
  "$CMUX" focus-pane "$LEAD_PANE" >/dev/null 2>>"$OPLOG"
}

# Idempotent: if already canonical, do NOT churn the layout. Re-running after a wave
# that didn't actually skew must be a no-op, and reshaping a good layout is itself
# what spawns strays (emptying a pane respawns a shell).
if verify; then echo "layout-team-panes: LAYOUT-OK (already canonical)"; exit 0; fi

reshape
# pixel_frame geometry is updated by a DEFERRED reconcile after a structural op,
# while verify() keys off pixel_frame — so the first read can lag. Poll verify a few
# times, nudging with equalize between, before declaring a miss.
settle_i=0
while [ "$settle_i" -lt 6 ]; do
  if verify; then echo "layout-team-panes: LAYOUT-OK"; exit 0; fi
  "$CMUX" rpc workspace.equalize_splits "$WSJSON" >/dev/null 2>>"$OPLOG"
  sleep 0.1 2>/dev/null || true
  settle_i=$((settle_i + 1))
done

# Edge case: shape still wrong. Capture for /dot-ai-reflect to learn from.
MISS_DIR="$HOME/.claude/cmux-layout-misses"
mkdir -p "$MISS_DIR"
MISS="$MISS_DIR/$(date +%Y%m%dT%H%M%S).json"
TEAM_JSON=$(printf '%s\n' "${TEAMMATES[@]}" | jq -R . | jq -s 'map(select(length>0))')
BYS_JSON=$(printf '%s\n' "${BYSTANDERS[@]}" | jq -R . | jq -s 'map(select(length>0))')
OPERR=$(cat "$OPLOG" 2>/dev/null)
if ! jq -n --arg lead "$LEAD_SURF" --argjson mates "$TEAM_JSON" --argjson bys "$BYS_JSON" \
      --arg operr "$OPERR" --argjson panes "$(plist)" \
      '{lead_surface:$lead, teammates:$mates, bystanders:$bys, op_errors:$operr, pane_list:$panes}' \
      >"$MISS" 2>/dev/null; then
  plist >"$MISS" 2>/dev/null || true
fi
echo "layout-team-panes: LAYOUT-MISS — could not normalize; snapshot at $MISS (run /dot-ai-reflect agent-team to fold this shape in)" >&2
exit 3
