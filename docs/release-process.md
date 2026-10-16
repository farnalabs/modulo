# Release Process

How we cut, validate and promote a release of Modulo - and how to rehearse the whole thing without publishing anything.

---

## The single entry point

Every release action runs through one workflow, `.github/workflows/release.yml` ("Release: Cut / Recut / Promote"). It is `workflow_dispatch` only - it never runs on a push - and takes two inputs:

| Input | Meaning |
|-------|---------|
| `action` | One of `cut`, `recut`, `release`, `dry-run` |
| `version` | The plain numeric version being released, e.g. `0.4.0` |

The workflow runs with workflow-level `permissions: contents: write` and `actions: write`, and a `release-process` concurrency group with `cancel-in-progress: false`, so two release actions can never interleave.

## The four modes

| Mode | What it does | Publishes? |
|------|--------------|------------|
| `cut` | Tags `vX.Y.Z-rcN` at the current `main` HEAD, pushes the `release-freeze` tag (which suspends the merge queue), and dispatches `rc-validate.yml` | Yes - RC tag + freeze |
| `recut` | After a fix has been manually merged to `main`: tags the *next* RC and re-freezes | Yes - RC tag + freeze |
| `release` | Promotes the validated RC to GA: creates and pushes the `vX.Y.Z` tag, lifts the freeze, then updates `CHANGELOG.md` and creates the GitHub Release | Yes - GA tag + Release |
| `dry-run` | Builds everything and resolves what a `cut` would have done, then stops | **No - nothing at all** |

### The freeze

`cut` and `recut` push a tag named `release-freeze`. The merge queue checks for that tag and does not merge while it is present, so `main` is held still against the RC under validation. `release` deletes the tag, which lets the merge queue resume on its next tick.

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
- builds every container image (see below).

It does **not** publish anything. It does not create a tag of any kind - not the RC, not `release-freeze`, not `validated/*`, not GA - does not push to any registry, and does not create a Release, commit a CHANGELOG, publish a package, or deploy. It is therefore safe to run during an active freeze.

### Why the "no tag at all" guarantee is structural, not a convention

A tag push is not a private act: `.github/workflows/publish-images.yml` triggers on `push: tags: ["v*"]`. Pushing *any* version-shaped tag - including an RC - immediately publishes container images to GHCR. So the dry-run path takes no tag action whatsoever rather than "a harmless local tag".

The rule that keeps this true as the workflow grows: **any job added to `release.yml` that publishes something must be explicitly gated so it cannot run under `dry-run`.** Publish-shaped work is expected to carry `if: inputs.action != 'dry-run'` (matching the existing publish jobs). This is documented in the workflow header so the next author cannot miss it.

## Container images and platform scope

The `dry-run` build job builds the three images the deployments consume:

| Image | Context | Dockerfile |
|-------|---------|------------|
| all-in-one | `.` | `deploy/docker/Dockerfile.all-in-one` |
| backend | `backend` | `backend/Dockerfile` |
| frontend | `frontend` | `frontend/Dockerfile.prod` |

Server images are `linux/amd64` **only**. Nothing we ship is `arm64`: `publish-images.yml` declares no `platforms:` (so it builds amd64), and `docs/system-requirements.md` lists `linux-amd64 (x86_64)` as supported while `linux-arm64` is Planned (P2) - as are macOS (P2) and Windows (P3). `bundle-release.yml` carries the same deferral ("linux-amd64 only for now; the matrix structure below is where linux-arm64 slots in; `scripts/bundle-versions.env` already carries the arm64 pins"). The Windows/macOS installer scope-out is tracked separately (FAR-1873).

`dry-run` builds each image with `push: false`, so it validates that every Dockerfile still builds on a clean runner without producing a loadable image. The post-publish smoke checks that guard against the 2026-07-06 stale-image regression (a `:latest` that did not even contain the `modulo` package) live in `publish-images.yml` and are *not* duplicated here - they belong with the publish path, and re-implementing them in a `push: false` build would be a parallel copy that could drift. Wiring them into this workflow is future work (see below).

## Not implemented yet

`release.yml` today covers the RC/GA lifecycle and the rehearsal. The remaining publish steps are **deliberate, visible stubs** rather than silent gaps - each carries a comment naming its tracking ticket. They are gated so they can never execute.

| Stub job | Ticket | Will own |
|----------|--------|----------|
| `publish-images` | FAR-1869 | Publishing the built images to GHCR (folding in `publish-images.yml`, including its smoke checks) |
| `publish-release-bundle` | FAR-1869 | The release bundle, GitHub Release assets and Helm chart |
| `produce-sbom-and-attestations` | FAR-1868 | SBOM and build attestations (`backend/scripts/generate-sbom.py` exists but has no caller yet) |
| `dispatch-rc-soak` | FAR-1870 | Dispatching the RC soak run |
| `publish-pypi` | FAR-1871 | Publishing the Python package |
| `pin-release-docs` | FAR-1872 | Pinning the released version in the docs |

Until those land, a GA release still gets its tag, its GitHub Release and its CHANGELOG section - it just does not yet publish new images, bundles, SBOMs, packages or version-pinned docs through this workflow.

## Running a release

```bash
# Rehearse first - builds everything, publishes nothing.
gh workflow run "Release: Cut / Recut / Promote" -f action=dry-run -f version=0.4.0

# Cut an RC and freeze.
gh workflow run "Release: Cut / Recut / Promote" -f action=cut -f version=0.4.0

# After a fix has been merged to main, cut the next RC.
gh workflow run "Release: Cut / Recut / Promote" -f action=recut -f version=0.4.0

# Once the RC has validated, promote to GA.
gh workflow run "Release: Cut / Recut / Promote" -f action=release -f version=0.4.0
```

## Version numbering

The version is a plain `X.Y.Z`. A pre-release suffix (`-rc1`) lives in the git tag only and is never written into any file. See [upgrade-process.md](upgrade-process.md) for the version source of truth (`backend/pyproject.toml`) and how to bump it.
