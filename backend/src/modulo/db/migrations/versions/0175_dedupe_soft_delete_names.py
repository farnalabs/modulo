"""Dedupe live (organisation_id, name) rows before 0156's partial unique indexes.

Revision ID: 0175_dedupe_soft_delete_names
Revises: 0155_add_hot_query_indexes
Create Date: 2026-09-03

Migration 0156 creates partial unique indexes ``(organisation_id, name)
WHERE deleted_at IS NULL`` on six SoftDeleteMixin tables. On databases where
no prior constraint ever enforced that key among live rows, real deployments
can already contain duplicates, so 0156's ``CREATE UNIQUE INDEX`` fails and
the whole (single-transaction) upgrade is blocked. First seen on the
farnalabs production deployment on 2026-09-03: three live pipelines named
'Agent Echo' in one organisation, created within 21 minutes on 2026-07-15.

Because this product is self-hosted, the cleanup must run BEFORE 0156 for
every deployment stuck the same way - so this migration is spliced into the
chain (0155 -> 0175 -> 0156) rather than appended at the tail. A deployment
sitting at 0152 will run 0155, then this dedupe, then 0156's index creation.

Deterministic rule: for each table, keep the OLDEST live row per
(organisation_id, name) (``created_at ASC, id ASC`` tiebreak) and soft-delete
every other live row by setting ``deleted_at = now()``. Rows are never
DELETEd - the data is preserved and the soft-delete model stays intact.
"""

from __future__ import annotations

import logging

from alembic import op
from sqlalchemy import text

logger = logging.getLogger(__name__)

revision: str = "0175_dedupe_soft_delete_names"
down_revision: str | None = "0155_add_hot_query_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLES = [
    "pipelines",
    "variant_groups",
    "saved_views",
    "environment_profiles",
    "composite_templates",
    "lifecycle_maps",
]


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        result = bind.execute(
            text(
                f'UPDATE public."{table}" SET deleted_at = now() '  # noqa: S608  # nosec B608
                "WHERE deleted_at IS NULL AND id IN ("
                "    SELECT id FROM ("
                "        SELECT id, ROW_NUMBER() OVER ("
                "            PARTITION BY organisation_id, name"
                "            ORDER BY created_at ASC, id ASC"
                "        ) AS rn"
                f'        FROM public."{table}"'
                "        WHERE deleted_at IS NULL"
                "    ) ranked"
                "    WHERE ranked.rn > 1"
                ");"
            )
        )
        logger.info("0175 dedupe: %s: soft-deleted %d duplicate live row(s)", table, result.rowcount)


def downgrade() -> None:
    # Deliberate no-op: which live row was "the duplicate" is unknowable after
    # the fact, so soft-deleted duplicates cannot be safely un-deleted. The
    # indexes 0156 creates are dropped by 0156's own downgrade.
    pass
