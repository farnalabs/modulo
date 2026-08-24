"""Rename remy ``user_id`` FK columns to ``account_id`` (FK -> ``accounts.id``).

Revision ID: 0136_rename_remy_user_id_to_account_id
Revises: 0135_status_check_constraints
Create Date: 2026-08-24

The three Remy tables (``chat_sessions``, ``remy_skills``, ``remy_context_sources``)
consistently name the foreign key to ``accounts.id`` as ``user_id``, while the
rest of the codebase uses ``account_id``. This migration standardises them:

- ``chat_sessions.user_id`` -> ``chat_sessions.account_id``
- ``remy_skills.user_id`` -> ``remy_skills.account_id``
- ``remy_context_sources.user_id`` -> ``remy_context_sources.account_id``

For each table the migration drops the FK constraint / index / unique + check
constraints / tenant trigger that referenced ``user_id``, renames the column,
then recreates each object against ``account_id`` with the new naming:
``ix_*_user_id`` -> ``ix_*_account_id`` and ``trg_*_user_id_tenant`` ->
``trg_*_account_id_tenant``. The Postgres tenant trigger also passes
``'account_id'`` (rather than ``'user_id'``) to ``enforce_same_organisation``,
which resolves the column by name at trigger runtime.

Postgres is the production dialect and is handled with explicit ``ALTER TABLE``
DDL. Non-Postgres dialects (SQLite) are handled with ``batch_alter_table`` so the
column rename and dependent constraint/index rebuild keep SQLAlchemy table-recreate
semantics; the SQLite test backend builds its schema from the ORM model, so this
branch mainly guards parity. ``remy_context_sources`` never had a ``user_id``
index, so none is created for it.
"""

from __future__ import annotations

from alembic import op

revision: str = "0136_rename_remy_user_id_to_account_id"
down_revision: str | None = "0135_status_check_constraints"
branch_labels: str | None = None
depends_on: str | None = None


class _TableSpec:
    """Per-table rename recipe shared by the Postgres and SQLite paths."""

    def __init__(
        self,
        table: str,
        fk_old: str,
        fk_new: str,
        fk_cols: list[str],
        index_old: str | None,
        index_new: str | None,
        index_cols: list[str],
        uniques: list[tuple[str, list[str]]],
        checks: list[tuple[str, str]],
        trigger_old: str,
        trigger_new: str,
        owner_table: str,
    ) -> None:
        self.table = table
        self.fk_old = fk_old
        self.fk_new = fk_new
        self.fk_cols = fk_cols
        self.index_old = index_old
        self.index_new = index_new
        self.index_cols = index_cols
        self.uniques = uniques
        self.checks = checks
        self.trigger_old = trigger_old
        self.trigger_new = trigger_new
        self.owner_table = owner_table


_SPECS: list[_TableSpec] = [
    _TableSpec(
        table="chat_sessions",
        fk_old="chat_sessions_user_id_fkey",
        fk_new="chat_sessions_account_id_fkey",
        fk_cols=["account_id"],
        index_old="ix_chat_sessions_user_id",
        index_new="ix_chat_sessions_account_id",
        index_cols=["account_id"],
        uniques=[("uq_chat_sessions_user_session_number", ["account_id", "session_number"])],
        checks=[],
        trigger_old="trg_chat_sessions_user_id_tenant",
        trigger_new="trg_chat_sessions_account_id_tenant",
        owner_table="accounts",
    ),
    _TableSpec(
        table="remy_skills",
        fk_old="remy_skills_user_id_fkey",
        fk_new="remy_skills_account_id_fkey",
        fk_cols=["account_id"],
        index_old="ix_remy_skills_user_id",
        index_new="ix_remy_skills_account_id",
        index_cols=["account_id"],
        uniques=[],
        checks=[
            (
                "ck_remy_skills_owner",
                "(organisation_id IS NOT NULL AND account_id IS NULL) OR "
                "(organisation_id IS NULL AND account_id IS NOT NULL)",
            )
        ],
        trigger_old="trg_remy_skills_user_id_tenant",
        trigger_new="trg_remy_skills_account_id_tenant",
        owner_table="accounts",
    ),
    _TableSpec(
        table="remy_context_sources",
        fk_old="remy_context_sources_user_id_fkey",
        fk_new="remy_context_sources_account_id_fkey",
        fk_cols=["account_id"],
        index_old=None,
        index_new=None,
        index_cols=[],
        uniques=[("uq_remy_context_sources_key", ["organisation_id", "account_id", "source_key"])],
        checks=[
            (
                "ck_remy_context_sources_owner",
                "(organisation_id IS NOT NULL AND account_id IS NULL) OR "
                "(organisation_id IS NULL AND account_id IS NOT NULL)",
            )
        ],
        trigger_old="trg_remy_context_sources_user_id_tenant",
        trigger_new="trg_remy_context_sources_account_id_tenant",
        owner_table="accounts",
    ),
]


def _upgrade_postgres() -> None:
    for spec in _SPECS:
        # 1. Drop objects that referenced user_id so the rename is unambiguous
        #    and we can recreate them against account_id with new names.
        op.execute(f'ALTER TABLE public."{spec.table}" DROP CONSTRAINT IF EXISTS "{spec.fk_old}";')
        if spec.index_old:
            op.execute(f'DROP INDEX IF EXISTS public."{spec.index_old}";')
        for name, _cols in spec.uniques:
            op.execute(f'ALTER TABLE public."{spec.table}" DROP CONSTRAINT IF EXISTS "{name}";')
        for name, _expr in spec.checks:
            op.execute(f'ALTER TABLE public."{spec.table}" DROP CONSTRAINT IF EXISTS "{name}";')
        op.execute(f'DROP TRIGGER IF EXISTS "{spec.trigger_old}" ON public."{spec.table}";')

        # 2. Rename the column.
        op.execute(f'ALTER TABLE public."{spec.table}" RENAME COLUMN "user_id" TO "account_id";')

        # 3. Recreate the FK (new name, references account_id).
        cols = ", ".join(f'"{c}"' for c in spec.fk_cols)
        op.execute(
            f'ALTER TABLE public."{spec.table}" ADD CONSTRAINT "{spec.fk_new}" '
            f"FOREIGN KEY ({cols}) REFERENCES public.accounts (id) ON DELETE CASCADE;"
        )

        # 4. Recreate the lookup index.
        if spec.index_new and spec.index_cols:
            index_cols = ", ".join(f'"{c}"' for c in spec.index_cols)
            op.execute(
                f'CREATE INDEX IF NOT EXISTS "{spec.index_new}" ON public."{spec.table}" USING btree ({index_cols});'
            )

        # 5. Recreate unique + check constraints against account_id.
        for name, cols_list in spec.uniques:
            constraint_cols = ", ".join(f'"{c}"' for c in cols_list)
            op.execute(f'ALTER TABLE public."{spec.table}" ADD CONSTRAINT "{name}" UNIQUE ({constraint_cols});')
        for name, expr in spec.checks:
            op.execute(f'ALTER TABLE public."{spec.table}" ADD CONSTRAINT "{name}" CHECK ({expr});')

        # 6. Recreate the tenant trigger (column reference + function arg updated).
        op.execute(
            f'CREATE TRIGGER "{spec.trigger_new}" '
            f"BEFORE INSERT OR UPDATE OF account_id, organisation_id "
            f'ON public."{spec.table}" FOR EACH ROW '
            f"EXECUTE FUNCTION public.enforce_same_organisation('{spec.owner_table}', 'account_id');"
        )


def _upgrade_other() -> None:
    """Non-Postgres (SQLite) path — batch_alter_table table-recreate semantics.

    SQLAlchemy's ``batch_alter_table`` automatically propagates a column rename to
    dependent foreign keys, unique constraints, and indexes (so those all
    reference ``account_id`` after the rename, and the lookup-index columns follow
    the renamed column). It does NOT rewrite the text of CHECK constraints, so
    those are dropped and recreated explicitly against ``account_id``.
    """
    for spec in _SPECS:
        with op.batch_alter_table(spec.table) as batch:
            for name, _expr in spec.checks:
                batch.drop_constraint(name, type_="check")
            batch.alter_column("user_id", new_column_name="account_id")
            for name, expr in spec.checks:
                batch.create_check_constraint(name, expr)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgres()
    else:
        _upgrade_other()


def downgrade() -> None:
    # Remy tables consistently use account_id after this migration. Downgrading is
    # intentionally not implemented: the rename is part of the canonical schema and
    # reverting it would require reversing every FK/index/constraint/trigger, which
    # this reconciliation migration deliberately does not encode.
    pass
