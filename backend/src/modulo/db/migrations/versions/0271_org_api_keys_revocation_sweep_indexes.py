"""improve-database(api_key): revocation + stale-sweep lookup indexes.

Revision ID: 0271_org_api_keys_revocation_sweep_indexes
Revises: 0270_pipeline_snapshots_max_autonomy_ge_default
Create Date: 2026-09-30

Covers ``backend/src/modulo/db/models/api_key.py`` (``org_api_keys``), an
area with no prior improve-database visit. Existing coverage is single-column
only (0005/0108/0110/0114/0142/0164/0181): ``(organisation_id)``,
``(team_id)``, ``(account_id)``, ``(run_id)`` plus the global
``uq_org_api_keys_lookup_prefix`` unique and the ``role``/``scope`` CHECKs.
All FK constraints are present (org CASCADE, account RESTRICT, team CASCADE,
run SET NULL). Two read paths still filter on column pairs with no composite
to serve them:

* ``ix_org_api_keys_live_run_keys`` — the per-run revocation UPDATE
  (``auth/api_key.py::revoke_run_api_key``,
  ``organisation_id = $1 AND run_id = $2 AND revoked_at IS NULL``) and the
  compensating sweep (``revoke_run_api_key_sweep``,
  ``organisation_id = $1 AND run_id IS NOT NULL AND revoked_at IS NULL``
  joined to ``runs``) ran on a bitmap-AND of the single-column org/run_id
  indexes over every live key in the org. The partial predicate matches both
  shapes (equality on ``run_id`` implies ``IS NOT NULL``).
* ``ix_org_api_keys_stale_sweep`` — the housekeeping stale-key scan
  (``core/housekeeping.py::_scan_stale_api_keys``,
  ``organisation_id = $1 AND revoked_at IS NULL AND
  (last_used_at IS NULL OR last_used_at < $2)``) had no ``last_used_at``
  index at all and full-scanned the org's live keys.

The per-request auth lookup (``validate_api_key``,
``lookup_prefix = $1 AND revoked_at IS NULL``) needs nothing: the global
unique on ``lookup_prefix`` already pinpoints a single row. No expiry index:
``expires_at`` is checked in Python post-fetch, never in a DB sweep predicate.

Additive indexes only — no column/table changes. Same
``CREATE INDEX IF NOT EXISTS`` pattern as 0128/0155/0267 (Alembic wraps each
revision in a transaction, so ``CONCURRENTLY`` is unavailable).
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0271_org_api_keys_revocation_sweep_indexes"
down_revision: str | None = "0270_pipeline_snapshots_max_autonomy_ge_default"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_INDEXES = [
    (
        "ix_org_api_keys_live_run_keys",
        (
            "CREATE INDEX IF NOT EXISTS ix_org_api_keys_live_run_keys "
            'ON public."org_api_keys" (organisation_id, run_id) '
            "WHERE revoked_at IS NULL AND run_id IS NOT NULL;"
        ),
    ),
    (
        "ix_org_api_keys_stale_sweep",
        (
            "CREATE INDEX IF NOT EXISTS ix_org_api_keys_stale_sweep "
            'ON public."org_api_keys" (organisation_id, last_used_at) '
            "WHERE revoked_at IS NULL;"
        ),
    ),
]


_DROPS = [
    "DROP INDEX IF EXISTS ix_org_api_keys_live_run_keys;",
    "DROP INDEX IF EXISTS ix_org_api_keys_stale_sweep;",
]


def upgrade() -> None:
    bind = op.get_bind()
    for _name, stmt in _INDEXES:
        bind.execute(text(stmt))


def downgrade() -> None:
    bind = op.get_bind()
    for stmt in _DROPS:
        bind.execute(text(stmt))
