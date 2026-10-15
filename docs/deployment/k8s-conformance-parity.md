# Kubernetes conformance parity (FAR-1053)

This document records what the Kubernetes conformance gate proves, what it
CANNOT prove, and the rule for claiming a substrate as conformant. It is the
README for `.github/workflows/k8s-conformance.yml`.

## The gate

| Leg | Trigger | Cluster | Scope |
| --- | --- | --- | --- |
| `kind` | pull requests and pushes to `main`, path-filtered to `backend/src/modulo/core/{runtime_provider,bundled_runner,pipeline_engine}/**`, `backend/tests/conformance/**`, `backend/pyproject.toml`, `backend/uv.lock` and the workflow itself, + `deploy/helm/**` (FAR-1558 follow-up: a chart-only change lints the chart without creating a cluster); manual via `workflow_dispatch` (`job=kind`). Pushes to `main` and manual dispatches are deliberately **unfiltered**. | throwaway [kind](https://kind.sigs.k8s.io) cluster, deleted `always()` | The runtime-provider conformance suite (`backend/tests/conformance/runtime_provider/`, marker `runtime_provider_conformance`) plus the negative suite (a deliberately broken adapter must fail; a kill-before-collect must be detected, never a synthetic success), plus `helm lint deploy/helm/modulo` on the chart (runs whenever either the conformance surface or the chart changed). |
| `managed` | weekly cron (Monday 04:00 UTC); manual via `workflow_dispatch` (`job=managed`) | ONE managed cloud per ISO week, rotating EKS -> AKS -> GKE so all three cycle over three weeks | The same conformance suite against a real cloud control plane. |
| `staleness` | daily cron (05:17 UTC); manual via `workflow_dispatch` (`job=staleness`) | none | Last-green visibility on the deploy-staleness-check pattern: publishes the last-green dates to the run summary and fails when the gate goes dark. Counts only runs where the conformance suite step actually executed and passed (FAR-1620) - a path-skipped docs-only/chart-only success never refreshes last-green. |

The suite itself is **deselected by default** (the `addopts` clause in
`backend/pyproject.toml`), so a normal unit or integration run never touches a
cluster and never skip-passes it green. The workflow selects it explicitly
with `-m runtime_provider_conformance`.

## Claim discipline

**A substrate is claimed as conformant ONLY while its most recent
conformance run is green.**

- `kind` green: the kind substrate is claimed. The claim is withdrawn by the
  next red kind-leg run.
- A managed leg run green on week N: that one cloud (EKS, AKS or GKE) is
  claimed until its next scheduled run. The other two clouds are unaffected -
  each is claimed only by its own green run.
- A red run withdraws the claim for that substrate immediately. There is no
  grace period on a red: green-after-red re-establishes the claim.
- Never-green means never-claimed. The `staleness` job fails the whole gate
  if no leg has ever run green, so "no claim" stays visible.

The dates are published two ways: each green run stamps its date into its
step summary, and the `staleness` job recomputes the authoritative
last-green date from the workflow's run history every day - paged back past
the widest grace window, excluding its own cron's runs (so a green
staleness run can never refresh the number it is measuring - the
deploy-throttle self-reference lesson), and verifying each candidate green
run's job steps so only runs where the suite actually ran and passed count
(FAR-1620).

## What `kind` cannot prove (CI-parity gaps, not product gaps)

These are gaps in the EVIDENCE the CI run provides. They are not defects in
the product, and none of them is claimed by a green kind run. Where a managed
weekly run also cannot prove a row, the table says so.

| Dimension | kind per-PR run | managed weekly run | Why |
| --- | --- | --- | --- |
| Workload identity (EKS IRSA, GKE Workload Identity, AKS Entra workload identity) | NOT proven | NOT proven by the suite | The suite authenticates with a plain kubeconfig/ServiceAccount token - it never assumes a cloud identity fabric. Cloud workload identity for customer workloads is exercised only when a customer configures it; the parity claim stops at "the ABC contract holds on this cluster". |
| NetworkPolicy enforcement | NOT proven | NOT proven by the suite | kind's kindnet CNI does not enforce NetworkPolicy, so a policy applied on kind proves nothing about traffic. The suite therefore only asserts the provider's TYPED REFUSAL to claim egress enforcement (the `ProviderCapabilityUnsupportedError` contract); actual egress enforcement remains the customer's NetworkPolicy on an enforcing CNI. |
| Pod Security `restricted` admission | PROVEN - but only because the conformance namespace opts in | PROVEN as configured on that cluster | kind does NOT enforce Pod Security Admission by default. The conformance fixture labels its namespace `pod-security.kubernetes.io/enforce: restricted` (this is the "CI values" hook), so every workspace pod in the run is admitted under `restricted` or the run fails. A customer cluster's own admission policy can still be stricter. |
| Cloud control-plane realism (RBAC edge cases, API server quotas, regional rate limits, node pools) | NOT proven | PROVEN (real control plane) | kind is a single-node cluster with a permissive admin kubeconfig. |
| CNI / multi-node behaviour | NOT proven (single node, kindnet) | Partially - depends on the cloud's default CNI | Out of scope for this gate; the gate is the ABC contract, not a distribution matrix. |
| Chart install/upgrade lifecycle | NOT proven (`helm lint` only) | NOT proven (`helm lint` only) | The gate lints the chart; it never installs it. Chart lifecycle validation is deliberately out of scope (see the workflow comment on the lint step). |

## needs-human: managed-cluster secrets (not yet provisioned)

The `managed` leg needs three repository secrets, each holding the RAW
kubeconfig YAML of a dedicated conformance cluster:

- `K8S_CONFORMANCE_EKS_KUBECONFIG`
- `K8S_CONFORMANCE_AKS_KUBECONFIG`
- `K8S_CONFORMANCE_GKE_KUBECONFIG`

Requirements on each kubeconfig:

1. The referenced user/context can create and delete namespaces (the suite
   creates a uniquely-named namespace plus ServiceAccount per run and tears
   them down), and can use `pods/exec` and `pods/log` (the exec and
   log-tail primitives under test).
2. The cluster is a throwaway or non-production cluster: the suite runs
   real pods, and a crashed run is cleaned up by the label selector
   `modulo.conformance=true`, not by hand.
3. No cluster-level Pod Security prerequisite: the suite's namespace fixture
   applies the `restricted` enforcement labels itself, so the admission row
   of the table above is proven on any conformant cluster.

Until a secret exists, the `managed` job fails loudly with a
`FAR-1053 needs-human` error naming the missing secret. That failure is
deliberate: no green run means no conformance claim, and a silently skipped
job would read as green.

## Last-green visibility

- Every green leg writes its date to that run's step summary.
- The `staleness` job (daily) recomputes from the Actions API, scanning run
  history back past the widest grace window and inspecting each candidate
  green run's job steps:
  - last suite-verified green run of any leg (staleness-cron runs excluded;
    FAR-1620: a run counts only when the "Run the runtime-provider
    conformance suite" step concluded `success` - a path-skipped
    docs-only push or chart-only PR that still concludes `success` never
    refreshes this, and a jobs-API read failure is never counted as fresh)
    - fails the gate when this ages past the grace window (default 28
    days, repo variable `K8S_CONFORMANCE_STALENESS_GRACE_DAYS`) or when no
    suite-verified green exists in the scanned history;
  - last scheduled (managed) run - fails when the weekly cron has produced
    no run for 14 days (repo variable
    `K8S_CONFORMANCE_MANAGED_DARK_GRACE_DAYS`), which is the GitHub
    cron-drop tripwire;
  - last green managed run - published, with a warning while it has never
    been green (the needs-human state above).

## Running it locally

```sh
kind create cluster --name modulo-conformance --wait 180s
kubectl wait --for=condition=Ready nodes --all --timeout=120s
# The suite's pods run the runtime provider's default workspace image
# (`python:3.13-slim`, `core/runtime_provider/k8s.py::_DEFAULT_IMAGE`). CI
# pre-loads it into the kind node to sidestep Docker Hub pull limits; do the
# same locally, otherwise every pod waits on a registry pull.
docker pull python:3.13-slim
kind load docker-image python:3.13-slim --name modulo-conformance
cd backend
uv sync --frozen
uv run pytest tests/conformance/ -m runtime_provider_conformance --tb=short -q --timeout=600
kind delete cluster --name modulo-conformance
```

The `pytest` flags match the workflow's invocation exactly. CI runs it as
`uv run --no-build --no-sync pytest ...` because `uv sync --frozen` already
populated the environment on the preceding step; `--no-build` is safe there
only because the sync already built `pytest-bdd` (a pinned source archive with
no wheel, so `--no-build` on a cold cache hard-fails the sync - FAR-791). Keep
the two steps separate locally for the same reason.

`KUBECONFIG` (or the default kubeconfig path) must point at the kind
cluster; the suite creates and deletes its own namespace, and the workflow's
final cleanup is `kubectl delete namespace -l modulo.conformance=true`. Never
leave a kind cluster running after a local run.
