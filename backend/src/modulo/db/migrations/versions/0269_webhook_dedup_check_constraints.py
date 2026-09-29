"""improve-database(webhook): dedup hash shape + TTL ordering CHECKs.

Revision ID: 0269_webhook_dedup_check_constraints
Revises: 0268_webhook_lookup_expiry_indexes
Create Date: 2026-09-29

Covers ``backend/src/modulo/db/models/webhook.py`` (``webhook_dedup_hashes``,
``webhook_payloads``), same area as 0268. Three data-integrity gaps, each
with an invariant provable from the write paths:

* ``ck_webhook_dedup_hash_length`` — every write site stores
  ``sha256_hex(raw_body)`` (``api/routes/webhooks.py`` ×3,
  ``core/trigger_engine`` dedup insert), always 64 hex chars. Nothing at
  the DB level rejects a truncated/non-hash string. Non-nullable column,
  so a plain ``char_length(payload_hash) = 64`` CHECK is safe.
* ``ck_webhook_dedup_expires_ordering`` — dedup rows are written with
  ``expires_at = now() + _DEDUP_TTL_SECONDS`` (positive TTL), so
  ``expires_at > created_at`` always holds; a clock-skew or caller bug
  writing an already-expired TTL would silently disable dedup.
* ``ck_webhook_payload_expires_ordering`` — payload rows are written with
  ``expires_at = now() + _DEDUP_TTL_SECONDS + 3600`` (``trigger_engine``),
  same ordering invariant for the replay store.

Deploy-safety: every CHECK is added ``NOT VALID`` then ``VALIDATE``-d,
mirroring ``0151_fix_constraints`` / ``0165_add_check_constraints``. The ADD
takes only a brief lock and does NOT scan existing rows, so a populated
table never aborts the upgrade; the VALIDATE is online. The app only ever
writes in-set values, so VALIDATE succeeds in practice.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0269_webhook_dedup_check_constraints"
down_revision: str | None = "0268_webhook_lookup_expiry_indexes"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# (add_sql, validate_sql) — each pair added NOT VALID then VALIDATE-d.
# Literal SQL per constraint (no f-string interpolation): the semgrep
# raw-sql-fstring / migration-fstring-sql rules reject interpolated DDL.
_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ALTER TABLE webhook_dedup_hashes ADD CONSTRAINT ck_webhook_dedup_hash_length CHECK (char_length(payload_hash) = 64) NOT VALID",
        "ALTER TABLE webhook_dedup_hashes VALIDATE CONSTRAINT ck_webhook_dedup_hash_length",
    ),
    (
        "ALTER TABLE webhook_dedup_hashes ADD CONSTRAINT ck_webhook_dedup_expires_ordering CHECK (expires_at > created_at) NOT VALID",
        "ALTER TABLE webhook_dedup_hashes VALIDATE CONSTRAINT ck_webhook_dedup_expires_ordering",
    ),
    (
        "ALTER TABLE webhook_payloads ADD CONSTRAINT ck_webhook_payload_expires_ordering CHECK (expires_at > created_at) NOT VALID",
        "ALTER TABLE webhook_payloads VALIDATE CONSTRAINT ck_webhook_payload_expires_ordering",
    ),
)


def upgrade() -> None:
    # CHECK constraints: add NOT VALID (no row scan at ADD time) then VALIDATE
    # online, so a populated table never aborts the upgrade.
    for add_sql, validate_sql in _CHECKS:
        op.execute(text(add_sql))
        op.execute(text(validate_sql))


def downgrade() -> None:
    op.drop_constraint("ck_webhook_payload_expires_ordering", "webhook_payloads", type_="check")
    op.drop_constraint("ck_webhook_dedup_expires_ordering", "webhook_dedup_hashes", type_="check")
    op.drop_constraint("ck_webhook_dedup_hash_length", "webhook_dedup_hashes", type_="check")
