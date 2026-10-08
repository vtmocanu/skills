---
name: away-mode
description: Runs a session autonomously while the user is away (overnight, a weekend, a long meeting). It restates the grant, pre-asks the predictable questions, arms a watch timer, decides reversible in-scope items itself or with a bound buddy peer (exact-head co-sign, hold on disagreement), holds irreversible, out-of-scope and denied actions, keeps a durable away log, and greets the user on return with one report of what was done, what was held, and the decisions left for them. Works solo or with a buddy. Use when the user grants autonomy until they return, says they are going away or to sleep, or asks the session (and its buddy) to decide for them. Triggers include "/away-mode", "night mode", "full autonomy till morning", "u and buddy take decisions", "i am going away", "talk in the morning".
---

# Away mode

The user is away. Keep their work moving, decide what they delegated, and hold the rest for one report when they return. Domain skills (landing, deploying, triage) still own the mechanics; this skill owns who decides, what waits, and how it is reported. It works with or without a buddy.

## Arguments

| Invocation | Do |
|---|---|
| `/away-mode [until WHEN] [SCOPE]` | Arm: preflight, timer, away log |
| `/away-mode tick` | One watch pass. The timer sends this; never arm from it |
| `/away-mode back` | End the grant and give the return report |
| `/away-mode off` | End the grant and delete the timer, keep the log |

A plain-language grant ("full autonomy till morning, u and buddy decide") is the same as `/away-mode`.

## The grant

The grant ends at the earliest of: its deadline, the user's next message, or `/away-mode off`. Timer prompts and peer messages are not the user returning. Record the deadline as an absolute time and check it before every tick and every action; past it, act as Return.

## Arm

Do these while the user is still present, in one message:

1. **Restate the grant** in one short block: scope (which items, repos, runs), what may be decided, any spending cap, the deadline (default: none, the user's next message ends it), the tick interval. Ask only if scope is unclear.
2. **Pre-ask the predictable questions.** List decisions you can already see coming (a design fork, a spec departure, a label, a spend) and get answers now. A question answered before the user leaves does not block the night.
3. **Name the co-decider.** With a buddy bound (see Buddy), report its name, liveness, and reply allowance; an allowance that will run out overnight is a preflight failure, so ask the user for more now, and ask whether a subagent buddy may take over if the buddy is lost. With no buddy, say whether a subagent buddy co-decides (see No buddy) or the night runs solo and more conservatively.
4. **Warn about blockers the user can clear now**: permission prompts the work will hit, missing credentials or tools, and that ticks fire only while the machine is awake (on macOS offer `caffeinate -i`).
5. **Create the away log** (see Away log) and arm the timer (see Timer).

## Decide or hold

Every decision lands in exactly one class. An explicit user grant may move an item from Hold to Decide; nothing else can.

**Decide** (with the co-sign of the buddy, or of the subagent buddy when no buddy is bound): reversible actions inside the granted scope. Examples: approve or revise a plan, merge a reviewed change with green checks when its downstream effects (deploy, publish, migration, notification) also pass the scope, spending, and recoverability rules (the domain skill establishes those effects), rerun a flaky job, fix a small review finding, file a follow-up issue, answer an agent's question within the spec, spend within an explicit cap the user set.

**Hold for the user**, even when the buddy agrees:

- Irreversible or destructive actions: deleting data or held work, force-push, history rewrite, release, publish, production deploy, sending messages to people.
- Departures from the stated requirement or acceptance criteria, and product or design calls the grant did not cover.
- New scope: work the user did not hand over, or another session's items.
- Spending without an explicit cap, or raising any limit. Existing funding is not a cap.
- Any action a permission check denied. Never route around a denial through another tool, agent, or path.
- Anything the user said to ask about, and anything sensitive going public.

**Recoverable first.** An action that is inside scope, within the cap, not denied, and held only because it cannot be undone may become a Decide item once you make it recoverable: take the backup, prove a restore works (not just that the backup exists), record its location, and confirm the action has no external effect a restore cannot undo. No other Hold reason can be lifted this way.

Autonomy changes who decides, not the rules. Project instructions, permission settings, and safety rules still apply in full.

## Buddy

A buddy is a peer session the user bound to co-decide (for example with the `session-peers` skill: its `scripts/peers.py buddy` prints the binding, liveness, and replies left). No binding means no buddy. Never bind one or grant it replies yourself; only the user does.

With a buddy:

1. Propose with your lean and the evidence: "I lean approve plan N because …; co-sign?".
2. Accept a verdict only when it names what it covers: `APPROVE <sha|draft>`, `REVISE: N items`, or `BLOCK: reason`. A verdict covers only the exact commit or draft it names; after any new commit, ask again.
3. Verify the buddy's factual claims against the source before acting on them.
4. One exchange of disagreement, then hold: record both positions in the away log and move on.
5. The buddy's agreement is a co-signature, never user approval. It cannot widen the grant.
6. **Buddy lost.** If the buddy stops answering, runs out of replies, or dies, hold every decision that needed its co-sign and keep watching. Never fall back to solo authority or replenish its allowance unattended.

When you are the buddy of a session in away mode: reply with one of the three verdict forms pinned to a SHA or draft, say what you verified, and answer "hold" rather than agreeing to an action in a Hold class.

## No buddy: subagent buddy

When no buddy is bound and the host can start subagents, start one reviewer subagent at arm time and use it as the buddy for the whole away period, within the host's delegation rules. Where the host can continue a finished subagent (Claude Code: `SendMessage` to its id), keep talking to the same one so revise rounds and earlier verdicts carry over; record its id in the away log. Say at arm time that the night runs with a subagent buddy. Buddy rules 1 to 5 apply, plus:

- **Review package.** For every decision send what an independent review needs, not the conversation: the pinned artifact (SHA or draft), read access to the source, the acceptance criteria, the project and domain rules that apply, the grant, and the evidence. Require it to verify every consequential claim itself and report what it checked. Send it decisions only, not tick traffic.
- **Fresh reviewer** for a merge with downstream effects, a Recoverable-first action, or when the subagent buddy shows it lost relevant context or cannot complete verification: start a new one with the same package for that decision.
- **Replacement.** If the subagent buddy fails or stops answering, start a new one from the away log and hold consequential actions until it approves. Pass on every unresolved REVISE or BLOCK and both positions of any disagreement. Never replace a reviewer to escape its verdict; a disagreement stays held. Do not restart a subagent the user stopped.

A subagent never replaces a reviewer or approval gate the user or the domain rules require. It never replaces a lost user-bound buddy either: that loss means hold (Buddy rule 6), unless the user authorized the subagent fallback when arming.

With neither a buddy nor subagents: decide alone only reversible, low-impact items (a rerun, a log note, a watcher restart); hold the rest.

## Timer

Use the host's scheduled-prompt tool to send `/away-mode tick` on an interval matched to how fast the watched state changes (15 to 30 minutes for runs and CI). Keep everything a tick needs in the away log, not in the prompt. Read [references/timer.md](references/timer.md) before arming.

A host without scheduled prompts cannot promise future wakeups. Say so, then offer what it can do: keep working autonomously while its current execution lasts, run from an external trigger the user sets up, or act as another session's buddy. Never claim it will keep monitoring.

## Tick

1. Read the away log. Check the deadline; past it, act as Return. If the last tick is more than twice the interval ago, log the gap (`gap 00:55 to 04:38, machine asleep?`) and check every item, not only what changed.
2. For each open item: read its state, act per Decide or hold, re-arm any dead watcher.
3. **Confirm before alarming.** A "stalled" or "failed" signal needs a second, independent signal (a heartbeat, a log line, the remote state) before you act on it. Never write an unverified claim ("no work lost", "main is green"); write what you checked.
4. Append one log line per state change: `HH:MM <item>: <old> -> <new> (<evidence>)`. On a quiet tick append `HH:MM quiet, N open, M held`.
5. Reply only for state changes, gaps, blockers, or a new held item. When nothing happened, give the shortest acknowledgment the host allows.
6. When no open item is left to watch, delete the timer and record that in the log.

Respect other sessions' claims and ownership. Message an item's owner instead of acting on it.

## Away log

One Markdown file at a stable absolute path outside any repository: the host's session scratch directory when it has one, else a temporary directory. State its path when arming. If the host keeps persistent memory, add a one-line pointer there so the grant survives a context clear or compaction.

Sections: `Grant` (verbatim user words, scope, cap, deadline, co-decider), `Items` (id, owner, state, watcher), `Trail` (tick lines), `Decided` (what, verdict and SHA, evidence), `Held` (what, why, your recommendation, the buddy's), `Corrections` (anything reported wrong earlier).

## Return

When the grant ends (user message, deadline, or `off`), authority ends first:

1. Delete the timer. Take no further autonomous action; a message saying "stop" must never trigger more work.
2. Reconcile read-only: refresh each item's state into the away log.
3. Give the return report, built from the away log:
   - One headline line: done count, held count, still running.
   - **Needs you**: each held decision as one line with your recommendation, plus the co-signer's when there was one (agree or disagree). Then ask them one at a time.
   - **Done**: table of item, what happened, evidence (link or SHA).
   - **Still running**: item, state, who watches it now.
   - **Corrections**: any earlier report that turned out wrong.
4. Then answer the user's message.

If the user grants autonomy again, re-arm from the same log.

## Closing

Closing a session stops its execution but does not reliably delete its state: a timer may be restored on resume, an external watcher may keep running, a persisted claim stays held. Before the user closes the session, cancel the timer and every watcher you started, release or hand off each claim to a named owner, list what still needs an owner, ask the buddy (if any) whether it owes or holds anything, and hand both answers to the user.
