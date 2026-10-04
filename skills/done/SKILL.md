---
name: done
description: End-of-session wrap-up prompt. When invoked, asks Claude to determine whether the session is finished and safe to close by checking git status for uncommitted, unstaged, untracked, and unpushed changes across the working directories touched this session, removing git artifacts this session created once they are safely no longer needed (worktrees, local branches, temporary refs, throwaway clones), reviewing the session for unfinished tasks or loose ends, then reporting a plain verdict on whether the session can be closed or something is still outstanding. Invoke explicitly with /done when wrapping up a working session.
---

# Done

## Document Location

This document is located at: `~/stuff/gitrepos/gh/vtmocanu/skills/skills/done/SKILL.md` (public repo: github.com/vtmocanu/skills)

> **Note**: This is the source of truth. The installed copy at `~/.claude/skills/done/SKILL.md` is derived from this file by the `npx skills` package manager; edit here, then run `npx skills update` to re-pull it. Never edit the installed copy.

Are we done here? Can we close this session? Decide and tell me, after checking:

- **Git**: run `git status` in the current repo and any other working directory we touched this session. Report uncommitted, unstaged, untracked, and unpushed changes (where an upstream exists, `git log --oneline @{u}..`). If everything is committed and pushed, say so in one line.
- **Cleanup, without asking**: remove git artifacts this session created that are no longer needed: worktrees, local branches, temporary refs (e.g. `refs/reviews/*`), throwaway clones under `/tmp`. Remove one only when all hold, else leave it and report it:
  - this session created it, still owns it, and nothing uses it: not handed to another session, not backing a running check, review or borrowed tree;
  - it is clean: `git status --porcelain` is empty, and ignored files (`git status --ignored --porcelain`) hold nothing needed;
  - its current tip is preserved: equal to a merged PR's final head, or every commit is on the remote per a fresh `git fetch` (not a stale tracking ref). A squash merge counts only through the first test, never through ancestry. A tools or review tree counts as never holding work only while its HEAD is still the commit it was created at.

  Commands: `git worktree remove` without `--force`; `git branch -d`, using `-D` only after the preservation proof above (e.g. a squash-merged PR); `git update-ref -d` for a temporary ref. Never touch the `main` worktree, stashes, remote branches, or anything another session or the user created. List what you removed.
- **Outstanding work**: review this session for anything unfinished: partial edits, failed or skipped steps, open TODOs, loose ends.

Then give me a plain verdict: can we close the session, or is there something left to do? Apart from that cleanup, do not commit, push, or change anything; report only.
