# Release Signing, SBOM & Checksums

Modulo releases are **cosign-signed** (keyless via GitHub OIDC) and ship an
**SBOM** (generated with syft) plus **SHA-256 checksums** for every release
artifact. This page documents how a release is cut and how to verify it.

## Cutting a release

Releases are tag-driven. Push a `v*` tag to trigger
`.github/workflows/release.yml`:

```bash
git tag v0.3.324
git push origin v0.3.324
```

The release path is intentionally **separate from PR CI**: nothing in `ci.yml`
or `deploy.yml` signs, checksums, or generates SBOMs. `deploy.yml` deploys the
same git SHA to Fly via `fly deploy` (Fly builds the image) — it never touches
ghcr.io.

## What the release workflow produces

1. **Docker image** — builds the all-in-one image
   (`backend/Dockerfile.fly`: backend + nginx-served frontend SPA) for
   `linux/amd64` + `linux/arm64`, pushes to `ghcr.io/farnalabs/modulo` with
   `vX.Y.Z` + `latest` tags. SLSA build provenance attestations are attached at
   build time (`provenance: mode=max`).
2. **SBOM** — syft scans the pushed image and emits an SPDX SBOM
   (`sbom.spdx.json`), which is **attested onto the image** with cosign so the
   SBOM is retrievable straight from the registry.
3. **cosign signature** — the image index digest is signed **keyless** using
   the GitHub Actions OIDC identity (`id-token: write`). No private key is
   stored in the repo or in secrets; the signature is bound to this
   workflow's run identity.
4. **Python package** — `farnalabs-modulo` sdist + wheel are built with uv.
5. **Checksums** — `SHA256SUMS` covers the wheel, sdist, and SBOM files, and is
   itself cosign blob-signed (`SHA256SUMS.bundle`).
6. **GitHub Release** — every artifact is attached to the `vX.Y.Z` release.

## Why keyless

The old model — a signing private key in repo secrets — has a severe failure
mode: the key that signs releases becomes the single secret an attacker wants.
Keyless signing uses a short-lived OIDC token minted by GitHub for this exact
workflow run, so there is **no long-lived signing secret to leak or rotate**.
The trust root is the GitHub repository + workflow identity
(`.github/workflows/release.yml`), verifiable via the certificate subject.

## Verifying a release

### Image signature

```bash
cosign verify ghcr.io/farnalabs/modulo@<digest> \
  --certificate-identity-regexp '^https://github.com/farnalabs/modulo/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

The certificate identity pins the signature to this repository's release
workflow running on a `v*` tag — a signature minted by any other workflow,
branch, or repository fails verification.

### SBOM attestation

```bash
cosign verify-attestation ghcr.io/farnalabs/modulo@<digest> \
  --certificate-identity-regexp '^https://github.com/farnalabs/modulo/\.github/workflows/release\.yml@refs/tags/v' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com'
```

The SBOM is also attached to the release as `sbom.spdx.json` and checksummed in
`SHA256SUMS`.

### Blob signatures (checksums + SBOM files)

```bash
# Download SHA256SUMS and SHA256SUMS.bundle from the release, then:
cosign verify-blob --bundle SHA256SUMS.bundle SHA256SUMS
# Re-verify every artifact matches the checksums:
sha256sum -c SHA256SUMS
```

## Consuming signed images in production

Pin deployments to the signed digest (not the mutable `latest` tag) so the
image identity cannot drift between builds:

```yaml
image: ghcr.io/farnalabs/modulo@sha256:<digest>
```

`docker-compose.prod.yml` and `scripts/install.sh` reference
`ghcr.io/farnalabs/modulo`; `docker-compose.prod.yml` supports a pinned digest
via the `TAG` variable.
