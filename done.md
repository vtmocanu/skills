---
name: done
description: End-of-session wrap-up prompt. When invoked, asks Claude to determine whether the session is finished and safe to close: check git status for uncommitted, unstaged, untracked, and unpushed changes across the working directories touched this session, review the session for unfinished tasks or loose ends, then report a plain verdict on whether the session can be closed or something is still outstanding. Invoke explicitly with /dot-ai-done when wrapping up a working session.
---

# Done

## Document Location

This document is located at: `~/stuff/gitrepos/gh/vtmocanu/skills/done.md` (public repo: github.com/vtmocanu/skills)

> **Note**: This is the source of truth. The skill copy at `~/.claude/commands/dot-ai-done/SKILL.md` is derived from this file. All edits should be made here. After editing, use `/dot-ai-skills` to regenerate; never copy files directly to `~/.claude/commands/`.

Are we done here? Can we close this session? Decide and tell me, after checking:

- **Git**: run `git status` in the current repo and any other working directory we touched this session. Report uncommitted, unstaged, untracked, and unpushed changes (where an upstream exists, `git log --oneline @{u}..`). If everything is committed and pushed, say so in one line.
- **Outstanding work**: review this session for anything unfinished: partial edits, failed or skipped steps, open TODOs, loose ends.

Then give me a plain verdict: can we close the session, or is there something left to do? Do not commit, push, or change anything to perform this check; report only.
