# Release Process

How we cut, validate and promote a release of Modulo - and how to rehearse the whole thing without publishing anything.

---

## The single entry point

Every release action runs through one workflow, `.github/workflows/release-unified.yml` ("Release: Unified Pipeline"). It is `workflow_dispatch` only - it never runs on a push - and takes two inputs. The older `.github/workflows/release.yml` name remains only as a fail-closed migration wrapper: it has no write permissions or release-mutating steps, so it cannot create a tag, release, freeze marker, or package. Operators using an old bookmark must re-run the command against the unified workflow.

| Input | Meaning |
|-------|---------|
| `mode` | One of `cut`, `recut`, `promote`, `dry-run` |
| `version` | The plain numeric version being released, e.g. `0.4.0` |

**The unified pipeline owns every publication.** `release-unified.yml` pushes the four container images (and their versioned tags) to GHCR, pushes the Helm chart to the OCI registry, builds and signs the release bundle through `bundle-release.yml`, creates the GitHub Release from that bundle, pushes the `bundle-v*` tag, publishes `farnalabs-modulo` to PyPI on `promote`, and dispatches Deploy plus `rc-validate`.

`release.yml` is the Alpha-path workflow the release lifecycle was originally built on, now reduced to a deprecated entry point. Every run fails closed with an `::error::` naming `release-unified.yml`, and it creates no tag, release, freeze marker, package, or release dispatch. Its old GA mode name `release` is the unified pipeline's `promote`.

Both workflows default to workflow-level `permissions: contents: read`, with write scopes granted only on the job that needs them. Each declares a concurrency group with `cancel-in-progress: false`, so two release actions of that workflow can never interleave.

## The unified pipeline modes

| Mode | What it does | Publishes? |
|------|--------------|------------|
| `cut` | Bumps the version in the source-of-truth files when needed, commits and pushes that to `main`, tags `vX.Y.Z-rcN` at the bumped commit, force-moves the `release-freeze` tag, publishes the four images and the Helm chart, builds and signs the bundle, creates the GitHub Release, pushes `bundle-v*`, and dispatches Deploy plus `rc-validate` | Yes - RC tag, freeze, images, chart, bundle, Release |
| `recut` | From the latest existing RC tag for the version: re-freezes `main` and re-publishes every artefact from **that RC commit** (never from a moved `main`) | Yes - re-publish under the existing RC |
| `promote` | Promotes the validated RC to GA: creates and pushes the `vX.Y.Z` tag, lifts the freeze, updates `CHANGELOG.md` and the GitHub Release, then publishes `farnalabs-modulo` to PyPI | Yes - GA tag + Release + CHANGELOG + PyPI package |
| `dry-run` | Validates the version, builds all four images with no push, rehearses the bundle build, and produces the SBOM artefact - then stops | **No - nothing at all** |

### The freeze

`cut` and `recut` push the tag `release-freeze`. The merge queue's freeze-gate job checks for that tag and does not merge while it is present, so `main` is held still against the RC under validation. `promote` deletes the tag, which lets the merge queue resume on its next tick.

### What `promote` requires (the promote invariants)

Promotion is the irreversible step, so the `release` job refuses to run unless all of these hold. Each is a separate, loud `::error::` and a non-zero exit:

1. **A freeze is active.** The `release-freeze` tag must exist - you cannot promote out of nothing.
2. **The GA tag does not already exist.** `vX.Y.Z` must not be present; a release is never overwritten.
3. **An RC exists for this version.** The latest `vX.Y.Z-rc*` tag must resolve.
4. **`main` is exactly the validated RC commit.** The newest RC tag's commit must equal `origin/main`'s HEAD (fetched live, so a commit landing between checkout and promote is caught). We promote the commit that was validated, not whatever happens to be on `main` now.
5. **The RC was actually validated.** The `validated/<rc_tag>` marker - pushed by `rc-validate.yml` on success - must exist *and* point at that same RC commit.

Only when all five pass does the job create and push the annotated GA tag.

### After the tags

The GA tag is the release identity, so it is created and pushed before anything else. The following step updates `CHANGELOG.md` (via `scripts/generate_changelog.py`) and the GitHub Release. Its failure handling is deliberate:

- A failed CHANGELOG commit/push turns the job **red** - it is a required deliverable.
- A failed GitHub Release update is a **warning only**, because the tag already exists and is intact.

In `cut` and `recut` the GitHub Release is created by a separate `publish-release` job from the bundle artefacts. It refuses to publish an incomplete release (every expected asset must be present) and never clobbers an existing asset: a replaced artefact invalidates every verification already made against the manifest.

## `dry-run` - rehearse without publishing

`dry-run` is the safe way to answer "what would this release actually do?" before committing to it. It:

- validates the version input (must be semver `X.Y.Z`);
- builds all four images with `push: false`, so every Dockerfile is proven on a clean runner without reaching a registry;
- rehearses the bundle build for real through `bundle-release.yml` under a throwaway `vX.Y.Z-dry-run` tag that is never created or pushed;
- generates the CycloneDX SBOM and uploads it as the `sbom-cyclonedx` workflow artefact (building a file inside the run is not publishing - it reaches no registry and no release).

It does **not** publish anything. It does not create a tag of any kind - not the RC, not `release-freeze`, not `validated/*`, not GA, not `bundle-v*` - does not push to any registry, and does not create a Release, commit a CHANGELOG, publish a package, create an attestation, deploy, or dispatch `rc-validate`.

### Why the "no tag at all" guarantee is structural, not a convention

A tag push is not a private act: `native-release-smoke.yml` triggers on the `bundle-v*` tag, and the `release-freeze` tag pauses the merge queue. A push to `main` fires `publish-images.yml`. So the dry-run path takes no tag and no commit push whatsoever rather than "a harmless local tag".

The rule that keeps this true as the workflow grows: **publish-shaped work belongs in `release-unified.yml`, and every job that publishes must be explicitly gated so it cannot run under `dry-run`.** Publish-shaped jobs carry `if: inputs.mode != 'dry-run'` (or a narrower mode gate). The legacy `release.yml` wrapper must remain fail-closed and read-only: do not add tags, releases, freeze changes, registry publishes, package publishes, changelog commits, or release dispatches to it.

## Container images and platform scope

`cut` and `recut` build and publish four images:

| Image | Context | Dockerfile |
|-------|---------|------------|
| all-in-one | `.` | `deploy/docker/Dockerfile.all-in-one` |
| backend | `backend` | `backend/Dockerfile` |
| frontend | `frontend` | `frontend/Dockerfile.prod` |
| runner-opencode | `.` | `deploy/docker/runner-opencode.Dockerfile` |

Each is tagged with both the RC tag and the plain `X.Y.Z`, and each runs its own smoke check after the push (`import modulo` for the all-in-one and backend, `nginx -t` for the all-in-one and frontend, `opencode --version` for the runner). A failing smoke check fails the job - never `|| true`. The `dry-run` rehearsal builds the same four images with no push.

Server images are `linux/amd64` **only**. Nothing we ship is `arm64`: every build step pins `--platform linux/amd64`, and `docs/system-requirements.md` lists `linux-amd64 (x86_64)` as supported while `linux-arm64` is Planned (P2) - as are macOS (P2) and Windows (P3). `bundle-release.yml` carries the same deferral ("linux-amd64 only for now; the matrix structure below is where linux-arm64 slots in; `scripts/bundle-versions.env` already carries the arm64 pins"). The Windows/macOS installer scope-out is tracked separately (FAR-1873).

## How images are published

`publish-images.yml` is the continuous image-build path, not a release path:

- **Trigger.** It runs on a `push` to `main` and on manual `workflow_dispatch`. It no longer triggers on version tags.
- **Tags.** It publishes a short-git-SHA tag on every run and moves `:latest` on `main` pushes, so `:latest` always means the newest `main` build. Versioned tags are published only by `release-unified.yml` (`cut` / `recut`).
- **Workflow-initiated pushes trigger no workflow at all.** A push made with the default `github.token` does not start a workflow run, and that applies to the unified pipeline's own pushes as well - its version-bump commit to `main` on `cut` therefore does not trigger `publish-images.yml`. The same reason is why the release job reaches `rc-validate.yml` and Deploy by `gh workflow run` rather than relying on a tag.
- **Smoke checks, no vulnerability gate.** The workflow gates each GHCR push on image smoke checks (`import modulo`, `nginx -t`) only. It has no critical-vulnerability gate - see `docs/deployment-security.md` §5.1.

## Legacy workflow migration

`release.yml` is retained only so an old bookmarked workflow name produces a loud, actionable failure. It is deliberately read-only and contains no release-mutating job. A failed wrapper run is safe: it creates no tag, moves no freeze marker, creates no GitHub Release, publishes no package, commits no changelog, and dispatches no release workflow. Re-run the intended operation through `Release: Unified Pipeline` using the commands above.

## Not implemented yet

`release-unified.yml` owns the RC/GA lifecycle, rehearsal, and PyPI publication. The legacy `release.yml` is not a second release implementation: its release jobs were removed outright in favour of a single fail-closed refusal, and it must stay that way. The work below is the remaining release scope.

| Remaining work | Ticket | Will own |
|----------------|--------|----------|
| `publish-images` | FAR-1869 | Publishing the built images to GHCR (folding in `publish-images.yml`, including its smoke checks) - implemented in `release-unified.yml` |
| `publish-release-bundle` | FAR-1869 | The release bundle, GitHub Release assets and Helm chart - implemented in `release-unified.yml` |
| `dispatch-rc-soak` | FAR-1870 | Dispatching the RC soak run - not yet in `release-unified.yml` |
| `publish-pypi` | FAR-1871 | Publishing the Python package - implemented in `release-unified.yml` `promote` |
| `pin-release-docs` | FAR-1872 | Pinning the released version in the docs - not yet in `release-unified.yml` |

SBOM production and file-artefact attestation are **no longer stubs** (FAR-1868). `release-unified.yml` runs `produce-sbom` for every mode, dry-run included, and uploads a validated CycloneDX SBOM as the `sbom-cyclonedx` artefact. `attest-artefacts` creates SLSA build provenance with `actions/attest-build-provenance` for that SBOM, and is the producer for the fail-closed `gh attestation verify` gate in `native-release-smoke.yml`. Today the SBOM is the only attested subject: container-image and release-bundle attestation remain FAR-1869 work, because attesting an image needs a registry digest - nothing fabricates a subject for an artefact that was not produced.

## Attestation scope: `gh attestation verify`, not cosign

The repository enforces build provenance with `gh attestation verify`, fail-closed, before any release artifact is installed (`native-release-smoke.yml`). cosign/Sigstore is explicitly **out of scope**: no consumer in the repo verifies a cosign signature or bundle, and `gh attestation verify` needs neither, so a cosign producer would be a second, unreferenced signing path.

## Running a release

```bash
# Rehearse first - builds everything, publishes nothing.
gh workflow run "Release: Unified Pipeline" -f mode=dry-run -f version=0.4.0

# Cut an RC and freeze (bumps the version, publishes images/chart/bundle/Release).
gh workflow run "Release: Unified Pipeline" -f mode=cut -f version=0.4.0

# After a fix has been merged to main, re-publish from the RC.
gh workflow run "Release: Unified Pipeline" -f mode=recut -f version=0.4.0

# Once the RC has validated, promote to GA (tag, Release, CHANGELOG, PyPI).
gh workflow run "Release: Unified Pipeline" -f mode=promote -f version=0.4.0
```

The deprecated `Release: Legacy Entry Point (Deprecated)` workflow still accepts the same arguments under its `action` input, but every run now fails closed with an instruction to re-run here; it performs no release action.

## Version numbering

The version is a plain `X.Y.Z`. A pre-release suffix (`-rc1`) lives in the git tag only and is never written into any file. `cut` on the unified pipeline bumps the source of truth with `scripts/bump-version.py` and commits that bump to `main` before the RC tag is created, so a new tag points at the bumped commit. See [upgrade-process.md](upgrade-process.md) for the version mirrors (`backend/pyproject.toml` as source of truth, `frontend/package.json` and the Helm `Chart.yaml`) and the manual bump commands.

**Legacy tags already on origin:** 42 `v*` tags predate this process (`v0.1.1`-`v0.1.41` and `v0.2.0`, committed June-August 2026, all predating `publish-images.yml`). The promote invariant that refuses to create a GA tag which already exists means none of those version numbers can be released through this process.
