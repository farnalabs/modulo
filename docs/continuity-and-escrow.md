# Continuity and Escrow

Modulo is self-hosted and published as source under the Business Source License
1.1 (BUSL). This page describes what a customer, or a successor taking over a
deployment, can rely on if Farnalabs stops operating. It covers the from-source
build that makes the source usable on its own, and the two licence caveats that
need planning before that day: PrimeUI and the Team trust anchor.

The short version: you keep the **Community** tier forever because it needs no
licence key, you can build, modify and patch the source yourself, and each
released version converts to Apache-2.0 three years after its first public
release. You do not automatically keep the **Team** tier or Farnalabs' future
patches.

## What escrow by publication means

Escrow here is not a copy of the source held by a third party. It is the public
repository: the code that builds Modulo is readable, and it builds without any
credential held only by Farnalabs.

Verified properties:

| Property | Evidence |
|---|---|
| The source builds with no Farnalabs-held secret. | The production all-in-one image builds from a clean checkout with no credential or build secret from Farnalabs. [Build the image from source](#build-the-image-from-source) below gives the command, which was run against this checkout for this page. |
| The Community tier needs no licence key. | A deployment with no `MODULO_LICENSE_KEY` boots and runs on Community (`backend/src/modulo/core/license_bootstrap.py`: "Booting with no licence at all stays valid"). |
| You may modify and patch the source. | The BUSL Additional Use Grant permits production use of Modulo and of any modified version or derivative work you create, including internal and commercial use, except offering Modulo or such a version as a paid hosted or managed service that provides substantially the same functionality as Modulo. See `LICENSE`. |
| Each released version converts to Apache-2.0. | `LICENSE`: the Change Date for the distributed version is 2029-07-12, and "For each version of the Licensed Work, the Change Date is three years after that version is first publicly released." |

## What you keep, and what you do not

| Capability | Without Farnalabs |
|---|---|
| Community tier, pipelines, runs, connectors, evals, HITL, audit | Kept. No licence is required. |
| Source, local patches, your own fork | Kept. Build from your fork at any time. |
| A released version after its Change Date | Apache-2.0, per `LICENSE`. |
| Team tier (SSO, team RBAC, audit viewer) | Not automatic. It needs a licence key signed by an anchor you trust. Recoverable by issuing your own key: see [The Team licence trust anchor](#the-team-licence-trust-anchor). |
| Farnalabs' future patches and releases | Not kept. You maintain your fork from the Change Date onward, or from the last release you mirrored. |
| A PrimeUI licence key for the frontend | Not yours. Obtain your own: see [The PrimeUI licence](#the-primeui-licence). |

## Build the image from source

The production compose file pulls `ghcr.io/farnalabs/modulo:${TAG}`. That image
is convenient, but it is a Farnalabs-built artefact. The escrow path is to build
the same all-in-one image from the source you already have:

```bash
git clone https://github.com/farnalabs/modulo.git
cd modulo
git checkout <release-tag>          # the release you intend to keep running

docker build \
  -f deploy/docker/Dockerfile.all-in-one \
  -t ghcr.io/farnalabs/modulo:local \
  .
```

Tagging the local build with the reference the compose file expects lets compose
use it without pulling:

```bash
TAG=local docker compose -f deploy/compose/docker-compose.prod.yml up -d
```

Compose reuses a local image when one exists under the requested reference, and
pulls from the registry otherwise. The first start still needs the environment
file and the first account described in the
[Deployment Guide](deployment.md#docker-compose-single-server-multi-user).
Verify the stack with the same readiness probe:

```bash
curl -s http://localhost/healthz/ready | python -m json.tool
```

Notes:

- Server images are built for `linux/amd64` only. On an arm64 host, add
  `--platform linux/amd64` to the build (and run the image under emulation).
- The build is self-contained: it installs the pinned Python and Node
  dependencies from the repository's lock files. It does not need a Farnalabs
  registry credential, a Farnalabs signing key, or a Farnalabs build secret.
- A build of an unmodified checkout is the baseline. If you patch the source,
  the same command builds your fork.

## The PrimeUI licence

The frontend uses PrimeVue 5 (`primevue` 5.0.4, with `@primeuix/styled` 1.2.0
and `@primeuix/themes` 3.1.0). PrimeVue 5 is part of PrimeUI, which PrimeTek
licenses commercially. PrimeVue 4 was MIT; version 5 is not.

The bundled licence text states:

> A valid license key is required to use this software. License verification is
> performed offline, with no telemetry and no remote connection. A missing,
> invalid, or expired key may cause the software to display a license notice.

### What the key does, and what happens without it

`frontend/src/main.ts` passes `VITE_PRIMEUI_LICENSE` to
`app.use(PrimeVue, { license: ... })`. PrimeVue hands it to
`@primeui/license-manager`, which verifies the Ed25519 signature offline against
a public key built into the package.

Without a key, or with an invalid one:

- the build **succeeds** and the application runs normally;
- PrimeVue logs a console warning (`[PrimeUI] No license key configured for
  PrimeUI.`) and renders a fixed bottom-right banner reading **"Invalid PrimeUI
  License"** (`@primevue/core` `license/licenseBanner/index.mjs`);
- the banner is a `pointer-events: none` element with `role="alert"`. It does
  not gate a component, a route or an API call.

So the missing key is cosmetic on screen, but it is not a compliant long-term
state: PrimeTek's terms require a valid key, and the package's terms also forbid
removing the licence mechanism.

### Which builds currently carry a key

Only the Fly deployment passes the key, as a build argument through
`backend/Dockerfile.fly`, from the `VITE_PRIMEUI_LICENSE` GitHub secret
(`.github/workflows/deploy.yml`). The **all-in-one image published to GHCR is
built without it**: `.github/workflows/publish-images.yml` and
`.github/workflows/release-unified.yml` build
`deploy/docker/Dockerfile.all-in-one` with no build arguments, and that
Dockerfile declares no `VITE_PRIMEUI_LICENSE` argument. A self-hosted compose
deployment, whether it pulls the published image or builds it from source, shows
the banner unless the operator supplies a key.

### What a self-hoster must do

Obtain a PrimeUI licence key from PrimeTek, then build with it. Choose the
licence you qualify for:

- **Community (free)** if your organisation meets PrimeTek's published
  thresholds: fewer than 5 developers in total, fewer than 10 employees, less
  than $1,000,000 USD annual gross revenue, and less than $3,000,000 USD of
  outside funding. The free licence covers up to 4 developer seats and requires
  annual renewal.
- **Commercial (paid)** otherwise. Licensed per developer, perpetual, with one
  year of updates.

Apply and read the binding terms at:

- Community: <https://primeui.dev/licenses/community>
- Commercial: <https://primeui.dev/licenses/commercial>

The all-in-one Dockerfile does not currently accept `--build-arg
VITE_PRIMEUI_LICENSE`. The verified way to supply a key to that build is the
frontend directory's `.env` file, which `frontend/vite.config.ts` lifts into
`process.env` (FAR-1858), and which the build context copies into the frontend
builder:

```bash
printf 'VITE_PRIMEUI_LICENSE=%s\n' "$YOUR_PRIMEUI_KEY" > frontend/.env
docker build -f deploy/docker/Dockerfile.all-in-one \
  -t ghcr.io/farnalabs/modulo:local .
rm frontend/.env
```

The `.env` file is gitignored, and the root `.dockerignore` does not exclude it,
so it reaches the builder as `/app/.env` and Vite inlines the value into the
client bundle. Never commit the key: a `VITE_*` value is compiled into the
browser bundle and is readable by every user of the instance.

The key embedded in a Farnalabs-built image is Farnalabs' credential, not your
organisation's licence. If you need your deployment to run under your own
PrimeUI licence, build from source with your key as above. If you intend to rely
on the key inside a published image, confirm with PrimeTek that your use is
covered.

## The Team licence trust anchor

Team licences are Ed25519-signed keys in the format
`<base64(payload)>.<base64(signature)>`. Verification uses exactly one public
key, the trust anchor:

- the built-in development public key in `backend/src/modulo/core/license.py`
  when `MODULO_LICENSE_PUBLIC_KEY` is unset; or
- the configured `MODULO_LICENSE_PUBLIC_KEY` (64 hex characters).

The built-in key is the public half of Farnalabs' **development** keypair. It is
not the anchor Farnalabs signs production licences with, and outside debug mode
it is not trusted. A production licence therefore cannot verify against it, and
startup logs a `LICENSE_CRITICAL` message saying the development anchor is in
use. The production anchor is not in the repository.

### Retain the anchor with the licence

Keep a copy of `MODULO_LICENSE_PUBLIC_KEY` wherever you keep the licence key. It
is a public key, not a secret, so storing it next to the licence is safe. If you
hold a Farnalabs-issued Team licence and lose the anchor, the licence can no
longer verify. Re-issuing a key that verifies against the anchor you still hold
requires the matching private key, which Farnalabs holds.

### Issue your own Team licence

A successor does not need Farnalabs to restore Team. The deployment trusts
whatever anchor the operator configures, so you can generate your own keypair
and sign your own Team licences. Run these commands in a checkout of the source.

1. Generate an Ed25519 keypair:

   ```bash
   cd backend
   uv run python -c "
   from modulo.core.registry.crypto import generate_keypair
   import json
   print(json.dumps(generate_keypair(), indent=2))
   "
   ```

   This prints `private_key`, `public_key` and `fingerprint`. Both keys are 64
   hex characters. Keep the private key offline and never put it in the
   deployment environment unless you intend to issue keys from that instance.

2. Configure the public key as the deployment's anchor:

   ```bash
   MODULO_LICENSE_PUBLIC_KEY=<your-public-key-hex>
   ```

   Set it wherever the deployment's other configuration lives, then restart the
   API and the workers. Every process resolves the same anchor.

3. Mint a Team licence with the private key:

   ```bash
   cd backend
   uv run python -c "
   from modulo.core.license_signing import generate_team_license
   print(generate_team_license(
       'Your Organisation',
       term_months=12,
       private_key_hex='<your-private-key-hex>',
   ))
   "
   ```

   The command prints one line in the licence-key format. The signed payload
   carries `tier: team`, a `features` list (`team_rbac`, `sso`, `audit_viewer`),
   the organisation id, and the expiry (`issued_at + 12 months` by default).
   A `team` tier licence unlocks every flag catalogued at the Team tier,
   including `admin_spend_limits`. Pass a different `term_months` for a
   different term.

4. Install the key in one of two ways:

   - **Environment variable.** Add `MODULO_LICENSE_KEY=<the key>` next to the
     anchor and restart. Configuring a key with no anchor fails startup closed,
     so set both.
   - **Admin UI.** Open Settings > License and paste the key, or call the admin
     endpoint as a system administrator:

     ```bash
     curl -sS -X POST "$MODULO_URL/api/v1/admin/license" \
       -H "Authorization: Bearer $TOKEN" \
       -H 'Content-Type: application/json' \
       -d '{"license_key": "<the key>"}'
     ```

     The upload is verified against the anchor before it is stored, and a
     mismatch returns a structured error naming the anchor instead of storing an
     unusable key.

Verified: a keypair generated by step 1, a key minted by step 3, and
`parse_and_verify` against the matching public key returns a valid `team`
licence with the three feature flags and a 12-month expiry.

Activation is a technical mechanism. Whether you are entitled to use the Team
tier remains governed by your licence from Farnalabs and the BUSL terms.

## A continuity checklist

1. Mirror the repository and keep the release tag you run. Tag the exact commit;
   do not rely only on a mutable branch.
2. Keep the build recipe above, and run it once per release so you know it still
   works from your own infrastructure.
3. Keep your Team anchor and its private key, or record that you will generate a
   replacement keypair. Without the private key you cannot renew a self-issued
   licence; without the anchor a Farnalabs-issued one will not verify.
4. Keep your PrimeUI licence key and the terms you accepted, and supply it on
   every rebuild.
5. Record the Change Date of the version you run, so you know when that version
   becomes Apache-2.0.
6. Keep a tested restore of the database and object storage. Continuity of the
   binary is not continuity of the data: see
   [Backup and Restore](operations/backup.md).

## Related documentation

- [Deployment Guide](deployment.md)
- [Self-Hosted Admin Operations](operations/self-hosted-admin.md)
- [Licensing and Plan Tiers](product-map/licensing/license.md)
- [Configuration Reference](configuration-reference.md)
- [Upgrade Process](upgrade-process.md)
- `LICENSE`
