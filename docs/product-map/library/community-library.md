---
id: feat-community-library
prd: 15
delivery-tasks: []
bdd:
  - backend/tests/bdd/features/library/browse.feature
  - backend/tests/bdd/features/library/copy_to_adapt.feature
  - backend/tests/bdd/features/library/contribute.feature
  - backend/tests/bdd/features/library/ratings.feature
  - backend/tests/bdd/features/library/auto_update.feature
code:
  - backend/src/modulo/db/migrations/versions/0063_library_community_source.py
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

- [x] `library_primitives.source` CHECK constraint widened from `local`/`registry`/`modulo` to also allow `community` via migration `0063_library_community_source`
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

- [x] All DB-backed routes catch `ProgrammingError` and return 501 Not Implemented with a descriptive migration-prompt message (`list_library_primitives_endpoint`, `get_library_primitive_endpoint`, `create_library_primitive_endpoint`, `update_library_primitive_endpoint`, `delete_library_primitive_endpoint`, `copy_to_adapt_endpoint`, `export_pipeline_endpoint`, `_analyse_bundle`, `confirm_import_endpoint`, `create_pipeline_from_template_endpoint`, `community_contribute_endpoint`, `list_community_contributions_endpoint`, `admin_publish_contribution_endpoint`)
- [x] `list_community_contributions_endpoint` has an outer `except Exception` catch that returns 500 with a structured message — the only community-specific endpoint that does this (others rely on the CatchAllMiddleware)
- [x] `_fetch_published_community_from_db` returns `[]` on `ProgrammingError` or any `Exception` — degrades gracefully when the DB is not migrated
- [x] `get_primitive` in `library_service` returns `None` (not 500) on `ProgrammingError` or `SQLAlchemyError` — fallback to in-memory community items still works when the DB table is missing
- [x] `CommunityPrimitiveReadOnlyError` → 403 on `copy_to_adapt_endpoint` when `via_mcp=True`
- [x] `ContributionNotFoundError` → 404, `ContributionInvalidTransitionError` → 400 on `admin_publish_contribution_endpoint`
- [x] `LookupError` → 404 when the primitive does not exist for copy-to-adapt

## Edge Cases

- [x] **Concurrent community list mutations use `_COMMUNITY_CACHE_LOCK`** — the `_update_community_cache` function acquires `_COMMUNITY_CACHE_LOCK` (module-level `asyncio.Lock`) before appending to `_COMMUNITY_PRIMITIVES` and updating the ID/slug index dicts. Prevents race conditions between concurrent publish operations.
- [x] **Cross-tenant community DB fetch strips the caller's RLS org_id** — `_fetch_published_community_from_db` temporarily saves and removes `session.info["org_id"]` to query published community items across all orgs, then restores it. Without this, RLS filtering would restrict to only the caller's org — community items must be visible to every org.
- [x] **Community items are never returned in the "Native" tab** — `LibraryView.vue` filters `section === 'native'` items with `prim.source !== 'community'` as a client-side belt-and-braces measure, even though the API query with `source=modulo` already excludes community items server-side.
- [x] **No auto-update toggle for community primitives** — the auto-update toggle only renders for items with `prim.forked_from`, which community primitives never have (they are in-memory constants, not DB rows).
- [x] **Community contribution publish adds to both DB and in-memory cache** — `publish_contribution` calls `_update_community_cache` to add the published item to `_COMMUNITY_PRIMITIVES`, `_COMMUNITY_BY_ID`, and `_COMMUNITY_BY_SLUG`, so the in-memory cache stays consistent even across server restart warm-start.
- [x] **Slug collision protection** — community `get_primitive_by_slug` returns the in-memory community match only after exhausting org DB and modulo in-memory lookups (`_MODULO_BY_SLUG.get(...) or _COMMUNITY_BY_SLUG.get(...)`), so an org-local primitive with the same slug takes priority.
- [ ] **No pagination/count semantics documented for merged native+community lists** beyond the unit tests already listed — large community seed growth is untested.
- [ ] **Community DB publish after server restart** — the in-memory cache is the primary mechanism. `_fetch_published_community_from_db` supplements it for warm-start scenarios but has no backfill into the in-memory cache: published items are only added to the in-memory cache at publish time, not on server restart. Items published between restarts won't appear in the in-memory list until another publish triggers cache refresh.

## Known Gaps

- **No external contribution mechanism exists yet.** Community items are currently a fixed, hardcoded in-memory seed list (`_COMMUNITY_PRIMITIVES`) — there is no UI or API path for an outside user to submit a new community primitive. This is explicitly called out in ADR 010 as a go-to-market sequencing item, not a built feature: the database should not be marketed as "community-driven" until real external contributions exist.
- **No moderation/review workflow for community submissions** — because there is no submission mechanism yet, there is also no review, approval, or rejection flow for community content.
- **No pagination/count semantics documented for merged native+community lists** beyond the unit tests already listed — large community seed growth is untested.
- **Community DB publish does not backfill the in-memory cache on restart** — items published while the server is running are cached in `_COMMUNITY_PRIMITIVES`, but after a restart, only `_fetch_published_community_from_db` retrieves them (best-effort, no RLS bypass for warm-start). Published items do not survive a full server restart as in-memory primitives.
