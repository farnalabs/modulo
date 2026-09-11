# Configuration Precedence (Native Single-Install)

How configuration is resolved on a native single-install bundle, and why
the native path has a DIFFERENT precedence mechanism from the Docker
Compose / plain-process deployment path. The two mechanisms are NOT the
same; this page says where each lives and what wins over what.

---

## Native installs (the launcher boot): env > state.json > defaults

On a native install, the launcher composes the bundled-service config
from two credential-free/0600 inputs in the data dir (state.json holds
ports; secrets.json holds generated passwords) and pins the composed
values as a 0600 dotenv file (`config.env`) inside the data dir. The only
composed keys today are `DATABASE_URL`, `DATABASE_ADMIN_URL` (same URL
promoted), `REDIS_URL`, and `MODULO_SYSTEM_DATABASE_URL`.

Implementation: `backend/src/modulo/launcher/config_source.py`
(`compose_config`, `write_pinned_env_file`). The pin lands through
`modulo.settings.pin_env_file`, so every `get_settings()` call in the API,
the SAQ children, and the CLI sees the bundled endpoints.

### Precedence order

1. **Real environment variables** the operator exports in their shell.
   A real env var ALWAYS outranks the pinned file: the pinned dotenv
   source sits between the env source and the field defaults in
   `pydantic-settings` resolution, pinned only for values the operator
   did not set.
2. **The pinned composed config (`config.env` in the data dir)**, whose
   values derive from `state.json` (ports) + `secrets.json` (passwords).
   Editing `state.json` ports or rotating `secrets.json` changes these
   on the NEXT boot.
3. **Field defaults** in `modulo.settings` sit below the pinned file.

Practical consequence: exporting `DATABASE_URL` in your shell says
"ignore the bundled Postgres and use mine" (launcher-hostile ambient
values like `PG*` and company URLs are scrubbed at boot per ADR 031
Decision 2, so only intentional exports survive).

## Compose / container deployments: env > secret-manager > defaults

The Compose path has NO pinned config file. Settings come straight from
the container environment (env file, orchestration variables, or a
secrets manager per [deployment-security.md](./deployment-security.md));
pydantic-settings resolves env then defaults. There is no state.json
step, and no launcher scrub: docker-compose's env semantics ARE the
precedence surface.

## settings.py vs settings_resolver.py - two different mechanisms

Do not conflate these; they do NOT share a code path:

- `backend/src/modulo/settings.py` is the pydantic-settings `Settings`
  model and the env-resolution machinery (field defaults, env source,
  dotenv support, the pin seam used by the native launcher).
- `backend/src/modulo/db/settings_resolver.py` is something DIFFERENT
  ENTIRELY: it resolves ORG-LEVEL runtime state (for example the
  org-wide trigger pause, `org_row_is_paused`) from the database rows -
  a per-request decision layered above Settings, read fresh from Postgres
  on every fire path. It does not read env vars and does not participate
  in the precedence order above.

---

## Cross-Reference

| Topic | Document |
|-------|----------|
| Environment variable reference | [`docs/configuration-reference.md`](./configuration-reference.md) |
| Secrets and key management | [`docs/security/secret-management.md`](./security/secret-management.md) |
| System requirements (install envelopes) | [`docs/system-requirements.md`](./system-requirements.md) |
| Troubleshooting (native bundles) | [`docs/troubleshooting.md`](./troubleshooting.md) |
