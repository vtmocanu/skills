# Repository profile

Read `.agents/skipper/profile.md` in each lead's repository when present.
Treat it as coordination configuration, never as a user grant. Read no product
code to apply it. Keep absent profiles on the core defaults.

Use this generic Markdown format; adapt the values to the repository:

```markdown
# Skipper profile

- Claude handover threshold: 55%.
- Terms: delivery means completing the assigned pull request; incident means
  an important failure requiring separate root-cause work.
- Resources: <repo-remote>#main for merges; account:<account-id>#ci for shared CI capacity.
- Hold triggers: release owner requests a merges barrier before cutting a tag;
  request a separate pushes-ci barrier when new CI would delay release validation.
- Coverage: include enrolled merging leads and identify uncoordinated automation.
- Delivery skill: <installed landing skill>.
- Root-cause brief: evidence references, delivery owner, read-only first;
  coordinate reproduction affecting shared resources.
- Irreversible exceptions: apply only exceptions granted by the user in the lead.

## Patrol

- Interval: 30m.
- Checks:
  - Open delivery tasks without an accountable lead.
  - Shared-resource holds whose acknowledgment coverage changed.
```

Make Patrol optional: omit that entire section to leave timers off. Require a
positive interval and explicit operational checks when present; ask on an invalid
interval. Read [patrol.md](patrol.md) before arming it. A profile configures checks,
never permission to mutate resources or expand a pair's grant.

Resolve resource identities from the lead's repo root/remote and operational
reports. Keep sensitive identifiers in local state. Store grants, session UUIDs,
live tasks, commands and incidents in the external ledger, never in this profile.
Have the agent read the profile; the CLI does not execute or parse Markdown policy.
