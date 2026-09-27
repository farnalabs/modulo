---
id: feat-notifications
prd: N/A
adr: []
code:
  - backend/src/modulo/core/notifier
  - backend/src/modulo/api/routes/admin_notifications.py
  - backend/src/modulo/api/routes/notifications.py
  - backend/src/modulo/api/routes/in_app_notifications.py
  - backend/src/modulo/core/email_service.py
  - backend/src/modulo/db/models/notification.py
  - backend/src/modulo/db/models/notification_delivery.py
  - backend/src/modulo/db/models/notification_endpoint.py
unit-tests:
  - backend/tests/unit/notifier/test_notifier.py
  - backend/tests/unit/notifier/test_event_mapper.py
  - backend/tests/unit/notifier/test_hitl_awaiting_terminal_suppression.py
  - backend/tests/unit/api/test_notifications_endpoint.py
  - backend/tests/unit/api/test_admin_notifications_webhooks.py
  - backend/tests/unit/api/test_in_app_notifications_preferences.py
  - backend/tests/unit/api/test_in_app_notifications_lifecycle.py
  - backend/tests/unit/api/test_in_app_notifications_run_metadata.py
  - backend/tests/unit/api/test_admin_email.py
bdd:
  - backend/tests/bdd/features/notifications/failure_webhook.feature
  - backend/tests/bdd/features/notifications/hitl_webhook.feature
  - backend/tests/bdd/features/notifications/signing.feature
  - backend/tests/bdd/features/in_app_notifications/dashboard_panel.feature
  - backend/tests/bdd/features/in_app_notifications/dismiss_flow.feature
  - backend/tests/bdd/features/in_app_notifications/notification_filters.feature
  - backend/tests/bdd/features/in_app_notifications/sse_integration.feature
  - backend/tests/bdd/steps/test_alpha_notifications.py
  - backend/tests/bdd/steps/test_in_app_notifications.py
depends-on:
  - feat-runs
  - feat-hitl
status: covered
---

# Notifications

Notifications surface pipeline events to operators across `/notifications`,
`/settings/email`, `/admin/notification-delivery` and the in-app panel. The
`core/notifier` dispatches outgoing webhooks with HMAC-SHA256 signing, bounded retry, and
dead-letter tracking of final failures; `api/routes/admin_notifications.py` exposes the
notification-delivery log with retry/DLQ admin actions; `event_mapper.py` maps run/HITL
events into typed notification payloads; and in-app notifications stream over SSE.

## Behaviours

- [x] A failure webhook fires on an unhandled node exception with the `run_id` and
      `error_detail`, includes the failed node name and error message, retries up to 3
      times, and auto-disables the endpoint after 10 consecutive failures (logging an
      alert) (`failure_webhook.feature`)
- [x] A HITL webhook fires when a run reaches an approval gate with `run_id`/`gate_id`
      and gate context, retries on failure, and after final failure the event lands on the
      dead-letter queue (`hitl_webhook.feature`)
- [x] Outgoing webhooks are signed with HMAC-SHA256 (per-endpoint secrets) and carry
      `X-Modulo-Signature` / `X-Modulo-Timestamp`, and a receiver can verify the signature
      (`signing.feature`)
- [x] Delivery attempts are recorded against `NotificationDeliveryLog` and are
      manageable from `/admin/notification-delivery` (status/event filters, retry of
      failed deliveries) (`api/routes/admin_notifications.py`,
      `test_admin_notifications_webhooks.py`)
- [x] In-app notifications stream over SSE with a dashboard panel, per-user filters and a
      dismiss flow (`in_app_notifications/*.feature`, `test_in_app_notifications.py`)
- [x] Email delivery settings and message sending are configured under `/settings/email`
      (`core/email_service.py`, `test_admin_email.py`)

### Lifecycle semantics

- [x] **`expires_at` is enforced on every live read (TTL is not decorative).** A
      notification past its `expires_at` — per-event TTLs verified against
      `_EVENT_CONFIG` in `core/notifier/event_mapper.py`: 72h for
      `hitl.awaiting` **and** `run.stalled`, 168h for `run.failed`,
      24h for `hitl.claim_expired`, 336h for `eval.regression` /
      `feedback.pending`, 90d default (`create_notification`) — drops out of the
      dashboard panel, the unread badge
      (`GET /unread-count` and the dashboard's `total_unread`), the inbox
      default (no `status`) and `status=active` — enforced in
      `db/crud/notifications.py` (`_not_expired_clause`), applied to BOTH the
      list and the count so a page's `items` and `total` always agree. The row
      is never deleted: an explicit historical filter (`status=dismissed_self`
      / `dismissed_scope`) and the by-id detail read still retrieve it, so an
      expired notification stops presenting as active/actionable without being
      lost (`test_in_app_notifications_lifecycle.py`, including the
      `expires_at IS NULL` never-expires arm — see the note below on why that
      arm exists despite the ORM model declaring `NOT NULL`).
- [x] **The `status` query param is constrained to its vocabulary.** The
      allowed values are `active`, `dismissed_self`, `dismissed_scope`, or the
      param omitted (the inbox default) — exactly the branches
      `_apply_status_filter` understands and the filter set the
      `/notifications` UI offers. Any other value (including the empty string
      `?status=`, which is *not* "unset") returns **422** from
      `api/routes/in_app_notifications.py` **before** any DB read, instead of
      the previous unfiltered 200 that served every row — expired and
      dismissed — as if live. The CRUD carries the same guard as an `else`
      that raises, so a future caller cannot reintroduce the bypass
      (`test_in_app_notifications_routes.py`,
      `tests/unit/db/crud/test_notifications.py`).
- [x] **Dismissals hide on EVERY live view, including the default inbox.**
      `_hidden_from_user_clause` is applied to the inbox default (no `status`)
      as well as `status=active`, the dashboard panel and the unread count, so
      a `self` dismissal hides the row for its actor and a `scope` dismissal
      hides it for the whole org on all four. `dismissed_self` /
      `dismissed_scope` keep returning the acting user's own history, and the
      by-id detail read is unchanged (documented exception — it applies
      neither the TTL nor the dismissal clause, so a dismissed row stays
      fetchable by its id). List and count share `_apply_status_filter`, so
      `items` and `total` cannot disagree (`test_in_app_notifications_lifecycle.py`).
- [x] **Dismissal scope is honoured in the active filter.**
      `POST /{id}/dismiss` with `dismiss_scope="self"` writes a per-user
      dismissal that hides the row ONLY for the dismissing user;
      `dismiss_scope="scope"` writes a dismissal that hides the row for EVERY
      user in the org (subject to the existing `dismiss_strategy` gate:
      `user_only` refuses, `org_admin` requires an admin, `any_scope` allows
      any member). The active filter (`_hidden_from_user_clause`) excludes a
      row when ANY user has dismissed it at scope, correlated on
      `organisation_id` so a dismissal can never cross a tenant on a backend
      without RLS; `dismissed_self` / `dismissed_scope` remain the record of
      what the acting user dismissed (`test_in_app_notifications_lifecycle.py`).
- [x] **A `self` dismissal can be escalated to `scope`.** A user who filed
      "Review later" (`dismiss_scope="self"`) may then call
      `POST /{id}/dismiss` with `dismiss_scope="scope"`: the existing row is
      **updated** to `scope` in place (the `(notification_id,
      dismissed_by_user_id)` unique constraint means escalation can never
      duplicate the row) instead of being refused as a "concurrent" duplicate
      — which used to make the org-wide hide unreachable for that actor. The
      escalation runs **after** the same `dismiss_strategy`/admin gate that
      governs a fresh scope dismissal, so it cannot be used to sidestep it;
      an identical-scope repeat keeps the pre-existing refusal
      (`test_in_app_notifications_lifecycle.py`,
      `tests/unit/db/crud/test_notifications.py`).
- [x] **No fresh `hitl.awaiting` for an already-terminal run.** The sole
      `EVENT_HITL_AWAITING` call site is `PipelineExecutor._dispatch_hitl_awaiting`
      (`core/pipeline_engine/executor.py`), which funnels through
      `Notifier.dispatch_event` → `_dispatch_inline` — the single choke point
      every emission and every resume/re-dispatch re-entry passes. When the
      linked run's CURRENT status is in `TERMINAL_STATUSES` (operator-cancelled,
      gate-expiry terminalised, failed, …), the dispatch is suppressed entirely
      (no webhook POST, no in-app row) and logged as
      `notifier.hitl_awaiting_suppressed_terminal_run`; a missing run row
      **or a run-status read that raises** (transient DB error — e.g. the
      `runs` table unreadable) fails OPEN (the dispatch proceeds) so a DB blip
      can never swallow a live review request. Other event types are
      unaffected. This is the emission-side fix; the FAR-1234 read-time
      enrichment (`run_status` / `run_terminal` / `run_cancel_reason`) is
      unchanged (`test_hitl_awaiting_terminal_suppression.py`, including the
      read-error fail-open case).

#### Why the `expires_at IS NULL` arm is still there

The ORM model declares `expires_at` `NOT NULL`, and a concurrent change on
PR #1022 removed the `IS NULL` arm as "dead" to satisfy the changed-lines
coverage gate. The model is not the migration history, so the arm stays:

- migration `0003` creates `notifications.expires_at` as **`nullable=True`**;
- the only tightening, in `0110`, is **conditional** —
  `IF ... is_nullable='YES' AND NOT EXISTS (... WHERE expires_at IS NULL) THEN SET NOT NULL` —
  so a database that already holds legacy NULL rows is left NULLable forever;
- the unconditional backfill (`0029_fix_expiry_fields_non_null`, renumbered
  `0057`) was **deleted** by the migration-tree rewrite in #1477, so nothing
  ever backfills those rows;
- `create_notification` gained its 90-day default only in #383 — before that,
  "runtime paths still wrote NULL".

Without the arm, `NULL > now` evaluates to NULL (not TRUE) and those legacy
rows silently disappear from every live view. It costs one `OR` term and is
covered by `test_never_expiring_row_is_present_on_every_live_view`. The
coverage gate is satisfied by that test rather than by deleting the branch.

## Known Gaps

- **In-app notification preference defaults and digest batching** are not surfaced here;
      the SSE panel is the primary in-app surface cited.
- **Webhook payload schemas are not versioned** — receivers depend on the documented
      field set (run_id / error_detail / gate context); there is no webhook payload
      version negotiation.

## QA History
- 2026-09-27: **qa-iterate follow-up pass** — closed six majors found by the
  multi-lens review of the fix above: (F1) `status` is now validated against
  its vocabulary at the route (422 before any DB read) with a fail-closed
  `else` in `_apply_status_filter` as backstop; (F2) `_hidden_from_user_clause`
  now also applies to the default (no `status`) live view; (F3) added coverage
  for the `expires_at IS NULL` never-expires arm; (F4) added coverage for the
  `_run_is_terminal` fail-open-on-DB-error branch (run-status read raising);
  (F5) corrected the TTL table in this document — `run.stalled` is **72h**, not
  168h, verified against `_EVENT_CONFIG` in `core/notifier/event_mapper.py`
  rather than against either side alone; (F6) a `self` dismissal can now be
  escalated to `scope` (row updated in place, same privilege gate, identical-scope
  duplicates still refused). Each new test was proven to fail with its fix
  reverted.

- 2026-09-27: **notification-lifecycle fix pass** — documented the three
  lifecycle semantics now enforced in code: `expires_at` TTL on every live read
  (dashboard / unread count / inbox default / `status=active`, list and count
  kept in agreement), `dismiss_scope` honoured in the active filter (`scope`
  hides for the whole org, `self` stays per-user), and post-terminal
  `hitl_awaiting` suppression at the single `Notifier.dispatch_event` choke
  point (fail-open on an unreadable run). Behaviours verified against
  `db/crud/notifications.py`, `core/notifier/__init__.py` and the new
  `test_in_app_notifications_lifecycle.py` / `test_hitl_awaiting_terminal_suppression.py`
  suites (each proven to fail without its fix). The authoritative
  `frontend/src/manifest.yaml` entry was not modified by this pass (out of the
  delivery's file scope); no existing manifest behaviour text contradicted the
  new semantics.

- 2026-09-12: **product-map review pass** — registered the shared
  plan-entitlement gate surface (`components/FeatureGate.vue` + `LockIcon.vue` static
  testids `feature-gate*` / `lock-icon`) in the manifest `elements:` inventory for `/admin/notification-delivery`, `/settings/email`
  and wired the two components into the reverse testid-coverage guard
  (`test_mapped_route_elements_cover_owning_view_testids`), so the entitlement-card
  surface on those pages stays visible to Assistant's docs indexer / `/api/v1/manifest` and
  can no longer drift unguarded.

- 2026-09-11: **product-map review pass** — extended the reverse
  testid-coverage guard (`test_mapped_route_elements_cover_owning_view_testids`) to
  `/notifications`: the whole-page view(s) `NotificationsPage.vue` render static `data-testid`s that the
  product map `elements:` inventory already documents, but the surface was not yet
  guarded against drift. The route now maps to its owning view so a newly shipped
  testid can no longer silently stay invisible to Assistant's docs indexer /
  `/api/v1/manifest`.

- 2026-08-27: **product-map review pass** — added this behaviour-tracker
  for the registered manifest feature `feat-notifications`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `core/notifier`,
  `api/routes/admin_notifications.py` and the notifications BDD/unit suites.
  Status: covered.
