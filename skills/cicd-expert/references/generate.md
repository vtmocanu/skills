# GENERATE mode: build a pipeline through interactive conversation

Vendored and adapted from [vfarcic/dot-ai](https://github.com/vfarcic/dot-ai) `shared-prompts/generate-cicd.md` (MIT, Copyright (c) 2025 Viktor Farcic).

Generate CI/CD workflows for a repo that has none, or add a workflow. CI/CD involves **policy decisions** (PR versus direct push, release triggers, deployment strategy) that cannot be deduced from code alone; they reflect team and org preferences. So this is interactive, not template-driven.

**Execute the phases sequentially. Ask about one phase at a time and wait for the answer before proceeding; do not batch every question upfront.** Verify everything against the actual codebase before adding a step, secret, or setting. Never assume; ask when uncertain.

```text
PHASE 1: ANALYZE      discover what CAN be built, tested, deployed
        v
PHASE 2: PRESENT+ASK  show findings, then present the policy choices
        v
PHASE 3: GENERATE     create workflows from the confirmed choices
```

## Step 0: platform gate (blocking)

Detect the forge from `origin` first (see the host-only, credential-safe command in `SKILL.md`), then ask which CI/CD platform the user runs, and nothing else. Present only: (1) GitHub Actions, (2) Other.

- **GitHub Actions**: only proceed to Step 1 if `origin` is a GitHub remote. If the user picks GitHub Actions but `origin` is GitLab or Forgejo/Gitea, stop and say so: generating a GitHub workflow for a non-GitHub repo would not run. Confirm the intended forge, and route to the matching tooling (`glab`, `tea`) rather than generating a mismatched workflow.
- **Other**: stop. Ask which platform, then offer to open a feature request at `https://github.com/vtmocanu/skills/issues` so it can be prioritized. Do not analyze the repo for an unsupported platform.

## Step 1: analyze the whole repo

The entire repository is context. Read, do not skim.

1. **Language and framework**: from source files and dependency manifests; note version requirements.
2. **Existing automation**: find every script and build target and **read them to understand how they are called** (arguments, fixtures, env, cleanup). If automation exists for a task, use it; only generate raw commands when none exists; when several options exist, ask.
3. **Existing CI**: analyze what is already configured and why. In Step 2 ask whether to update it or add new workflows.
4. **Container and registry**: check for a Dockerfile and container config; search existing CI, automation, and docs for the registry. If there is no Dockerfile but the project would benefit, suggest the `generate-dockerfile` skill.
5. **Branching and release strategy**: infer from existing CI triggers, git tags, and docs.
6. **Environment and secrets**: find env documentation and examples; search the code for required variables; identify what secrets the workflow needs.
7. **App definition**: Helm chart, Kustomize, plain manifests, or container-only.
8. **Deployment mechanism**: GitOps (ArgoCD, Flux), direct (Helm, kubectl), manual, or external. For GitOps, CI must NOT deploy directly; it updates manifests and the controller syncs. Find where the image tag lives for the bump, and whether the GitOps resources exist or must be created.
9. **Tool manager**: DevBox, mise, asdf. If present, use it; otherwise ask in Step 3.

## Step 2: present findings, get confirmation

Summarize only what is relevant to this repo (language, build and test commands with their source, existing CI, app definition, deployment mechanism). Ask the user to confirm or correct before generating.

## Step 3: present the policy choices

These require user input; present only the ones that apply (do not ask about a registry when there is no Dockerfile, or deployment when it is a library):

- **PR workflow**: what runs on pull requests.
- **Release trigger**: what starts a release build.
- **Release validation**: does the release re-run checks the PR already passed (re-run all is safest and slowest, skip is fastest, security-scans-only is the compromise).
- **Container registry**: where images push, if containerized.
- **Environment setup**: native runner tooling or DevBox.
- **Deployment strategy**: GitOps, direct, or manual, if deployed.

Ask targeted clarifiers when a strategy is unclear (feature branches with PRs versus push to main, which of several test commands is primary, where the GitOps manifests live).

## Step 4: generate

Write the workflow(s) from the analysis and the confirmed choices. Apply every principle in `SKILL.md` and `references/security.md`: pin actions to SHAs, minimal `permissions:`, call project automation, add `concurrency`, fail fast. Add **path filters only after** confirming the repo has real component boundaries (Step 1) and that every shared input (fixtures, root config, the workflow file itself) triggers all the jobs that read it; a single-component repo, or incomplete shared-file coverage, means a filter would skip a check that should run, so leave it unfiltered. If a filtered job is a required status check, add a stable aggregator job that always reports and require that instead (see `references/speed.md`).

## Step 5: validate before presenting

1. Valid YAML and valid workflow syntax (run the forge's linter, e.g. `actionlint`, not just an eyeball).
2. Every referenced automation target actually exists.
3. Required secrets listed clearly.
4. `permissions:` is minimal.
5. Deployment steps match the selected mechanism.

## Step 6: present to the user

Provide the workflow file(s) with explanatory comments, a summary of what was detected and decided, the required secrets with setup guidance, and the repository settings the workflow needs (permissions, environments, protected branches). Tell the user what to configure upfront rather than letting the first run fail. Show `gh secret set` (or the forge equivalent) as guidance; do not execute it.

## Step 7: commit, then execute with separate approval

Approval to edit is not approval to execute. After the user approves the generated files, commit them following the repo's established process. Before triggering anything, separate the two cases:

- A validation-only workflow (lint, test, build with no push) can be triggered and watched directly.
- A workflow that publishes artifacts, pushes an image, cuts a release, or changes an environment is side-effecting. Ask for explicit confirmation before the first trigger, and prefer a non-deploy validation path first (for example run the PR checks, or a `workflow_dispatch` with deploy steps gated off), so the first run cannot deploy on the strength of the edit approval alone.

Then trigger, watch the runs, and fix failures until they pass.
