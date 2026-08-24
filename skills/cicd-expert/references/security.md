# CI/CD security: supply chain, hardening, and verifying a control holds

GitHub Actions examples; the trust-boundary reasoning ports to any forge. When a control here is a repository or account setting rather than a file, say so and give the command, because a file review alone will never surface it.

## Supply-chain threats and the control that stops each

### Pin every third-party action to a full commit SHA

A tag is mutable. In March 2025 the `tj-actions/changed-files` action (CVE-2025-30066) and several `reviewdog` actions (CVE-2025-30154) had their tags re-pointed at a malicious commit that dumped CI secrets into the build log; over 23,000 repositories were exposed in the window. A version tag or `@latest` would have followed the attacker's commit. A full 40-hex SHA cannot be moved, so a pinned reference keeps building the reviewed commit even after a tag is hijacked.

```yaml
# pinned: survives a tag-repoint
- uses: actions/checkout@08c6903cd8c0fde910a37f88322edcfb5dd907a8  # v5
# NOT this: a moved tag silently ships attacker code
- uses: actions/checkout@v5
```

- Pin actions you author too, or reference them by a SHA the ruleset protects.
- Turn on the platform enforcement so a future unpinned action is rejected mechanically, not just by review. On GitHub the flag is `sha_pinning_required` on `PUT /repos/OWNER/REPO/actions/permissions`. That PUT **replaces the whole Actions policy**, so hardcoding `allowed_actions=all` would silently widen a repository that was restricted to `local_only` or `selected`. Read the current policy and pass it back, changing only the pin flag:

  ```bash
  cur=$(gh api repos/OWNER/REPO/actions/permissions)
  gh api -X PUT repos/OWNER/REPO/actions/permissions \
    -F enabled="$(jq -r .enabled <<<"$cur")" \
    --raw-field allowed_actions="$(jq -r .allowed_actions <<<"$cur")" \
    -F sha_pinning_required=true
  ```
- Gate it in CI as well with `zizmor` (its `unpinned-uses` audit) and `actionlint`, both pinned. Belt and braces: the repo setting covers actions the linter's file scope might miss, and the linter covers a bypass of the setting.
- Renovate keeps the SHA current: `helpers:pinGitHubActionDigests` in the config pins and bumps action digests with a readable version comment.

### Do not share a cache across the fork/release trust boundary (cache poisoning)

A forge does not segregate its build cache by trust level. A low-privilege pull-request job can write a cache entry that a later privileged job (a release, a publish) restores and executes. This is how the Ultralytics compromise and the May 2026 TanStack npm poisoning (CVE-2026-45321) pivoted from an unprivileged PR into the publishing pipeline.

- A privileged job (release, publish, sign) must not restore a cache that a fork-writable job can populate. Give release jobs their own cache namespace, or no shared cache at all.
- Prefer a **registry** cache (`type=registry`, written only by the trusted release job under `packages: write`) over the shared **Actions** cache (`type=gha`) for anything a publish path consumes. A PR validation build should read a cache, never write the one a release reads (`cache-from` without `cache-to`).
- The shared Actions cache has a per-repo quota (10 GB by default on GitHub, configurable and billable above the free tier). When usage reaches the configured limit, entries evict, so several large build caches evict each other and the package-manager caches, and a poorly keyed cache becomes a correctness and a speed problem at once (see `references/speed.md`).

### Never run unreviewed fork code with secrets (pull_request_target)

`pull_request_target` runs with access to the base repository's secrets and a `GITHUB_TOKEN` whose scope depends on the repository and workflow settings (write access unless narrowed by a `permissions:` block or the read-only default), in the context of the base branch. Checking out and running the fork's head in that context can hand an attacker your secrets and, if the token is writable, write scope (a "pwn request"). Prefer the plain `pull_request` trigger, which gives a fork PR a read-only token and no secrets. If you genuinely need base context, never check out or execute the PR head in the same job that holds the secrets. Keep the PR gate free of `secrets:`, `environment:`, and `pull_request_target` so it runs clean on an untrusted fork.

### Never interpolate untrusted input into a run body (template injection)

A `${{ github.event.pull_request.title }}`, `.body`, `.head_ref`, or a `workflow_dispatch` input expanded directly inside `run:` is shell, so a crafted value becomes command execution. Pass the value through `env:` and reference the shell variable instead.

```yaml
# unsafe: the title is spliced into the shell
- run: echo "PR is ${{ github.event.pull_request.title }}"
# safe: the value arrives as data in an env var
- env:
    TITLE: ${{ github.event.pull_request.title }}
  run: echo "PR is $TITLE"
```

Apply the same `env:` indirection to any `${{ }}` in a `run:` body, even a value you believe is validated; it costs nothing and future-proofs the step. `zizmor`'s `template-injection` audit flags these.

## Account and repository hardening checklist

These are settings, not files. Verify each with the API, not by reading the repo.

- **Default token read-only.** `gh api repos/OWNER/REPO/actions/permissions/workflow` should show `default_workflow_permissions: read` and `can_approve_pull_request_reviews: false`. Widen per job with a `permissions:` block only where needed.
- **Fork-PR approval.** On a public, forkable repo require approval before any fork PR runs CI: `gh api -X PUT repos/OWNER/REPO/actions/permissions/fork-pr-contributor-approval -f approval_policy=all_external_contributors`. The default only gates first-time contributors.
- **Branch ruleset on the default branch.** Require a pull request, block force-push and deletion, and require the status checks that must pass. Mark a stable aggregator check required (one that always reports, green when its heavy job succeeded or was legitimately skipped) rather than a path-filtered job that is absent on some PRs and would wedge them Pending forever.
- **Tag ruleset for releases.** Restrict who can create `v*` tags so the tag push itself is the release authorization.
- **SHA pinning enforced** (above).

## Secret handling

Fork PRs cannot read base-repo secrets. Skip a step that needs one instead of failing. The `secrets` context is **not** available in an `if:` expression (job or step level), and a step's `if:` cannot read an `env` value set in that same step, so map the presence check to a **job-level** env flag and gate on it:

```yaml
jobs:
  integration:
    runs-on: ubuntu-latest
    env:
      HAS_API_KEY: ${{ secrets.API_KEY != '' }}   # secrets are unusable in if:; a job-level env flag is
    steps:
      - if: env.HAS_API_KEY == 'true'
        run: npm run test:integration
        env:
          API_KEY: ${{ secrets.API_KEY }}
```

Document every required secret (name, purpose, how to create it) and show the `gh secret set NAME` command as guidance; do not run it.

## Verify a control actually holds

A security review fails when the instrument returns a reassuring answer for the wrong reason. Run the real check and read its output.

- **Registry visibility is not a raw curl.** A tokenless `GET https://ghcr.io/v2/OWNER/PKG/manifests/TAG` returns 401 for a public package too, because the registry demands the anonymous-token handshake first. 401 is not proof of "private". Check visibility with the API (`gh api /users/OWNER/packages/container/PKG --jq .visibility` for a user-owned package, or `/orgs/OWNER/packages/container/PKG` for an organization-owned one) or complete the token dance (`GET /token?scope=repository:OWNER/PKG:pull`, then use the token); a public package then answers 200, or 404 for an absent tag, never 401.
- **A comment that starts with the word `shellcheck` becomes a directive.** In a workflow `run:` block that `actionlint` feeds to shellcheck, a prose comment line beginning `# shellcheck ...` is parsed as a shellcheck directive and fails to parse (SC1072 or SC1073), reddening the lint over nothing. Do not start a comment line with `shellcheck`; put a word before it.
- **Trust a linter's output, not its silence.** A skipped or never-installed linter prints a green nothing indistinguishable from a clean pass. Give every install a positive control (run `TOOL --version` after installing) and, in CI, fail closed when a required tool is absent rather than skipping.
- **Grep the literal.** A grep for a string containing regex metacharacters (`^`, `.`, `{`, `---`) is read as a pattern and can return a false zero. Use `-F` for a literal, and confirm a restore with the version control status, not a grep count.
