---
id: feat-community-library
prd: 8.14
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/contribute.feature
  - backend/tests/bdd/features/library/ratings.feature
  - backend/tests/bdd/features/library/auto_update.feature
code:
  - backend/src/modulo/db/migrations/versions/0002_v2_teams_library.py
  - backend/src/modulo/core/library_service/__init__.py
  - backend/src/modulo/api/routes/library.py
  - frontend/src/views/LibraryView.vue
unit-tests:
  - backend/tests/unit/library_service/test_library_service.py
  - frontend/src/__tests__/LibraryView.spec.ts
depends-on: [feat-pipelines-library, feat-library-schemas]
status: partial
---

# Community Library Section

A separate "Community" tab in the Library UI, distinct from the
Modulo-maintained Native library, showing opinionated example pipelines
contributed under `source="community"`. Community items are never mixed
into the Native list and are labeled "not verified" everywhere they appear.
See ADR 010 §2.

## Behaviours

### Schema

- [x] `library_primitives.source` CHECK constraint widened from `local`/`registry`/`modulo` to also allow `community` via migration `0002_v2_teams_library`
- [x] `ck_library_primitives_source_fields` constraint updated with a `community` branch requiring `source_url`, `checksum`, `download_count`, `average_rating`, `review_count` all NULL (community items are not registry-verified entries)
- [x] Migration does not backfill any rows — `community` rows are created via the in-memory seed mechanism, not a data migration

### Seed content (in-memory, not DB-backed)

- [x] `_COMMUNITY_PRIMITIVES` in `library_service/__init__.py` defines 3 seed examples: "Translate to French" (`translate-to-french`), "QA Reviewer" (`qa-reviewer`), "Commit Message Linter" (`commit-message-linter`)
- [x] All seed primitives have `source="community"`, `verified=False`
- [x] `_filter_community(primitive_type, search)` filters the in-memory list by type and case-insensitive search
- [x] `_COMMUNITY_BY_ID` / slug-keyed index allow `get_primitive`/`get_primitive_by_slug` to fall back to community items when not found in the org's DB rows
- [x] Community primitives are visible to every organisation (not org-scoped) since they're an in-memory constant, not a DB row filtered by `organization_id`

### API — list & filter

- [x] `GET /api/v1/libraries?source=community` returns only community-database items (`test_list_primitives_source_community_only`)
- [x] `GET /api/v1/libraries?source=modulo` excludes community items (`test_list_primitives_source_modulo_excludes_community`)
- [x] Default (no `source` filter) merges community items alongside DB-backed org/native primitives (`test_list_primitives_default_merges_community_database`, `test_list_primitives_merges_community`)
- [x] `source` filter can explicitly exclude community (`test_list_primitives_exclude_community`)
- [x] `get_primitive` falls back to the community index when the ID isn't a DB row (`test_get_primitive_falls_back_to_community`)

### Copy-to-adapt / write restrictions

- [x] `copy_to_adapt` on a community primitive via MCP raises `CommunityPrimitiveReadOnlyError` (`test_copy_to_adapt_community_via_mcp_raises`) — community items cannot be adapted through the agent/API surface
- [x] `copy_to_adapt` on a community primitive via the browser UI path succeeds (`test_copy_to_adapt_community_via_browser_succeeds`) — copying creates an independent forked primitive, the seed item itself is never mutated

### Frontend — Native vs Community tabs

- [x] `LibraryView.vue` has a `section` ref (`'native' | 'community'`) driving two tabs: "Native Library" (`data-testid="library-section-native"`) and "Community" (`data-testid="library-section-community"`)
- [x] Community tab shows a disclaimer line (`data-testid="library-community-disclaimer"`) explaining these are unverified, user-contributed examples
- [x] Each community card shows a "Community — not verified" badge
- [x] Community items are excluded from the Native section's item list even if the API response merges them (`section.value === 'native' ? data.items.filter((p) => p.source !== 'community') : data.items`) — client-side belt-and-braces on top of the `source=community` query param
- [x] Within the Native section, the existing tier split (native/preview/in-dev) still applies; the Community section bypasses tiering entirely (community items aren't tiered)
- [x] Auto-update toggle (from `feat-library-auto-update`) is not shown for community primitives — they are read-only in the UI context

## Error Handling

- [x] All DB-backed routes catch `ProgrammingError` and return 501 Not Implemented with a descriptive migration-prompt message
- [x] All DB-backed routes also catch `SQLAlchemyError` and return 503 Service Unavailable with retry hint — connection/deadlock failures no longer propagate as raw 500
- [x] `list_community_contributions_endpoint` has an outer `except Exception` catch that returns 500 with a structured message — the only community-specific endpoint that does this (others rely on the CatchAllMiddleware)
- [x] `_fetch_published_community_from_db` returns `[]` on `ProgrammingError` or any `Exception` — degrades gracefully when the DB is not migrated
- [x] `get_primitive` in `library_service` returns `None` (not 500) on `ProgrammingError` or `SQLAlchemyError` — fallback to in-memory community items still works when the DB table is missing
- [x] `CommunityPrimitiveReadOnlyError` → 403 on `copy_to_adapt_endpoint` when `via_mcp=True`
- [x] `ContributionNotFoundError` → 404, `ContributionInvalidTransitionError` → 400 on `admin_publish_contribution_endpoint`
- [x] `LookupError` → 404 when the primitive does not exist for copy-to-adapt

## Resilience & Integration Robustness

- [x] All 7 library CRUD routes catch both ProgrammingError→501 and SQLAlchemyError→503
- [x] `get_primitive` service function catches SQLAlchemyError and returns None — community fallback still works when DB is degraded
- [x] `_fetch_published_community_from_db` degrades gracefully on any DB error — returns `[]`
- [x] Community contribution publish uses `_COMMUNITY_CACHE_LOCK` — serialises concurrent publish operations
- [x] `notify_importers_of_update` catches both ProgrammingError and generic exceptions — never blocks the publish response

## Edge Cases

- [x] **Concurrent community list mutations use `_COMMUNITY_CACHE_LOCK`** — the `_update_community_cache` function acquires `_COMMUNITY_CACHE_LOCK` (module-level `asyncio.Lock`) before appending to `_COMMUNITY_PRIMITIVES` and updating the ID/slug index dicts. Prevents race conditions between concurrent publish operations.
- [x] **Cross-tenant community DB fetch strips the caller's RLS org_id** — `_fetch_published_community_from_db` temporarily saves and removes `session.info["org_id"]` to query published community items across all orgs, then restores it. Without this, RLS filtering would restrict to only the caller's org — community items must be visible to every org.
- [x] **Community items are never returned in the "Native" tab** — `LibraryView.vue` filters `section === 'native'` items with `prim.source !== 'community'` as a client-side belt-and-braces measure, even though the API query with `source=modulo` already excludes community items server-side.
- [x] **No auto-update toggle for community primitives** — the auto-update toggle only renders for items with `prim.forked_from`, which community primitives never have (they are in-memory constants, not DB rows).
- [x] **Community contribution publish adds to both DB and in-memory cache** — `publish_contribution` calls `_update_community_cache` to add the published item to `_COMMUNITY_PRIMITIVES`, `_COMMUNITY_BY_ID`, and `_COMMUNITY_BY_SLUG`, so the in-memory cache stays consistent even across server restart warm-start.
- [x] **Slug collision protection** — community `get_primitive_by_slug` returns the in-memory community match only after exhausting org DB and modulo in-memory lookups (`_MODULO_BY_SLUG.get(...) or _COMMUNITY_BY_SLUG.get(...)`), so an org-local primitive with the same slug takes priority.

## Known Gaps

- **No external contribution mechanism exists yet.** Community items are currently a fixed, hardcoded in-memory seed list (`_COMMUNITY_PRIMITIVES`) — there is no UI or API path for an outside user to submit a new community primitive. This is explicitly called out in ADR 010 as a go-to-market sequencing item, not a built feature: the database should not be marketed as "community-driven" until real external contributions exist.
- **No moderation/review workflow for community submissions** — because there is no submission mechanism yet, there is also no review, approval, or rejection flow for community content.
- **No pagination/count semantics documented for merged native+community lists** beyond the unit tests already listed — large community seed growth is untested.
- **Community DB publish does not backfill the in-memory cache on restart** — items published while the server is running are cached in `_COMMUNITY_PRIMITIVES`, but after a restart, only `_fetch_published_community_from_db` retrieves them (best-effort, no RLS bypass for warm-start). Published items do not survive a full server restart as in-memory primitives.

## QA History

### 2026-07-12 — R2 docs QA

**CRITICAL — Fixed stale migration file reference:**
The `code:` field and behaviour description referenced `0063_library_community_source.py` which never existed. The community source CHECK constraint (`source IN ('local', 'registry', 'modulo', 'community')`) and `ck_library_primitives_source_fields` constraint with community branch were implemented as part of `0002_v2_teams_library.py`, not a standalone `0063` migration. Updated both references to point to `0002_v2_teams_library.py`.

**Product map updated:**
- Fixed migration file path in `code:` frontmatter
- Fixed migration reference in Schema behaviour description

**Status:** partial (same 4 known gaps remain).

### 2026-07-08 — cross-cutting QA (index 253)

**CRITICAL — Added SQLAlchemyError→503 catches to 7 library route handlers:**
`list_library_primitives_endpoint`, `get_library_primitive_endpoint`, `create_library_primitive_endpoint`, `update_library_primitive_endpoint`, `delete_library_primitive_endpoint`, `copy_to_adapt_endpoint`, and `export_pipeline_endpoint` previously only caught `ProgrammingError`→501. Connection failures, deadlocks, and transient DB errors propagated as raw 500. All 7 routes now have dual catch: `ProgrammingError`→501 (missing migrations) and `SQLAlchemyError`→503 (transient DB failure).

**MAJOR — ~15 hardcoded English strings in LibraryView.vue wrapped in `$t()`:**
Tab labels ("Native Library", "Community"), community disclaimer paragraph, "Loading..." spinner, Modulo/community badges, type filter options (Workflows, Agents, Schemas, Integrations, Composites), action buttons (Create Pipeline, View Details), and pagination controls (Previous, Next, Page N of M) all wrapped in `$t()` with 16 new i18n keys added to `en-US.js`.

**Product map updated:**
- Added Resilience & Integration Robustness section (5 checkboxes)
- Updated Error Handling section to note SQLAlchemyError→503 coverage

**Status:** partial (same 4 known gaps remain).

### 2026-07-06 — Library product map QA

**CRITICAL — Fixed `prd: 15` → `prd: 8.14`:**
The `prd:` frontmatter field was pointing to `15` (Resolved Design Decisions) instead of `8.14` (Community Library PRD section
§8.14). §15 only contains a single table row about community library scope — not the feature specification.

**MAJOR — Removed duplicate unchecked Edge Cases:**
Two `[ ]` items in the Edge Cases section (`No pagination/count semantics documented` and `Community DB publish after server
restart`) were duplicates of the same items in Known Gaps. Removed from Edge Cases — gaps belong only in Known Gaps.

**Product map updated:**
- Corrected `prd:` reference from §15 to §8.14
- Removed duplicate edge case entries that overlapped with Known Gaps

**Status:** partial (same 4 known gaps remain).
