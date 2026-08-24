# CI/CD speed: cache, filter, parallelize, pin

GitHub Actions examples; the levers port to any forge. Optimize for wall-clock on the critical path first, and know which optimizations are worth it for this repo before spending complexity on them.

## Know what is actually slow before optimizing

Measure the critical path, do not guess. On GitHub: `gh run list --limit 20`, then `gh run view RUN_ID` and the job durations. Optimize the pole (the longest job the whole run waits on), not a job that already runs in parallel and finishes early.

**Free minutes change the math.** Public repositories get free standard-runner minutes on GitHub, so an optimization whose only payoff is fewer runner-minutes (for example path-filtering parallel jobs that already fit in the wall-clock of a slower sibling) buys almost nothing there, while adding real risk. On such a repo, spend effort on wall-clock (the critical path) and on correctness, not on minute-shaving.

## Install pinned prebuilt binaries, do not compile a tool on every run

`go run some/tool@v1.2.3` and `go install some/tool@v1.2.3` compile the tool from source on a cold build cache, tens of seconds every run, because CI caches rarely persist the exact build objects a one-off compile produces. Fetch the prebuilt release binary and verify its checksum instead; it skips the compile and is byte-for-byte the pinned artifact.

```yaml
- name: Install the tool (pinned, sha256-verified)
  run: |
    set -euo pipefail
    VERSION="1.2.3"
    SHA256="....."                     # from the release, next to the version, never a variable
    curl -fsSLo /tmp/t.tar.gz --retry 5 --retry-all-errors \
      "https://github.com/OWNER/TOOL/releases/download/v${VERSION}/TOOL_${VERSION}_linux_amd64.tar.gz"
    echo "${SHA256}  /tmp/t.tar.gz" | sha256sum -c -
    mkdir -p "${HOME}/.local/bin" && tar -xzf /tmp/t.tar.gz -C "${HOME}/.local/bin" TOOL
    echo "${HOME}/.local/bin" >> "${GITHUB_PATH}"
    "${HOME}/.local/bin/TOOL" --version     # positive control
```

- Keep the version and its checksum inlined together; bumping the version means editing the checksum in the same change. A checksum in a variable can be displaced by a manual edit and stops being a control.
- Some tools ship no prebuilt release binary (for example `golang.org/x/tools/cmd/deadcode`, `golang.org/x/vuln/cmd/govulncheck`). There is nothing to pin; the only lever is a warm build cache for that job (below).
- For tools a team pins across many repos, provision through a reproducible manager (`devbox.json` with a committed `devbox.lock`, or a shared base image) so the version lives in one tracked file and Renovate can bump it. Any repo shipping a `devbox.json` needs an auto-commit wrapper for `devbox.lock`, because Renovate bumps the package version but does not regenerate the lock.

## Path-filter jobs to what changed

A change to one component should not rebuild the others. Gate each component's jobs on a paths filter (`dorny/paths-filter` or the trigger's `paths:`). Two cautions:

- A shared input (a fixtures directory, a root config) that several components read must trigger all of them, or a change to it silently skips the gate that consumes it.
- If a filtered job is a required status check, it will be absent (not failed) on a PR that does not touch its paths, wedging the PR Pending. Require a stable aggregator job that always reports instead, and mark that required.

## Order the DAG: cheap gates expensive

Put fast checks (lint, format, typecheck) ahead of slow ones (build, integration tests) with `needs:`, so a trivial failure stops the run before the expensive job starts. Do not gate a fast job behind a slow one it does not depend on. Run independent jobs in parallel; a flat set of gate jobs with no `needs:` between them is correct when they are truly independent.

## Cancel superseded runs

```yaml
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
```

Add this to the PR and branch gate so a new push cancels the in-flight run on the same ref. Set `cancel-in-progress: false` for release, publish, and deploy workflows; never cancel one of those mid-push.

## Cache, but mind the shared budget

Key the cache to what invalidates it, and do not reuse one key for two kinds of data. **Package-manager data** (downloaded dependencies) is keyed on the lock-file hash; the `setup-*` actions do exactly this with `cache:`, and it covers nothing else. **Build output** (compiled objects, a build cache) is invalidated by more than the lock file, so its key must also include the source files, the build configuration, and the toolchain version; keying build output on the lock file alone serves stale artifacts. Two traps:

- **Budget and eviction.** The Actions cache has a per-repo quota (10 GB by default on GitHub, configurable and billable above it); entries evict once usage reaches the configured limit. Several large build caches evict each other and the package-manager caches, so adding a per-job build cache can make other jobs slower. A dedicated per-job cache is only worth it when it reliably hits; watch the hit rate across several runs (`gh cache list`) before trusting it, and prefer a registry cache for the largest artifacts (a container base image) so it does not count against the cap.
- **A mis-keyed cache is invisible in one run.** It simply never hits; the job just runs cold while the cache silently uploads and evicts. It looks like a working optimization. Verify hits, do not assume them.

For a container build, reuse layers with `cache-from` (read-only in a validation build, so it never writes the cache a release consumes) and let the release job own `cache-to`.

## Let Renovate bump the CI tool versions pinned in shell

The `setup-*`, `go.mod`, `package-lock`, and Dockerfile `FROM` pins are tracked by Renovate's built-in managers. A tool version pinned inline in a shell step or a Taskfile is invisible to them; add a repo-local `customManager` plus a `# renovate:` annotation on the line above the version, so Renovate opens a bump PR.

```yaml
# in the workflow / action / script, directly above the version literal:
# renovate: datasource=github-releases depName=OWNER/TOOL
TOOL_VERSION="1.2.3"
```

```json
// renovate.json
{
  "customManagers": [
    {
      "customType": "regex",
      "managerFilePatterns": [
        "/^\\.github/workflows/[^/]+\\.ya?ml$/",
        "/^\\.github/actions/[^/]+/action\\.ya?ml$/",
        "/^scripts/[^/]+\\.sh$/",
        "/(^|/)Taskfile\\.ya?ml$/"
      ],
      "matchStrings": [
        "#\\s*renovate:\\s*datasource=(?<datasource>\\S+)\\s+depName=(?<depName>\\S+)(?:\\s+versioning=(?<versioning>\\S+))?[^\\n]*\\n[^\\n]*?(?<currentValue>\\d+\\.\\d+\\.\\d+[0-9A-Za-z._-]*)"
      ],
      "datasourceTemplate": "{{{datasource}}}",
      "depNameTemplate": "{{{depName}}}",
      "versioningTemplate": "{{#if versioning}}{{{versioning}}}{{else}}semver{{/if}}",
      "extractVersionTemplate": "{{#if extractVersion}}{{{extractVersion}}}{{else}}^v?(?<version>.+)${{/if}}"
    }
  ]
}
```

- Renovate bumps the version but cannot recompute a file checksum. For a checksum-pinned tool the bump PR reddens its `sha256sum -c` step, which is the signal to update the sha in that same PR. A tool with no checksum (a PyPI or npm install) bumps fully automatically.
- Renovate uses the RE2 engine: no lookahead or backreferences, and `^`/`$` match file boundaries, not line boundaries.
- A single-occurrence version literal (one shell variable) auto-replaces cleanly; a version repeated several times in one file (a URL plus a tar path plus an assert) needs a single source variable first, or the bump is partial.
- Validate the config and confirm it matches before trusting it: `npx --yes --package renovate renovate-config-validator renovate.json`, then a local `renovate --platform=local --dry-run=extract` and check the tool appears in the extracted dependencies.
