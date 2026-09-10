"""Create the ORM-declared ``deleted_defaults`` signal CHECK (FAR-644).

Revision ID: 0206_deleted_defaults_signal_check
Revises: 0205_library_collection_type
Create Date: 2026-09-10

The ORM declares ``ck_deleted_defaults_signal_nonempty`` (``signal <> ''`` on
``deleted_defaults``) but no migration ever created it — flagged as ORM↔DB
drift by the schema-parity test and tracked as FAR-644.

``DeletedDefault`` tombstones are keyed per ``(organisation_id, signal)``:
``restore_defaults`` skips every tombstoned signal by exact-name match, so an
empty-string signal could only ever be garbage (it would never match a real
default rule's signal name and would permanently shadow nothing). The column
is already NOT NULL (0108); the CHECK adds the missing strictness so the DB
agrees with the ORM declaration. The app only ever writes real signal names,
so the guard can never reject a legitimate write.

Deploy-safety: added ``NOT VALID`` then ``VALIDATE``-d, mirroring
0165_add_check_constraints and 0157_add_numeric_check_constraints.
``ADD CONSTRAINT ... CHECK (...) NOT VALID`` takes only a brief
``AccessExclusive`` lock and does NOT scan existing rows. Note that the
``VALIDATE`` runs unconditionally: if a historical row held an empty signal,
``VALIDATE`` would fail and abort the migration chain. That is accepted
here because no such row can plausibly exist — the only writer of
``deleted_defaults`` has zero source callers (tombstones are effectively
dead code paths in the current release) and every seeded signal value is
non-empty — but any future code path that writes an empty signal must fix
its data before this CHECK can roll out.

Future re-materialisation of the ``nodes`` table: the ``nodes.id`` FKs this
ticket stripped from the ORM (snapshot_schema_pin, node_observation,
eval_result, eval_definition, pipeline_edge, run_evidence) must be
RE-DECLARED deliberately at that point — re-adding
``ForeignKey("nodes.id")`` to satisfy the parity test would be a parity
finding, not a fix. See node.py's DEPRECATED note.
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0206_deleted_defaults_signal_check"
down_revision: str | None = "0205_library_collection_type"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "deleted_defaults"
_CHECK_NAME = "ck_deleted_defaults_signal_nonempty"
_CHECK_EXPRESSION = "signal <> ''"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite test backend builds its schema from the ORM model, so the
        # constraint appears automatically; this branch guards parity only
        # (same rationale as 0141's non-Postgres branch).
        return

    # Idempotent like 0157's pg_constraint guard, but state-aware: a run
    # interrupted between ADD CONSTRAINT NOT VALID and VALIDATE must replay
    # as VALIDATE (not a no-op), otherwise the constraint would stay NOT
    # VALID forever, never enforcing on ~real writes.
    existing = bind.execute(
        text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :name AND conrelid = 'public.deleted_defaults'::regclass"
        ),
        {"name": _CHECK_NAME},
    ).scalar_one_or_none()
    if existing is True:
        return
    if existing is None:
        op.execute(
            text(f'ALTER TABLE public."{_TABLE}" ADD CONSTRAINT {_CHECK_NAME} CHECK ({_CHECK_EXPRESSION}) NOT VALID')
        )
    op.execute(text(f'ALTER TABLE public."{_TABLE}" VALIDATE CONSTRAINT {_CHECK_NAME}'))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(text(f'ALTER TABLE public."{_TABLE}" DROP CONSTRAINT IF EXISTS {_CHECK_NAME};'))
