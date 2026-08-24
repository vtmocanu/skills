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

Ask which CI/CD platform the user runs, and nothing else, first. Present only: (1) GitHub Actions, (2) Other.

- **GitHub Actions**: proceed to Step 1.
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

Write the workflow(s) from the analysis and the confirmed choices. Apply every principle in `SKILL.md` and `references/security.md`: pin actions to SHAs, minimal `permissions:`, call project automation, path-filter and add `concurrency`, fail fast.

## Step 5: validate before presenting

1. Valid YAML and valid workflow syntax (run the forge's linter, e.g. `actionlint`, not just an eyeball).
2. Every referenced automation target actually exists.
3. Required secrets listed clearly.
4. `permissions:` is minimal.
5. Deployment steps match the selected mechanism.

## Step 6: present to the user

Provide the workflow file(s) with explanatory comments, a summary of what was detected and decided, the required secrets with setup guidance, and the repository settings the workflow needs (permissions, environments, protected branches). Tell the user what to configure upfront rather than letting the first run fail. Show `gh secret set` (or the forge equivalent) as guidance; do not execute it.

## Step 7: commit and iterate

After the user approves, commit following the repo's established process, trigger the workflows, watch the runs, and fix failures until they pass.
