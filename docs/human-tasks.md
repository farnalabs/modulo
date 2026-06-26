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

- [ ] **Create license signing key pair** — Ed25519 key pair for signing license tokens. The app already has `Ed25519SigningService` wired in. You need:
  ```
  openssl genpkey -algorithm Ed25519 -out modulo-license.pem
  openssl pkey -in modulo-license.pem -pubout -out modulo-license.pub
  ```
  The public key goes in the codebase. The private key stays off-network.
- [ ] **Back up the license private key** — print the hex + paper wallet? Bitwarden? Hardware security key? Pick a strategy and do it before any customer depends on it.
- [ ] **Register for Stripe** — stripe.com, create account, fill out business details. You'll need the API keys for the billing integration.
- [ ] **Set up Stripe billing** — create product + pricing tiers. The app will call Stripe APIs to create subscriptions, handle webhooks, etc. We'll wire this up later.
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
