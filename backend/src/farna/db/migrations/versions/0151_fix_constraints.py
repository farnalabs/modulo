"""improve-database: missing CHECK constraints + partial-unique org slug.

Adds data-integrity CHECK constraints identified as genuine remaining gaps in
the SQLAlchemy models (enum/vocabulary columns whose allowed set is knowable
from code/comments, and ledger/count numerics that must be non-negative).
None of these constraints exist yet on the deployed schema (verified against
migrations 0001-0150).

Also converts ``organisations.slug`` from a plain UNIQUE constraint into a
partial UNIQUE index ``WHERE deleted_at IS NULL`` so a soft-deleted
organisation's slug can be reused (the table is soft-deleted manually via
``status='deleted'`` + ``deleted_at``).

Deploy-safety notes:
* Every CHECK is added ``NOT VALID`` then ``VALIDATE``-d, so a populated table
  never aborts the upgrade if a historical row violates the constraint (the
  ADD takes only a brief lock and the VALIDATE is online). Columns are NOT
  NULL and the app only ever writes in-set values, so VALIDATE succeeds in
  practice.
* The slug drop is guarded (``IF EXISTS``) so a DB whose unique was created
  under a different name (e.g. via ``create_all``) does not hard-fail.
* The downgrade re-creates the full ``organisations_slug_key`` UNIQUE after
  de-duplicating rows that share a slug — mirroring 0127. After the upgrade's
  own use-case (active + soft-deleted rows on one slug) the un-guarded
  re-add would raise a unique-violation and abort the downgrade, exactly the
  case 0127 documents.

Revision ID: 0151_fix_constraints
Revises: 0150_add_router_no_match_status
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0151_fix_constraints"
down_revision: str | None = "0150_add_router_no_match_status"
branch_labels: str | None = None
depends_on: str | None = None

# (table, constraint_name, check_expression) — added NOT VALID then VALIDATE-d.
_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("run_evidence", "ck_run_evidence_state", "evidence_state IN ('has_work','verified_empty','unverifiable')"),
    ("pipeline_snapshots", "ck_pipeline_snapshots_version_kind", "version_kind IN ('edit','run','draft')"),
    ("pipeline_snapshots", "ck_pipeline_snapshots_created_kind", "created_kind IN ('initial','edit','rollback','run')"),
    ("pipeline_snapshots", "ck_pipeline_snapshots_channel", "channel IN ('none','stable','canary')"),
    ("oauth_authorization_codes", "ck_oauth_auth_codes_challenge_method", "code_challenge_method = 'S256'"),
    ("organisations", "ck_organisations_cum_spend", "org_cumulative_spend_cents >= 0"),
    ("org_daily_run_counts", "ck_org_daily_run_counts_run_count", "run_count >= 0"),
    ("org_daily_run_counts", "ck_org_daily_run_counts_total_spend", "total_spend_usd >= 0"),
    ("org_daily_run_counts", "ck_org_daily_run_counts_refused_spend", "refused_spend_usd >= 0"),
    ("spend_anomalies", "ck_spend_anomalies_amount", "amount >= 0"),
    ("spend_anomalies", "ck_spend_anomalies_baseline", "baseline >= 0"),
    ("library_primitives", "ck_library_primitives_dl_count", "download_count IS NULL OR download_count >= 0"),
    ("library_primitives", "ck_library_primitives_review_count", "review_count IS NULL OR review_count >= 0"),
    (
        "suite_runs",
        "ck_suite_runs_case_counts",
        "total_cases >= 0 AND passed_cases >= 0 AND failed_cases >= 0 AND excluded_case_count >= 0",
    ),
)


def upgrade() -> None:
    # 1-14. CHECK constraints: add NOT VALID (no row scan at ADD time) then
    # VALIDATE online, so a populated table never aborts the upgrade.
    for table, name, expr in _CHECKS:
        op.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr}) NOT VALID"))
        op.execute(text(f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}"))

    # organisations.slug: plain UNIQUE -> partial UNIQUE (deleted_at IS NULL).
    # Guarded drop: a DB built via create_all names this index uq_organisations_slug.
    op.execute(text("ALTER TABLE organisations DROP CONSTRAINT IF EXISTS organisations_slug_key"))
    op.create_index(
        "uq_organisations_slug",
        "organisations",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_organisations_slug", table_name="organisations")

    bind = op.get_bind()
    # De-duplicate rows sharing a slug before restoring the full UNIQUE. After
    # upgrade an active row and one or more soft-deleted rows may coexist on the
    # same slug (the documented slug-reuse flow); re-adding the full constraint
    # on that state would raise a unique-violation and abort the downgrade. Keep
    # the active row when one exists, else the most recently deleted row.
    de_dup = (
        "DELETE FROM organisations WHERE id IN ("
        "SELECT id FROM ("
        "SELECT id, ROW_NUMBER() OVER (PARTITION BY slug "
        "ORDER BY (deleted_at IS NULL) DESC, deleted_at DESC NULLS LAST) AS rn "
        "FROM organisations) sub WHERE rn > 1)"
    )
    bind.execute(text(de_dup))
    op.create_unique_constraint("organisations_slug_key", "organisations", ["slug"])

    for table, name, _expr in _CHECKS:
        op.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
