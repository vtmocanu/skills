---
name: git-worktrees
description: Create and clean up git worktrees for isolated, short-lived feature or PRD work. Defaults to a normal worktree (regular clone) via `git worktree add` or an optional `git new-wt` alias; also documents the advanced bare-clone-with-child-worktrees layout for keeping several branches checked out in parallel. Use when starting or finishing isolated work, deciding branch vs worktree, removing a worktree, or (advanced) setting up a bare-clone layout. Triggers include "worktree", "git worktree add", "git new-wt", "branch or worktree", "bare clone", ".bare directory".
---

# Git Worktrees

A worktree gives a branch its own working directory, so you can develop a feature without stashing or switching the main checkout. Treat them as **short-lived**: create one for a task, work in it, merge, remove it.

## Default: a normal worktree

This is the recommended path for almost everything. It works in any regular clone (`.git` is a directory) with no special setup.

### Branch or worktree?

- **Branch** — quick, in-place work you will finish before touching anything else. `git checkout -b feature/my-thing main`.
- **Worktree** — isolate the work in its own directory so the main checkout stays clean and usable in parallel. Prefer this when the work is non-trivial or you may need to jump back to `main`.

### Create one

Raw git (portable, no setup):

```bash
git worktree add -b feature/my-thing ../my-thing main
```

That creates branch `feature/my-thing` off `main` and puts its working directory at `../my-thing` (a sibling of the current repo). Then, since a tool cannot `cd` for you, print the command to start working:

```
Worktree created. Run this to start working:
  cd ../my-thing && claude
```

### Optional convenience: the `git new-wt` alias

If you make worktrees often, wrap the above in an alias. Add to your `~/.gitconfig` under `[alias]`:

```gitconfig
[alias]
  new-wt = "!f() { \
    set -e; \
    name=\"$1\"; branch=\"${2:-feature/$name}\"; base=\"${3:-main}\"; \
    if [ -z \"$name\" ]; then echo 'usage: git new-wt <dir-name> [branch] [base-branch]' >&2; return 1; fi; \
    git worktree add -b \"$branch\" \"../$name\" \"$base\"; \
    echo; echo 'Worktree created. Run this to start working:'; echo \"  cd ../$name && claude\"; \
  }; f"
```

Then: `git new-wt my-thing` (branch defaults to `feature/my-thing`, base to `main`). Args: `git new-wt <dir-name> [branch] [base-branch]`.

### Remove it when done

After merging, remove the worktree (run from another worktree, e.g. the main one):

```bash
git worktree remove ../my-thing
```

If you were on a plain branch instead of a worktree, just `git checkout main` after merging.

## Advanced (optional): bare-clone with child worktrees

> Use this **only if** you deliberately want several branches checked out **in parallel, long-term** (e.g. keep `main` always available while a long-lived feature builds beside it). It is more to set up and maintain than a normal clone, so for the short-lived worktrees above it is not worth it. The normal layout is the default; reach for this only when you specifically ask for it.

In this layout the repo dir holds a bare repo plus one child directory per checked-out branch:

```
~/repos/my-repo/
├── .bare/                    ← the bare repo (config, objects, refs, worktree registry)
├── .git                      ← pointer FILE: `gitdir: ./.bare`
├── main/                     ← worktree for branch `main`
├── dev/                      ← worktree for branch `dev`
└── my-thing/                 ← added for feature work
```

Key properties / gotchas:

- The root `.git` is a **file** (`gitdir: ./.bare`), not a directory. Run git from a child worktree, never the repo root (`git status` at the root fails with "must be run in a work tree" — expected).
- The bare repo's config MUST set `fetch = +refs/heads/*:refs/remotes/origin/*`. A raw `git clone --bare` does NOT, so remote branches stay invisible to `git branch -a` until fixed. Most common gotcha.
- The bare repo's config MUST also set `core.bare = false`, or every worktree inherits `is-bare=true` and `git submodule` breaks there (`fatal: git-submodule cannot be used without a working tree`) even with a valid working tree.
- Worktrees are **children** of the repo dir, not siblings. Child dir names stay flat (`my-thing`) even when the branch has slashes (`feature/my-thing`).
- Tools that scan for repos must treat the whole thing as **one** repo; the canonical origin is `{repo}/.bare/config`, never a worktree's `.git` pointer.

### Set it up with the `git clone-wt` alias

Add to `~/.gitconfig` under `[alias]` (encodes the whole recipe: bare clone, pointer file, refspec + `core.bare` fixes, fetch, prune stray refs, add the default-branch worktree):

```gitconfig
[alias]
  clone-wt = "!f() { \
    set -e; \
    url=\"$1\"; dir=\"${2:-$(basename \"$url\" .git)}\"; \
    if [ -e \"$dir\" ]; then echo \"error: $dir already exists\" >&2; return 1; fi; \
    mkdir -p \"$dir\"; cd \"$dir\"; \
    git clone --bare \"$url\" .bare; \
    printf 'gitdir: ./.bare\\n' > .git; \
    git --git-dir=.bare config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'; \
    git --git-dir=.bare config core.repositoryformatversion 1; \
    git --git-dir=.bare config extensions.worktreeConfig true; \
    git --git-dir=.bare config core.bare false; \
    git --git-dir=.bare fetch origin; \
    default_branch=$(git --git-dir=.bare symbolic-ref --short HEAD); \
    for ref in $(git --git-dir=.bare for-each-ref --format='%(refname:short)' refs/heads/); do \
      [ \"$ref\" != \"$default_branch\" ] && git --git-dir=.bare branch -D \"$ref\" >/dev/null; \
    done; \
    git --git-dir=.bare worktree add \"$default_branch\" \"$default_branch\"; \
    echo; echo \"Layout ready at: $(pwd)\"; echo \"Start working:  cd $dir/$default_branch\"; \
  }; f"
```

Usage: `git clone-wt git@github.com:owner/my-repo.git [dir-name]`. Add more worktrees later with `git new-wt` (it lands at `../<name>` relative to the current worktree, which is correct in this layout too).
