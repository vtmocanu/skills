---
name: reflect
description: Analyze the current session and propose improvements to agent skills based on what worked, what didn't, and edge cases discovered. Run after using a skill to capture learnings and update the skill file. Use when user says "reflect", "improve skill", "learn from this", "update the skill", or at end of skill-heavy sessions. Supports targeting a specific skill with /reflect [skill-name] or auto-detecting which skills were used.
---

# Reflect

Analyze the current session and turn what was learned into concrete edits to the skill's source file.

## Workflow

### 1. Identify the Skill

**Auto-detect** which skills were used by scanning the conversation for:

- `<command-name>/skill-name</command-name>` tags
- Reads of a skill's source file (a `SKILL.md` or `<skill-name>.md` in a skills directory)

If a skill name was provided as an argument, use that directly.

If multiple skills were detected, ask which to analyze. If none were detected and no argument was given, list the available skills and ask.

### 2. Analyze the Conversation

Scan the conversation for these signals:

| Signal | Confidence | What to look for |
|--------|-----------|-------------------|
| **Corrections** | HIGH | User said "no", "not like that", "I meant..."; explicitly corrected output; asked for changes immediately after generation |
| **Successes** | MEDIUM | User said "perfect", "great", "exactly"; accepted output without modification; built on top of the output |
| **Edge Cases** | MEDIUM | Questions the skill didn't anticipate; scenarios requiring workarounds; features not covered |
| **Preferences** | LOW | Repeated patterns in user choices; implicit style/tool preferences |

### 3. Propose Changes

If no actionable signals are found, report that the skill performed well and end:

> No improvements identified for [skill-name]. The skill performed well in this session.

Otherwise, present findings:

```
+-- Skill Reflection: [skill-name] ----------------------------+
|                                                               |
|  Signals: X corrections, Y successes, Z edge cases           |
|                                                               |
|  Proposed changes:                                            |
|                                                               |
|  [HIGH] + Section: "specific change description"              |
|  [MED]  + Section: "specific change description"              |
|  [LOW]  ~ Note: "observation for review"                      |
|                                                               |
|  Commit: "[skill]: [summary of changes]"                      |
|                                                               |
+---------------------------------------------------------------+

Apply these changes? [Y/n] or describe tweaks
```

### 4. Apply (if approved)

1. Locate the skill's source file in its skills repository and read it.
2. Apply the changes with the Edit tool.
3. Commit and push the change to that repository.
4. Confirm: "Skill updated and pushed."

If declined, acknowledge and end.

## Example

User runs `/reflect` after a frontend-design session where they corrected gradient usage and dark background colors:

```
+-- Skill Reflection: frontend-design -------------------------+
|                                                               |
|  Signals: 2 corrections, 3 successes                         |
|                                                               |
|  Proposed changes:                                            |
|                                                               |
|  [HIGH] + Constraints: "Never use gradients unless            |
|           explicitly requested"                               |
|  [HIGH] + Color & Theme: "Dark backgrounds: use #000,         |
|           not #1a1a1a"                                        |
|  [MED]  + Layout: "Prefer CSS Grid for card layouts"          |
|                                                               |
|  Commit: "frontend-design: no gradients, #000 dark bg"        |
|                                                               |
+---------------------------------------------------------------+

Apply these changes? [Y/n] or describe tweaks
```

## Constraints

- Always show the exact proposed changes before applying.
- Never modify skills without explicit user approval.
- Push only after a successful commit.
- Edit the source file in its repository, never the generated copy in your agent's skills directory (it is overwritten on the next regeneration).
