# Supabase Integration — Research & Plan

**Status:** Parked (2026-06-28). Not started, no delivery tasks created.

---

## Why Supabase?

Supabase is the most popular open-source BaaS (Backend-as-a-Service). The free tier includes:

| Service | Free Tier | Limits |
|---|---|---|
| Postgres 16 | 500 MB | Paused after 1 week idle, 2 concurrent connections |
| Auth (GoTrue) | 50,000 users | Built-in magic link, OAuth, SSO |
| Storage | 1 GB | 5 GB bandwidth |
| Edge Functions | 500,000 invocations | Deno-based, not Python |

A native Supabase connector would let Modulo users skip managing Postgres entirely — huge onboarding win for solo devs.

---

## Integration Layers (ordered by effort)

### Layer 0: Direct Postgres Connection (already works)

Supabase exposes a standard Postgres connection string:

```
DATABASE_URL=postgresql+asyncpg://postgres:[PASSWORD]@db.[REF].supabase.co:5432/postgres
```

This works today with zero code changes. Modulo's Alembic migrations, RLS abstraction, and asyncpg driver will all work against Supabase Postgres.

**Caveats:**
- Free tier pauses after ~1 week of inactivity — you'd get a connection error on first request after idle. Waking takes 10–30s.
- Max 2 concurrent connections — fine for a solo dev, fails under any real load.
- 500 MB limit — small for LangGraph checkpoints + run history.
- No advisory locks (`pg_advisory_lock` is blocked) — Modulo's lock abstraction falls back to `GenericLock` (in-memory), which is fine for single-worker.

**Action needed:** Add a `docs/backends/supabase-postgres.md` guide. That's it.

### Layer 1: Supabase Auth (GoTrue) Integration

Supabase Auth is GoTrue (open-source, self-hostable by Supabase). It provides:
- Email/password auth with magic links
- OAuth (GitHub, Google, GitLab, etc.)
- Row Level Security integration (but Modulo has its own RLS layer)

Modulo already has JWT auth with refresh tokens, Basic Auth, OIDC, SAML, and API keys. Supabase Auth is a fifth auth provider — it maps to Modulo's existing OIDC flow.

**Why it'd be popular:** Many supabase users manage auth through supabase and would expect Modulo to accept their supabase JWT. A supabase JWT middleware that validates against supabase's JWKS would let users skip Modulo's auth setup.

**Implementation sketch:**
- Add a Supabase auth provider: supabase project ref + anon key → fetch JWKS from `https://[ref].supabase.co/auth/v1/.well-known/jwks.json`
- Accept `Authorization: Bearer <supabase_jwt>` on the same middleware chain as Modulo JWTs
- User info endpoint maps supabase user → Modulo user (JIT provisioning)

**Effort:** S (~1 file, ~100 lines middleware)

### Layer 2: Supabase Storage Connector

Supabase Storage is S3-compatible under the hood. Modulo already has the `FilesystemConnector` and could add a generic `S3Connector` that works with any S3 API — Supabase, MinIO, AWS S3, R2.

**Why:** Let users store checkpoint blobs, pipeline artifacts, and uploaded schemas in Supabase Storage instead of the local filesystem.

**Effort:** M (new connector type)

### Layer 3: Supabase Edge Functions for agent execution

Supabase Edge Functions are Deno-based, not Python. Modulo's agent runtime runs Python (LangGraph + custom agents). This can't transparently port without a Python-to-Deno bridge, which isn't practical.

Not recommended for the agent execution path. Use E2B instead.

### Layer 4: Supabase Realtime for WebSocket event broker

Supabase Realtime uses Postgres LISTEN/NOTIFY + WebSocket. Modulo's event broker uses Redis pub/sub for multi-worker deployments. Supabase Realtime could replace Redis for the WebSocket fan-out in a Supabase-based deployment.

**Not worth it.** Redis is simpler, and one of the deployment options already embeds it.

---

## Summary

| Layer | Effort | Appeal | Recommendation |
|---|---|---|---|
| Layer 0 — Direct PG | S (docs only) | ✅ High | Ship as `docs/backends/supabase-postgres.md` now |
| Layer 1 — Auth | S | Medium | V1 Extended / V2 |
| Layer 2 — Storage | M | Medium | Generic S3Connector, not Supabase-specific |
| Layer 3 — Edge Functions | XL | Low | Don't do this |
| Layer 4 — Realtime | M | Low | Don't do this |

**Recommendation:** Do Layer 0 (docs) immediately — it costs nothing. Park Layers 1–4 for post-alpha.
