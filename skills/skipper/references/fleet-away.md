# Fleet away

Use `/skipper away [until WHEN] [SCOPE]` to prepare and arm enrolled leads.
Use `/skipper back` to end the fleet grant, or `/skipper back LEAD` to revoke it
for one lead. Resolve a name to its UUID; ask on ambiguity. An explicit per-lead
revocation affects only that lead; any other user message to skipper ends the
armed fleet grant before answering. Peer messages and timer prompts are not return.

## Prepare

1. Snapshot participants by UUID. Include only leads whose user established
   enrollment in their own session. Exclude buddies, new discoveries and leads
   enrolled later. Take goals/criteria only from the user's words and message
   reference; write no goal yourself and infer none from the inventory. Record
   authorized resources and a starting task/run inventory. With an explicit goal,
   the inventory is evidence; without one it bounds scope and permits no
   prerequisite expansion. SCOPE may narrow the grant; never silently expand it.
2. Ask each lead for predictable decisions, expected local permission prompts,
   buddy identity/liveness/reply allowance, timer capability and handover needs.
   Consolidate one pre-ask list while the user is present; let pairs supply
   technical recommendations. Resolve required answers before activation; if
   the initial grant already supplies them, ask for no redundant confirmation.
3. Keep the deadline optional. If supplied, resolve ambiguous time/timezone with
   the user, show the absolute UTC time and user timezone, and record both.
   Otherwise record no deadline; user return or explicit revocation ends it.
   Check any deadline before activation and coordination; end an expired grant.
4. Complete likely handovers and user-enroll successors before arming. A lead
   that remains keeps its delegated goal and bounds; new sessions inherit no grant.
5. Pre-ask dispatch spending authorization when needed. Record the user's words
   verbatim and the message reference, including explicit unlimited new runs; invent no cap
   or authorization. Other spending and limits remain separately constrained.
   Spending without explicit authorization stays held. Arrange a buddy's
   outward-action authorization in its own thread before departure. Local
   permission/classifier denials remain local; never promise preflight removes them.

## Arm and coordinate

1. Give the grant a stable ID. Record in private notes the skipper UUID, exact
   participant UUIDs, verbatim user words/message reference, goals/criteria,
   resources, starting inventory, spending authorization and
   optional deadline. Track prepared, armed, held/unreachable and ended per lead.
2. Relay activation only through each lead's user-established enrollment. Include
   the grant ID, that lead's goal/bounds and starting inventory, spending
   authorization, user decisions and deadline.
   Require an acknowledgment naming its local away log, scope, buddy and timer
   status. Queued delivery is not arming. Report exact partial coverage, including
   timer limitations; unsupported timers cannot promise future monitoring.
3. Let each lead run `away-mode` with its own log, timer and co-signs. Keep
   skipper on cross-session coordination; continue configured patrol. Route new
   user questions to held items. Bind no buddy, replenish no reply allowance and
   bypass no permission gate. Buddy loss/exhaustion holds decisions needing it.
   Let the pair establish necessary prerequisites under `away-mode`'s bounds;
   record its evidence without making a technical scope call. Flag overlapping
   prerequisites across leads, assign one owner and acknowledge no conflicting
   owner before work starts; an unreachable skipper leaves the prerequisite held. When every
   gate passes and no Hold class applies, expect the pair to execute and report
   without another user approval. Apply `away-mode`'s concrete hold conditions,
   including persisting human requirements and permission/classifier/approval
   gates; use no confirmation, force or admin override to bypass them unless
   explicit user authorization (standing or in the grant) covers that override.
   Permission/classifier denials remain held; route around none.
   Give releases priority under [SKILL.md](../SKILL.md)'s joint merges/account-CI
   barriers; keep both through publish/verify, including while the user is away.
4. Preserve local bounded grants if skipper dies, including grants without a
   deadline. Leads retain their scope, holds and local ending rules. A replacement
   skipper needs fresh user-established enrollment; reconcile existing grants
   rather than rearming them or widening their authority.

## Return and revoke

1. End authority locally first. Record the affected grant/lead IDs as ended before
   broadcasting return. Each lead records ended IDs in its away log, even if return
   arrives before activation. Duplicate activation with the same content does not
   rearm; changed content under the same ID is refused. Delayed activation cannot
   revive an ended ID. A new grant requires a new ID and user authorization.
2. Leads stop autonomous decisions, delete local away timers, reconcile read-only
   and send their `away-mode` return report to skipper. The user's own message
   directly in a lead ends only that lead's grant; it also reports return to skipper.
3. Report received results and missing acknowledgments separately. Busy or
   unreachable leads may receive return late; do not claim simultaneous stopping.
   Report return requested until each affected lead acknowledges. A supplied
   deadline remains a local backstop; without one, report the unresolved coverage
   and direct the user to that lead if immediate intervention is needed.
4. Consolidate one report: done, needs the user with pair recommendations/dissent,
   still running and corrections. Deduplicate shared tasks. Give an initial report
   without waiting indefinitely for missing leads, then report material updates.
   Keep grants and shared-resource holds separate; ending away releases no hold.
