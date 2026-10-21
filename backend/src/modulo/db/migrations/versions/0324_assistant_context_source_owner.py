"""Fix the ``assistant_context_sources`` owner CHECK and org-default uniqueness.

Revision ID: 0324_assistant_context_source_owner
Revises: 0323_pipeline_snapshots_substantive_version_index
Create Date: 2026-10-21

Two defects in the context-source config table, both reachable from the
``/me/assistant/context-sources`` and admin assistant endpoints:

1. **The owner CHECK rejects every real user override.** ``0005`` created
   ``ck_remy_context_sources_owner`` as an XOR — ``(organisation_id IS NOT NULL
   AND account_id IS NULL) OR (organisation_id IS NULL AND account_id IS NOT
   NULL)`` — and ``0257`` renamed it. The service that owns the table
   (``AssistantContextSourceService._upsert_context_source``) writes
   ``organisation_id=<org>`` AND ``account_id=<member>`` for a member override,
   and both the read path (``_query_by_user_id``) and the tests assert that
   shape. Every ``PUT /me/assistant/context-sources/{key}`` therefore raised
   ``IntegrityError``; the route-level tests missed it because they mock the
   service. Rows are organisation-scoped by design: ``account_id IS NULL`` is
   the org default, a set ``account_id`` is a member override *inside that
   org*. The replacement CHECK is ``organisation_id IS NOT NULL``.

   Legacy org-less rows (the XOR permitted them) are unreachable by the
   service, which always filters ``organisation_id``. The new constraint is
   therefore added ``NOT VALID`` when such rows exist — new writes are enforced
   immediately, old rows are left intact rather than deleted — and validated
   normally when the table is clean.

2. **Org-default rows had no working uniqueness.** ``uq_assistant_context_sources_key``
   is a three-column UNIQUE over ``(organisation_id, account_id, source_key)``;
   Postgres treats NULLs as distinct, so it never constrains the org-default
   rows (``account_id IS NULL``). Two concurrent/duplicate upserts could insert
   duplicate defaults, after which ``scalar_one_or_none()`` in the upsert raises
   ``MultipleResultsFound`` for that org permanently. This migration first
   de-duplicates the existing rows (keeping the most recently updated mode per
   ``(organisation_id, source_key)``) and then adds the partial unique index
   ``uq_assistant_context_sources_org_key_default`` that the ORM model declares.

Postgres-only DDL: the SQLite/MariaDB test schemas get both the CHECK and the
partial unique index from the model via ``create_all`` (the 0246 / 0250 / 0294
precedent). ``downgrade`` restores the legacy XOR CHECK as ``NOT VALID`` (the
rows written since this migration would violate it) and drops the index; the
de-duplication is intentionally not reversed.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0324_assistant_context_source_owner"
down_revision: str | None = "0323_pipeline_snapshots_substantive_version_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "assistant_context_sources"
_OWNER_CHECK = "ck_assistant_context_sources_owner"
_DEFAULT_UNIQUE = "uq_assistant_context_sources_org_key_default"

# Keep the newest row per (organisation_id, source_key) among the org defaults;
# ties break on id so the statement is deterministic. Only the rows the broken
# three-column UNIQUE could not protect are touched.
_DEDUPE_SQL = (
    "DELETE FROM public.assistant_context_sources a "
    "USING public.assistant_context_sources b "
    "WHERE a.account_id IS NULL AND b.account_id IS NULL "
    "AND a.organisation_id = b.organisation_id "
    "AND a.source_key = b.source_key "
    "AND (a.updated_at, a.id) < (b.updated_at, b.id)"
)
_NULL_ORG_COUNT_SQL = "SELECT count(*) FROM public.assistant_context_sources WHERE organisation_id IS NULL"
_ADD_OWNER_CHECK_SQL = (
    "ALTER TABLE public.assistant_context_sources "
    "ADD CONSTRAINT ck_assistant_context_sources_owner CHECK (organisation_id IS NOT NULL)"
)
_ADD_OWNER_CHECK_UNVALIDATED_SQL = _ADD_OWNER_CHECK_SQL + " NOT VALID"
_ADD_LEGACY_CHECK_UNVALIDATED_SQL = (
    "ALTER TABLE public.assistant_context_sources "
    "ADD CONSTRAINT ck_assistant_context_sources_owner CHECK ("
    "(organisation_id IS NOT NULL AND account_id IS NULL) OR "
    "(organisation_id IS NULL AND account_id IS NOT NULL)) NOT VALID"
)
_CREATE_DEFAULT_UNIQUE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_assistant_context_sources_org_key_default "
    "ON public.assistant_context_sources (organisation_id, source_key) "
    "WHERE account_id IS NULL"
)


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def upgrade() -> None:
    if not _is_postgres():
        return
    op.execute(text(_DEDUPE_SQL))
    op.execute(
        text(
            'ALTER TABLE public.assistant_context_sources DROP CONSTRAINT IF EXISTS "ck_assistant_context_sources_owner"'
        )
    )
    has_legacy_rows = bool(op.get_bind().execute(text(_NULL_ORG_COUNT_SQL)).scalar())
    op.execute(text(_ADD_OWNER_CHECK_UNVALIDATED_SQL if has_legacy_rows else _ADD_OWNER_CHECK_SQL))
    op.execute(text(_CREATE_DEFAULT_UNIQUE_SQL))


def downgrade() -> None:
    if not _is_postgres():
        return
    op.execute(text("DROP INDEX IF EXISTS uq_assistant_context_sources_org_key_default"))
    op.execute(
        text(
            'ALTER TABLE public.assistant_context_sources DROP CONSTRAINT IF EXISTS "ck_assistant_context_sources_owner"'
        )
    )
    op.execute(text(_ADD_LEGACY_CHECK_UNVALIDATED_SQL))
