# Human Tasks — Duncan

Things that need a human. No agent can do these.

---

## Infrastructure Setup

- [ ] **Register PyPI org** — `pip install modulo` needs the name reserved. Create account on pypi.org, register `modulo` package name.
- [ ] **Register Docker Hub org / ghcr.io access** — if using Docker Hub, create `farnalabs/modulo` org and repo. If ghcr.io, verify `anomalyco/modulo` has anonymous pull enabled (no login required for `docker pull`).
- [ ] **Configure ghcr.io anonymous pull** — without this, `install.sh | bash` fails because users aren't logged in. Go to GitHub org settings → Package settings → toggle "Allow public access".
- [ ] **Register `modulo.run` domain** — for `install.sh` and the website. Set up DNS (probably via your existing registrar).
- [ ] **Provision hosting for `modulo.run` website** — where the landing page + install.sh live. Static hosting (Netlify, Vercel, Cloudflare Pages) is fine.
- [ ] **Set up Cloudflare or CDN** — for `modulo.run`, TLS, caching of install.sh, etc.

---

## License / Commercial

- [x] **Create license signing key pair** — DONE (2026-08-13). Ed25519 key pair generated and stored in the KeePassXC vault (`license-signing-private-key`, `modulo-license-public-key`). The app signs issued licenses with `MODULO_LICENSE_PRIVATE_KEY` (see `core/license_signing.py`) and verifies against `MODULO_LICENSE_PUBLIC_KEY`.
- [x] **Back up the license private key** — DONE (2026-08-13). The private key is stored in the KeePassXC vault (`license-signing-private-key`) alongside a generated keypair backup; the vault itself is committed to the admin repo for backup/portability. A paper wallet is optional (revisit if the key is ever used to sign customer-facing licenses at scale).
- [x] **Register for Stripe** — DONE (2026-08-13). Account live under `admin@farnalabs.com`, payouts to Starling. Secret keys stored in the vault; `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` env vars feed the fulfilment webhook.
- [x] **Set up Stripe billing** — DONE (2026-08-13). Product `modulo-enterprise`, price `enterprise-annual-ga` ($8k/yr), coupon `first-year-50` (50% off first year), payment link `https://buy.stripe.com/fZu3cvdcJcgP4pHfM69EI03`. Backend auto-fulfils purchases (webhook → license key → customer email).
- [ ] **Write `SECURITY.md`** — contact email, PGP key fingerprint, disclosure timeline. Required for any serious project. Goes in the repo root.

---

## Release / Publishing

- [ ] **Publish first Docker images** — once CI is set up, tag `v0.1.0-alpha.1` and let the release workflow push to ghcr.io. Verify pull & run.
- [ ] **Upload `install.sh` to `modulo.run`** — the script we create in `task-pkg0-install-sh` needs to live at `https://modulo.run/install.sh`.
- [ ] **Set up PyPI publisher (deferred)** — when `task-pkg0-pypi-package` is ready, register as a PyPI trusted publisher via GitHub OIDC so CI can push tags → PyPI automatically.

---

## Accounts & OAuth

- [ ] **Create GitHub OAuth app** — for the GitHub Connector. Modulo needs a GitHub OAuth App for users to authenticate their GitHub access. Register at GitHub Settings → Developer Settings → OAuth Apps. Callback URL will be `https://<your-modulo-instance>/api/v1/auth/github/callback`.
- [ ] **Create GitLab OAuth app** — same, for GitLab connector.
- [ ] **Create Linear OAuth app** — for Linear connector.
- [ ] **Register Sentry project** — for error monitoring if you want it. Free tier is generous.
- [ ] **Set up email sender** — for notifications, password resets, etc. Modulo doesn't send email yet, but it will. Register a SendGrid/Mailgun/Resend account so the API key is ready.

---

## Once-Off

- [ ] **`git secret` / `git-crypt` init** — the repo will need to store the Stripe public key, maybe test API keys. Install `git-secret` or `git-crypt`, init a key, add the license public key. Tell the team.
- [ ] **Register `modulo` on PyPI** — even if you're not publishing yet, claim the name so nobody else does.
- [ ] **Claim `modulo` on Homebrew** — check if `brew modulo` is taken. If not, you can reserve it.
