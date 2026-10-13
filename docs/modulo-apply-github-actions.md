# `modulo apply` in GitHub Actions (Terraform-style CI)

A copy-paste GitHub Actions workflow that gives the [`modulo apply` CLI](./product-map/configure/apply.md)
a Terraform-style CI loop: every pull request posts a **plan** – the upsert diff
the CLI would write – as a comment on the PR, and merging to `main` runs the
**apply** against each environment. Save the YAML below as
`.github/workflows/modulo-apply.yml` in the repo that holds your apply config.

## Prerequisites

- **`MODULO_URL`** – the deployment the CLI talks to. Store it as a repository
  Variable (`vars.MODULO_URL`), or set it per GitHub Environment when staging
  and production point at different deployments. The CLI hard-requires it.
- **`MODULO_API_KEY`** – an org API key (`mk_...` with the `operator` role)
  stored as a GitHub **secret**, scoped per Environment so staging and
  production hold different keys. The CLI refuses to run without both
  `MODULO_URL` and `MODULO_API_KEY`; GitHub does not export variables or
  secrets into a job automatically, so the workflow wires them into every
  job with a job-level `env:` block (environment-scoped values win when a
  job names `environment:`).
- **GitHub Environments** – create `staging` and `production`
  (*Settings → Environments → New environment*). Repo-level secrets and
  variables live under *Settings → Secrets and variables → Actions*;
  environment-scoped ones are added on each environment's page, and when a
  job names `environment:` they override the repo-level values. The example
  binds one job per environment. If you add protection rules (e.g. required
  reviewers) to an environment, every job that names it – plan jobs included –
  pauses until the rules pass, so keep rules only where you want a hold.
- **The apply config** – the YAML file `modulo apply -f` consumes, committed to
  this repo (the example uses `modulo.yaml`).
- **CLI installation in CI** – `pip install farnalabs-modulo` (or
  `uv tool install farnalabs-modulo`), the package name this repo declares
  (`[project.scripts]` in `backend/pyproject.toml`). **To be confirmed**:
  a published release can lag this repo, so before relying on the
  workflow's install step, check the installed release actually ships the
  `apply` subcommand (`modulo apply --help`) and pin that version
  (`pip install farnalabs-modulo==<version>`). Until that is confirmed,
  install from a checkout of this repo (`uv sync` in `backend/`, then
  `uv run modulo apply ...`, as the self-hosted docs do) or from your own
  package index.

## Workflow

```yaml
name: modulo apply

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  # ---- Plan: every pull request, one job per environment -----------------
  # `--plan` computes the upsert diff without writing anything; a computed
  # plan exits 0, so a pending change never fails the job (config, auth or
  # network errors still exit non-zero and fail it loudly).
  plan-staging:
    if: >-
      github.event_name == 'pull_request' &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    environment: staging
    # One in-flight plan per PR: a new push cancels the previous plan job, so
    # the PR gets a single, current plan comment instead of a duplicate per
    # push. Only the plan jobs are concurrency-scoped - the apply jobs are
    # deliberately left alone so a mid-run apply is never cancelled.
    concurrency:
      group: modulo-plan-staging-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    env:
      MODULO_URL: ${{ vars.MODULO_URL }}
      MODULO_API_KEY: ${{ secrets.MODULO_API_KEY }}
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install modulo CLI
        # To be confirmed: verify + pin a release that ships `apply` (see Prerequisites).
        run: pip install farnalabs-modulo
      - name: Plan
        run: |
          set -o pipefail
          modulo apply --plan -f modulo.yaml | tee plan-staging.txt
      - name: Comment the plan on the PR
        env:
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: |
          {
            echo "### modulo plan (staging)"
            echo '```'
            cat plan-staging.txt
            echo '```'
          } | gh pr comment "$PR_NUMBER" --body-file -

  plan-production:
    if: >-
      github.event_name == 'pull_request' &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    environment: production
    concurrency:
      group: modulo-plan-production-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    env:
      MODULO_URL: ${{ vars.MODULO_URL }}
      MODULO_API_KEY: ${{ secrets.MODULO_API_KEY }}
    permissions:
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install modulo CLI
        # To be confirmed: verify + pin a release that ships `apply` (see Prerequisites).
        run: pip install farnalabs-modulo
      - name: Plan
        run: |
          set -o pipefail
          modulo apply --plan -f modulo.yaml | tee plan-production.txt
      - name: Comment the plan on the PR
        env:
          GH_TOKEN: ${{ github.token }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
        run: |
          {
            echo "### modulo plan (production)"
            echo '```'
            cat plan-production.txt
            echo '```'
          } | gh pr comment "$PR_NUMBER" --body-file -

  # ---- Apply: merge to main, one job per environment ---------------------
  # A real apply exits 1 if any entity was blocked or failed, so the job (and
  # the merge's checks) go red instead of silently diverging.
  apply-staging:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    environment: staging
    env:
      MODULO_URL: ${{ vars.MODULO_URL }}
      MODULO_API_KEY: ${{ secrets.MODULO_API_KEY }}
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install modulo CLI
        # To be confirmed: verify + pin a release that ships `apply` (see Prerequisites).
        run: pip install farnalabs-modulo
      - name: Apply
        run: modulo apply -f modulo.yaml

  apply-production:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    needs: apply-staging
    runs-on: ubuntu-latest
    environment: production
    env:
      MODULO_URL: ${{ vars.MODULO_URL }}
      MODULO_API_KEY: ${{ secrets.MODULO_API_KEY }}
    steps:
      - uses: actions/checkout@v7
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
      - name: Install modulo CLI
        # To be confirmed: verify + pin a release that ships `apply` (see Prerequisites).
        run: pip install farnalabs-modulo
      - name: Apply
        run: modulo apply -f modulo.yaml
```

## Approval model

**Merging to `main` IS the approval.** The apply jobs run on push to `main`,
so whoever can merge the PR has approved the change. Modulo owns no approval
step: the CLI has no confirm/approve prompt in CI, and nothing in this
workflow asks Modulo for permission to write. Any extra gate – required
reviewers, protected branches, an approval queue before production deploys –
comes from GitHub branch protection or GitHub Environments protection rules
configured in the repo, never from a Modulo-owned approval.

## Exit codes the jobs rely on

| Command | Exit code |
|---|---|
| `modulo apply --plan -f <file>` (alias `--dry-run`) | `0` once the plan is computed – a pending change never fails the job |
| `modulo apply -f <file>` (real apply) | `1` if any entity was blocked or failed, else `0` – the apply jobs go red instead of silently diverging |
| `modulo apply --diff -f <file>` (read-only drift check) | `0` when the org matches the config, `1` on drift (`created` / `updated` / `blocked`) – usable as a separate CI gate |

Config-load, auth and network errors exit `1` on every command (the CLI
fails before producing a report), so a broken config or missing
`MODULO_API_KEY` fails the job loudly rather than posting an empty plan.

## Vault credentials (`credential_ref`)

CI is exactly where a committed apply config must never contain a credential
value. A model backend's credential is therefore declared as a **reference**,
never a literal, in one of three forms:

- `api_key: ${env:VAR}` – resolved client-side from the environment. Fine for
  a value the CI runner already holds, but the runner must hold it.
- `api_key: secretref://<key>` – a token the **server** resolves against the
  org vault at write time (FAR-1640). The value never leaves the server.
- `credential_ref: <key>` – the same server-side vault reference in its
  declarative short form (FAR-1216): name the bare vault key, and the CLI sends
  the server the `secretref://<key>` token.

```yaml
entities:
  model_backends:
    - name: openai-prod
      display_name: OpenAI (production)
      provider: openai
      model_id: gpt-4o
      credential_ref: openai-prod-key   # value lives in the org vault
```

`credential_ref` adds a **plan-time** guarantee: before it computes the plan,
the CLI lists the org vault keys (through the server's `list_secrets` surface)
and **fails the plan** – never a silent pass – when the named key is absent, or
when the listing cannot be reached to confirm it. The `list_secrets` surface
requires the `secret.manage` scope, so the `MODULO_API_KEY` you store as a
GitHub secret must be an operator key permitted to list secrets; a key that
cannot list secrets blocks the affected entity rather than passing it. The
affected entity appears in the plan's `blocked` list with a reason naming the
key, so a PR comment shows the misconfiguration before anything is written. A
credential that is missing for any other reason (a `secretref://` set directly
on `api_key`, a key that
exists at plan time but is deleted before apply) still fails the apply with the
server's typed `credential_reference_error` (422), naming only the key.

Set the vault entry out-of-band (the org vault / `create_secret` surface) before
merging the config; the key itself is never committed.

- The plan jobs post the CLI's plain-text report (`create` / `update` /
  `block` / `fail` / `unchanged` lines plus the summary). If you need the
  machine-readable report instead, add `--output json` (alias `--json`) and
  post that.
- If trigger or pipeline secrets rotated in a place the server masks on read,
  add `--refresh-secrets` to the apply command so those declarations are
  re-sent every run.
- Fork PRs: `pull_request` runs from forks receive neither secrets nor a
  write-capable token, so the plan jobs fail at the CLI step (no
  `MODULO_API_KEY`) and could not comment anyway. The plan jobs therefore
  gate on `github.event.pull_request.head.repo.full_name == github.repository`
  so fork PRs skip them instead of failing. Drop that gate if you deliberately
  want forks to plan – they will then need their own `MODULO_URL` and
  `MODULO_API_KEY`.
