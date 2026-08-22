"""Relax the registry arm of ck_library_primitives_source_fields (FAR-363).

Revision ID: 0123_relax_registry_signature_check
Revises: 0122_library_sync_state
Create Date: 2026-08-22

Community-library installs write ``source='registry'`` rows with a NULL
``ed25519_signature`` — the signed community manifest (``library_sync_state``)
covers integrity, not a per-row signature. The registry arm of
``ck_library_primitives_source_fields`` still demanded ``ed25519_signature
IS NOT NULL``, so those installs were rejected at insert.

This migration drops the signature requirement from the registry arm and
makes ``average_rating``/``review_count`` optional (the community-install
constructor writes them as NULL), while keeping ``owner_team_id IS NULL``
(registry entries stay org-owned). All other clauses (``source_url``/
``checksum``/``verified``/``download_count`` NOT NULL, ``forked_from IS NULL``,
``visibility = 'org'``) are unchanged.

The DROP/ADD is Postgres-only, matching the base-table reconciliation
migrations (0108/0109/0110): MariaDB is deprecated and SQLite relies on
app-level filtering rather than DB CHECK enforcement.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0123_relax_registry_signature_check"
down_revision: str | None = "0122_library_sync_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_library_primitives_source_fields"

_ORIGINAL = (
    "(source = 'local' AND source_url IS NULL AND checksum IS NULL "
    "AND ed25519_signature IS NULL AND verified IS NULL "
    "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
    "OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL "
    "AND ed25519_signature IS NULL AND verified IS NULL "
    "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
    "OR (source = 'community' AND source_url IS NULL AND checksum IS NULL "
    "AND ed25519_signature IS NULL "
    "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
    "OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org' "
    "AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL "
    "AND ed25519_signature IS NOT NULL AND verified IS NOT NULL "
    "AND download_count IS NOT NULL AND average_rating IS NOT NULL "
    "AND review_count IS NOT NULL)"
)

_RELAXED = (
    "(source = 'local' AND source_url IS NULL AND checksum IS NULL "
    "AND ed25519_signature IS NULL AND verified IS NULL "
    "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
    "OR (source = 'modulo' AND source_url IS NULL AND checksum IS NULL "
    "AND ed25519_signature IS NULL AND verified IS NULL "
    "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
    "OR (source = 'community' AND source_url IS NULL AND checksum IS NULL "
    "AND ed25519_signature IS NULL "
    "AND download_count IS NULL AND average_rating IS NULL AND review_count IS NULL) "
    "OR (source = 'registry' AND owner_team_id IS NULL AND visibility = 'org' "
    "AND forked_from IS NULL AND source_url IS NOT NULL AND checksum IS NOT NULL "
    "AND verified IS NOT NULL "
    "AND download_count IS NOT NULL)"
)


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _swap(check_body: str) -> None:
    op.execute(f"ALTER TABLE library_primitives DROP CONSTRAINT {_CONSTRAINT}")
    op.execute(f"ALTER TABLE library_primitives ADD CONSTRAINT {_CONSTRAINT} CHECK ({check_body})")


def upgrade() -> None:
    if _is_postgres():
        _swap(_RELAXED)


def downgrade() -> None:
    if _is_postgres():
        _swap(_ORIGINAL)
