# Release Process

How we cut, validate and promote a release of Modulo - and how to rehearse the whole thing without publishing anything.

---

## The single entry point

Every release action runs through `.github/workflows/release-unified.yml` ("Release: Unified Pipeline"). This is the only executable release entry point. The legacy `.github/workflows/release.yml` is a read-only, fail-closed wrapper that exists only to direct old callers here; it performs no release action. It is `workflow_dispatch` only - it never runs on a push - and takes a version plus a mode input:

| Input | Meaning |
|-------|---------|
| `mode` | `cut`, `recut`, `promote`, or `dry-run` |
| `version` | The plain numeric version being released, e.g. `0.4.0` |

The workflow defaults to workflow-level `permissions: contents: read`, with write scopes granted only on the job that needs them: the `release` job carries `contents: write` (push tags and the CHANGELOG commit, create the GitHub Release) and `actions: write` (`gh workflow run rc-validate.yml`). The `dry-run` and build jobs are read-only. A `release-unified-${{ github.ref }}` concurrency group with `cancel-in-progress: false` ensures two release actions can never interleave.

## The four modes

| Mode | What it does | Publishes? |
|------|--------------|------------|
| `cut` | Tags `vX.Y.Z-rcN` at the current `main` HEAD, pushes the `release-freeze` tag (which suspends the merge queue), and dispatches `rc-validate.yml` | Yes - RC tag + freeze |
| `recut` | After a fix has been manually merged to `main`: tags the *next* RC and re-freezes | Yes - RC tag + freeze |
| `release` | Promotes the validated RC to GA: creates and pushes the `vX.Y.Z` tag, lifts the freeze, then updates `CHANGELOG.md` and creates the GitHub Release | Yes - GA tag + Release |
| `dry-run` | Builds everything and resolves what a `cut` would have done, then stops | **No - nothing at all** |

### The freeze

`cut` and `recut` push a tag named `release-freeze`. The merge queue checks for that tag and does not merge while it is present, so `main` is held still against the RC under validation. A cut chooses the first free `vX.Y.Z-rcN`; a recut always chooses the next free RC number and tags the current `main` commit, so it cannot accidentally republish an older RC after a fix has landed. The freeze stops merges, not deploys: the release workflow force-dispatches `Deploy: Staging + Prod` with `force_deploy=true`, and the deploy workflow's normal staging, E2E and production gates remain in force. `release` / `promote` deletes the tag, which lets the merge queue resume on its next tick. Do not add release-freeze awareness to `deploy.yml`.

### Recut runbook

The unified workflow is the canonical entry point. Its `recut` mode publishes from the new RC tag created at current `main`; it never checks out or reuses the previous RC tag.

Use `recut` after a fix has been merged to `main` while the release freeze is active:

1. Confirm the fix is on `main` and the previous RC remains identifiable.
2. Dispatch `recut` for the same `X.Y.Z` version. The workflow selects the next free `vX.Y.Z-rcN`, tags the current `main` commit with it, and force-moves `release-freeze` to that new RC commit.
3. The workflow then dispatches `Deploy: Staging + Prod` with `force_deploy=true`, so the freeze cannot throttle the soak deployment. It dispatches `rc-validate.yml` for the new RC only after the deploy dispatch is accepted.
4. `rc-validate.yml` waits for `/api/v1/deployment` to report the new RC commit, runs the read-only Schemathesis checks, and pushes `validated/vX.Y.Z-rcN` at the RC commit only on success.
5. Promote only after the validation marker exists and points at the latest RC commit. If validation fails, fix the issue on `main` and repeat this recut sequence; do not move the marker by hand.

## What `release` requires (the four promote invariants)

Promotion is the irreversible step, so the `release` job refuses to run unless all four of these hold. Each is a separate, loud `::error::` and a non-zero exit:

1. **A freeze is active.** The `release-freeze` tag must exist - you cannot promote out of nothing.
2. **The GA tag does not already exist.** `vX.Y.Z` must not be present; a release is never overwritten.
3. **`main` is exactly the validated RC commit.** The newest `vX.Y.Z-rc*` tag's commit must equal `origin/main`'s HEAD (fetched live, so a commit landing between checkout and promote is caught). We promote the commit that was validated, not whatever happens to be on `main` now.
4. **The RC was actually validated.** The `validated/<rc_tag>` marker - pushed by `rc-validate.yml` on success - must exist *and* point at that same RC commit.

Only when all four pass does the job create and push the annotated GA tag.

### After the tag

The GA tag is the release identity, so it is created and pushed before anything else. The following step updates `CHANGELOG.md` (via `scripts/generate_changelog.py`) and creates the GitHub Release. Its failure handling is deliberate:

- A failed CHANGELOG commit/push turns the job **red** - it is a required deliverable.
- Failed release-notes generation or a failed `gh release create` is a **warning only**, because the tag already exists and is intact.

## `dry-run` - rehearse without publishing

`dry-run` is the safe way to answer "what would this release actually do?" before committing to it. It:

- validates the version input (must be semver `X.Y.Z`);
- resolves the version, the next RC tag, and the artefacts a `cut` would produce, and writes them to the job summary;
- builds every container image (see below);
- generates the CycloneDX SBOM and uploads it as the `sbom-cyclonedx` workflow artefact (building a file inside the run is not publishing - it reaches no registry and no release).

It does **not** publish anything. It does not create a tag of any kind - not the RC, not `release-freeze`, not `validated/*`, not GA - does not push to any registry, and does not create a Release, commit a CHANGELOG, publish a package, create an attestation, or deploy. It is therefore safe to run during an active freeze.

### Why the "no tag at all" guarantee is structural, not a convention

A tag push is not a private act: `.github/workflows/publish-images.yml` triggers on `push: tags: ["v*"]`. Pushing *any* version-shaped tag - including an RC - immediately publishes container images to GHCR. So the dry-run path takes no tag action whatsoever rather than "a harmless local tag".

The rule that keeps this true as the workflow grows: **any job added to `release-unified.yml` that publishes something must be explicitly gated so it cannot run under `dry-run`.** Publish-shaped work is expected to carry `if: inputs.mode != 'dry-run'` (matching the existing publish jobs). This is documented in the workflow header so the next author cannot miss it.

## Container images and platform scope

The `dry-run` build job builds the three images the deployments consume:

| Image | Context | Dockerfile |
|-------|---------|------------|
| all-in-one | `.` | `deploy/docker/Dockerfile.all-in-one` |
| backend | `backend` | `backend/Dockerfile` |
| frontend | `frontend` | `frontend/Dockerfile.prod` |

Server images are `linux/amd64` **only**. Nothing we ship is `arm64`: `publish-images.yml` declares no `platforms:` (so it builds amd64), and `docs/system-requirements.md` lists `linux-amd64 (x86_64)` as supported while `linux-arm64` is Planned (P2) - as are macOS (P2) and Windows (P3). `bundle-release.yml` carries the same deferral ("linux-amd64 only for now; the matrix structure below is where linux-arm64 slots in; `scripts/bundle-versions.env` already carries the arm64 pins"). The Windows/macOS installer scope-out is tracked separately (FAR-1873).

`dry-run` builds each image with `push: false`, so it validates that every Dockerfile still builds on a clean runner without producing a loadable image. The post-publish smoke checks that guard against the 2026-07-06 stale-image regression (a `:latest` that did not even contain the `modulo` package) live in `publish-images.yml` and are *not* duplicated here - they belong with the publish path, and re-implementing them in a `push: false` build would be a parallel copy that could drift. Wiring them into this workflow is future work (see below).

### How images are actually published

`publish-images.yml` triggers on a `push` of a version tag (`v*`) - but a release's own tag push does NOT run it, so image publishing is owned by the unified workflow directly rather than by the tag trigger:

- **The unified workflow's `release` job tag push does not trigger the workflow.** The job pushes its `vX.Y.Z-rc*` and `vX.Y.Z` tags with the default `github.token` (`GH_TOKEN: ${{ github.token }}`). GitHub deliberately suppresses workflow runs for events caused by that token, so the tag push triggers no workflow at all - which is also why the same job reaches `rc-validate.yml` by running `gh workflow run` rather than relying on the tag. A release therefore does not publish container images through the tag trigger; the unified workflow's publish job (FAR-1869) is the piece that will own publishing them directly.
- **The dry-run's "no tag at all" rule stays correct.** It is conservative for the right reason: a HUMAN pushing a version-shaped tag - or any push made with a credential other than `github.token` - WOULD trigger `publish-images.yml` and publish images. The rationale above is unchanged; what is added here is that a WORKFLOW-initiated push behaves differently from a human one.
- **The CHANGELOG commit's push to `main` triggers no CI either.** The same `github.token` pushes it, so `main`'s HEAD after a release is not CI-validated. That is observed behaviour, recorded here rather than changed.

## Not implemented yet

`release-unified.yml` today covers the RC/GA lifecycle and the rehearsal. The remaining publish steps are **deliberate, visible stubs** rather than silent gaps - each carries a comment naming its tracking ticket. They are gated so they can never execute.

| Stub job | Ticket | Will own |
|----------|--------|----------|
| `publish-images` | FAR-1869 | Publishing the built images to GHCR (folding in `publish-images.yml`, including its smoke checks) |
| `publish-release-bundle` | FAR-1869 | The release bundle, GitHub Release assets and Helm chart |
| `dispatch-rc-soak` | FAR-1870 | Dispatching the RC soak run |
| `publish-pypi` | FAR-1871 | Publishing the Python package |
| `pin-release-docs` | FAR-1872 | Pinning the released version in the docs |

SBOM production and file-artefact attestation are **no longer stubs** (FAR-1868). `produce-sbom` runs for every action, dry-run included, and uploads a validated CycloneDX SBOM as the `sbom-cyclonedx` artefact. `attest-artefacts` runs for every action except `dry-run` and creates SLSA build provenance with `actions/attest-build-provenance`; it is the producer for the fail-closed `gh attestation verify` gate in `native-release-smoke.yml`. Container-image and release-bundle attestation remain FAR-1869 work: both sets are produced by that slice's still-stubbed publish jobs, and attesting an image needs a registry digest that does not exist yet - nothing fabricates a subject for an artefact that was not produced.

Until those land, a GA release still gets its tag, its GitHub Release and its CHANGELOG section - it just does not yet publish new images, bundles, packages or version-pinned docs through this workflow. It does produce an SBOM for the run, plus an attestation for it on non-`dry-run` actions.

## Attestation scope: `gh attestation verify`, not cosign

The repository enforces build provenance with `gh attestation verify`, fail-closed, before any release artifact is installed (`native-release-smoke.yml`). cosign/Sigstore is explicitly **out of scope**: no consumer in the repo verifies a cosign signature or bundle, and `gh attestation verify` needs neither, so a cosign producer would be a second, unreferenced signing path.

## Running a release

```bash
# Rehearse first - builds everything, publishes nothing.
gh workflow run "Release: Unified Pipeline" -f mode=dry-run -f version=0.4.0

# Cut an RC and freeze.
gh workflow run "Release: Unified Pipeline" -f mode=cut -f version=0.4.0

# After a fix has been merged to main, cut the next RC and re-run the soak.
gh workflow run "Release: Unified Pipeline" -f mode=recut -f version=0.4.0

# Once the RC has validated, promote to GA.
gh workflow run "Release: Unified Pipeline" -f mode=promote -f version=0.4.0
```

## Version numbering

The version is a plain `X.Y.Z`. A pre-release suffix (`-rc1`) lives in the git tag only and is never written into any file. See [upgrade-process.md](upgrade-process.md) for the version source of truth (`backend/pyproject.toml`) and how to bump it.

**Legacy tags already on origin:** 42 `v*` tags predate this process (`v0.1.1`-`v0.1.41` and `v0.2.0`, committed June-August 2026, all predating `publish-images.yml`). The promote invariant that refuses to create a GA tag which already exists means none of those version numbers can be released through this process.
