# Refresh safety

Read this when modifying `scripts/refresh_skills.py` or diagnosing intermittent
skill-loading warnings. Vercel's `skills` CLI remains responsible for fetching,
discovery, and installation into staging. The local wrapper coordinates calls
and publishes their output; it does not patch Vercel's package.

## Verified failure and correction

On 2026-09-08, source inspection of `skills` 1.5.23 and 1.5.24 found that
`cleanAndCreateDirectory` recursively deletes an installed skill before copying
the replacement, including an unchanged `add`. A concurrent reader during ten
isolated 1.5.24 reinstalls observed 114 missing-file reads and eight partial reads.
All files were intact afterward. This explains intermittent `Skipped loading ...
invalid SKILL.md` and `No such file or directory` warnings with changing names.

The old wrapper's `flock` serialized installers, but agent readers never acquired
that lock. Its documentation described only an older inventory at startup;
missing and partial files were also possible. Making a single hook synchronous
would still leave readers in other running sessions exposed.

## Publication contract

- All Vercel invocations are project-scoped in temporary directories. Neither a
  global install nor a live project update is launched. The subprocess inherits
  credentials and the package cache; its selection-state lock uses a temporary
  `XDG_STATE_HOME`. The user's home directory is not redirected.
- Each staged batch is validated before publication. Installer errors, empty or
  unsupported lock metadata, missing entrypoints, and file/directory conflicts
  leave that batch unpublished. Unrelated successful sources can still publish.
- Existing files use a sibling temporary file and `os.replace`, so readers see
  complete old or new bytes. Modes are preserved, and unchanged files are not
  rewritten. Supporting files publish before `SKILL.md`.
- A new skill is assembled outside the discovery root and renamed into place
  only when complete. Existing skill directories are never removed.
- Existing Claude copies and directory-wide aliases are preserved. Missing
  Claude projections are symlinked to the canonical store. Supporting symlinks
  and shape conflicts in live skills are rejected instead of being followed.
- Publication is atomic per file, not a whole-skill transaction or snapshot.
  Different reads may see different versions. An I/O failure during publication
  can leave a mix of complete files; the wrapper reports failure. It does not
  claim whole-batch rollback after publication starts.
- Retired skills and supporting files are retained, since an already-loaded
  body can reference them. To prune, close consumers of that store, then use
  Vercel's direct `add` to clean a skill's contents or `remove` to remove a skill.
  Automatic refresh does not garbage-collect retired files.
- Direct Vercel commands do not take the wrapper lock or use its publisher. Run
  modifying commands with consumers closed, including the one-time installation
  of this fix when upgrading from the old live-writing wrapper.

## Sources, scopes, and metadata

Configured catalogs install `--skill '*'`. Other global entries and current
project dependencies are grouped by source and ref and installed by recorded
name with `--full-depth`. Required-source failures make the hook fail; explicitly
optional catalog failures do not. Unsupported source types or lock schemas are
reported and preserved, not guessed. The supported lock source types are
`github`, `git`, `gitlab`, and `local`; registry/provider-specific formats need
an explicit adapter before they can be refreshed by this wrapper.

The output targets are the canonical `.agents/skills` store and Claude's skill
directory. OpenCode reads the canonical store. The wrapper does not fan out to
other detected agents' private directories as a direct `skills update` can.
It honors `CLAUDE_CONFIG_DIR` and `XDG_STATE_HOME` for the live Claude directory
and global lock, respectively. Current-project dependencies are taken from the
launch directory's `skills-lock.json`; authored skills absent from it are not
updated. Do not use global refresh to overwrite an untracked authored skill.

Project lock version 1 and global lock version 3 are validated explicitly. The
wrapper preserves unrelated global state and original installation timestamps,
then publishes lock metadata after the files. Vercel's staged `computedHash`
becomes global `skillFolderHash`, while source URLs, paths, and refs are retained.
For unchanged files, an existing global hash is preserved.

GitHub global installs normally use a Git tree SHA; project installs use a
SHA-256 content hash. After changed staged content publishes, a direct global
Vercel check may offer one redundant update to restore its preferred Git SHA.
This is a conservative mismatch, never a false claim that changed content is
current. Project metadata keeps Vercel's original hash. Local project source
paths are rebased out of staging before its temporary directory is removed.
Staging uses its physical path so macOS `/var` and `/private/var` aliases cannot
corrupt relative source paths (covered by the local-dependency regression).

## Validation

Run `python3 skills/skill-maker/scripts/test_refresh_skills.py` from the source
repo. The suite covers installer isolation, reads during deletion and copying,
atomic publication, failures, aliases, modes, no-op writes, metadata, authored
skills, and two refresh processes contending for the same lock.

Also exercise the real cached or rolling CLI in a temporary project before
changing its invocation contract. Keep a reader active against both canonical
and Claude paths through several changed installs. Verify no missing or partial
reads, complete final files, correct metadata, and no changes to the live stores.
