---
name: done
description: Wraps up a working session. Checks git state in every directory the session touched, removes git artifacts the session created once they are safely unneeded, reviews unfinished work, and gives a plain verdict on whether the session can close. Use when the user invokes /done or asks whether the session is done or can be closed.
---

# Done

## Document Location

This document is located at: `~/stuff/gitrepos/gh/vtmocanu/skills/skills/done/SKILL.md` (public repo: github.com/vtmocanu/skills)

> **Note**: This is the source of truth. The installed copy at `~/.claude/skills/done/SKILL.md` is derived from this file by the `npx skills` package manager; edit here, then run `npx skills update` to re-pull it. Never edit the installed copy.

Are we done here? Can we close this session? Decide and tell me, after checking:

- **Git**: run `git status` in the current repo and any other working directory we touched this session. Report uncommitted, unstaged, untracked, and unpushed changes (where an upstream exists, `git log --oneline @{u}..`). If everything is committed and pushed, say so in one line.
- **Cleanup, without asking**: remove git artifacts this session created that are no longer needed (worktrees, local branches, temporary refs, throwaway clones). Remove one only when all hold; otherwise leave it and report it:
  - this session created it, still owns it, and nothing uses it: not handed to another session, not backing a running check or review;
  - nothing in it is needed, ignored files included;
  - its current tip is preserved: it equals a merged PR's final head, or every commit is on the remote according to freshly fetched state (stale or deleted tracking refs do not count). A squash merge counts only by the final-head match, never by ancestry. A tools or review tree counts as never holding work only while it still sits at the commit it was created on.

  Never force a removal past git's own safety checks, except deleting a branch whose work the preservation test above has proven. Never touch the `main` worktree, stashes, remote branches, or anything another session or the user created. List what you removed.
- **Outstanding work**: review this session for anything unfinished: partial edits, failed or skipped steps, open TODOs, loose ends.

Then give me a plain verdict: can we close the session, or is there something left to do? Apart from that cleanup, do not commit, push, or change anything; report only.
