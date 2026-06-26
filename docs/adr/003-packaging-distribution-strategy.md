# ADR 003 — Packaging & Distribution Strategy

**Date**: 2026-06-26
**Status**: Draft — awaiting review

---

## Context

Modulo currently has no automated publishing pipeline. Users can only run it by cloning the repo and using Docker Compose with local builds (`docker compose up` in dev mode). There is no way to:

- Run a single command and have Modulo up and running
- Download a pre-built artifact
- Install via a package manager

The project needs a distribution strategy that covers the spectrum from "try it in 30 seconds" to "deploy to production K8s." Every distribution channel must also answer the database dependency question — Modulo requires a database (PostgreSQL default, SQLite supported) and optionally Redis for Celery.

The `MODULO_DB=sqlite` mode (implemented in `settings.py`, backed by `aiosqlite`) is the key technical enabler. It allows a zero-dependency standalone mode where the database is just a file on disk. Redis/Celery remains the tighter coupling — the `celery_app.py` module-level init crashes if Redis is unreachable, and the polling trigger engine depends on Celery beat.

---

## Options Considered

### 1. Docker Images on ghcr.io (primary)

Publish pre-built backend and frontend images to GitHub Container Registry. Users pull images and run with Docker Compose.

| Pros | Cons |
|---|---|
| Matches existing architecture | Two-container setup (backend + frontend) |
| Works on all OSes with Docker | Docker is a dependency |
| Supports both PG and SQLite | Frontend API URL baked at build time |
| K8s-compatible (Helm chart exists) | |

**Database story:** PostgreSQL via a third container, or SQLite via a mounted volume.

### 2. All-in-One Docker Image

A single container that bundles backend + built frontend (served via FastAPI `StaticFiles` mount).

| Pros | Cons |
|---|---|
| Single `docker run` command | Loses nginx-level optimisations (gzip, ETags, CSP) |
| Simple mental model | Catch-all route needed for SPA history-mode routing |
| | Couples frontend version to backend |

**Database story:** Same as #1 — PG or SQLite.

### 3. PyPI Package (`pip install modulo` / `uv tool install modulo`)

Publish the backend as a Python package with the built frontend embedded as package data. A `modulo` CLI command starts the server.

| Pros | Cons |
|---|---|
| Natural for Python ecosystem | Requires Python 3.12+ |
| `uv tool install modulo` is one command | ~200MB installed (Python + deps + frontend) |
| SQLite works out of the box | Celery + Redis still coupled |
| | Need to build frontend in CI and embed it in the wheel |

**Database story:** SQLite by default. PG via env var for production.

### 4. Standalone Binary (PyInstaller / Nuitka)

Bundle Python interpreter + all deps + frontend into a platform-specific executable.

| Pros | Cons |
|---|---|
| No Python or Docker required | ~80-150MB per binary |
| Double-click to run | AV false positives |
| | Fragile with asyncio + multiprocessing |
| | Must build per OS/arch in CI |
| | LangChain + OTel + Cryptography make this brittle |

**Database story:** SQLite only.

### 5. Homebrew (macOS)

A Homebrew formula that installs Modulo. Three possible approaches:

| Approach | Viability |
|---|---|
| Formula that installs via pip | Adds Homebrew as middleman — no value over pip |
| Formula that downloads a binary | Requires standalone binary (option #4) |
| Cask that installs Docker | `brew install --cask docker` + helper script |

**Database story:** Depends on underlying distribution method.

### 6. Chocolatey / Scoop (Windows)

| Approach | Viability |
|---|---|
| Chocolatey package | Same issues as Homebrew — needs a binary or wraps Docker |
| Scoop bucket | Same |

### 7. npm

A JavaScript wrapper that downloads and manages the Docker container (pattern used by some CLI tools). Architecturally wrong for a Python web app.

### 8. apt / yum / Snap / Flatpak

Native Linux package managers and desktop app stores. Modulo is a web server, not a desktop app or library. None of these fit.

---

## Decision

Adopt a **tiered distribution strategy** with three tiers corresponding to user type and effort-to-value ratio.

### Tier 0 — Foundation (build before shipping anything)

1. **GitHub Actions CI/CD pipeline** — lint, type-check, test, build Docker images on every push
2. **Production Dockerfiles** — multi-stage backend, working frontend nginx config (fix the broken `/tmp/nginx-template` issue), `docker-entrypoint.sh` that runs migrations before starting uvicorn
3. **Release workflow** — tag-driven: `docker buildx` for `linux/amd64` + `linux/arm64`, push to `ghcr.io/anomalyco/` with semver + `latest` tags
4. **Version automation** — `hatch-vcs` or `setuptools-scm` to derive version from git tags

### Tier 1 — Ship (release-blocking)

5. **Docker images published to ghcr.io** — `modulo-backend` and `modulo-frontend` (separate, as the Helm chart expects)
6. **`docker-compose.prod.yml`** — references published images instead of building from source, includes `.env` template, health checks, Celery worker/beat services
7. **Production single-container image** — backend + frontend combined, gunicorn with configurable workers, nginx sidecar or FastAPI-served static files with SPA catch-all
8. **`install.sh`** — `curl https://modulo.run/install.sh | bash` that detects Docker, downloads `docker-compose.prod.yml` and runs `docker compose up`

### Tier 2 — Ship Soon (post-launch)

9. **PyPI package** — `pip install modulo` or `uv tool install modulo`. Requires:
   - Celery optionality refactor (`celery` + `redis` as optional extras)
   - `modulo start` CLI command with first-run wizard (generate keys, run migrations, seed admin user)
   - Frontend build + embed in wheel
   - Manylinux CI for Rust-based deps (`cryptography`)

10. **`install.sh` enhancement** — detect Python 3.12+ and offer `uv tool install modulo` as alternative to Docker

### Tier 3 — Defer

11. **Standalone binary (PyInstaller)** — proven user demand first
12. **Homebrew / Chocolatey** — depend on standalone binary
13. **npm / apt / snap** — architecturally wrong fit

---

## Consequences

- **Positive:** Anyone can run Modulo with one command after Tier 1 ships
- **Positive:** Docker remains the canonical deployment for production (multi-container, PostgreSQL, Redis, K8s)
- **Positive:** SQLite-first for evaluation lowers the barrier to zero
- **Positive:** The CI/CD pipeline gates all future releases — every PR gets linted, tested, and validated as publishable
- **Negative:** We now maintain multiple distribution methods with different behaviours
- **Negative:** PyPI package requires a significant refactor (Celery optionality, CLI tooling) that has no user-facing value until the package ships
- **Negative:** The all-in-one Docker image duplicates nginx logic and will never be as good as the two-container setup

---

## Related Documents

- ADR 002 — Multi-Backend Database Abstraction Strategy (SQLite foundation)
- `backend/Dockerfile` — needs production multi-stage rewrite
- `frontend/Dockerfile.prod` — nginx config broken (`/tmp/nginx-template`)
- `helm/modulo/values.yaml` — references `ghcr.io/anomalyco/modulo-*`
- `docker-compose.yml` — dev-mode, builds from source
