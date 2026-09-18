"""SuiteRun entity + eval_results attribution (FAR-376 Phase 3).

Revision ID: 0137_eval_suite_run
Revises: 0136_rename_remy_user_id_to_account_id
Create Date: 2026-08-24

Phase 3 of the Generic Eval Product MVP: the run/comparison entity that closes
the flywheel. It REUSES the Phase 1 (``eval_suites``) + Phase 2
(``eval_datasets`` / ``eval_cases``) entities and the existing ``eval_results``
outcome table rather than building a parallel comparison store.

What this migration does:

* ``suite_runs`` — one execution of an ``EvalSuite`` against a pinned
  ``EvalDataset`` snapshot. Snapshots the dataset version + definition checksum
  + scenario signature at creation (immutable) so a changed contract produces a
  NEW baseline tuple instead of corrupting a prior one. ``baseline_tuple`` is
  the immutable comparison key; ``state`` drives a guarded state machine; a
  ``version`` column provides the optimistic lock.
* ``eval_results.suite_run_id`` — back-link the per-case outcomes to the
  ``SuiteRun`` they belong to (nullable, backfilled NULL; legacy pipeline-path
  rows stay NULL). ``eval_results.run_id`` is relaxed to NULL so a SuiteRun
  outcome is attributed to a ``suite_run`` instead of a pipeline ``Run``.
* RLS — ENABLE + FORCE ROW LEVEL SECURITY + ``rls_org_isolation`` on
  ``suite_runs`` (owner ``modulo_migrate``), same ceremony as the other eval
  tables. The ``OrgScoped`` mixin alone is insufficient: the app role must not
  be able to read a cross-org suite run.
* Covering indexes for the baseline-resolution and comparison queries.

ROLE WIRING (the 0066 ceremony, verbatim from 0131): the migration connects via
``DATABASE_ADMIN_URL``. ``modulo_migrate`` is NOLOGIN so the migration executes
``SET ROLE modulo_migrate`` ONLY around the ``create_table("suite_runs")``, then
``RESET ROLE`` IMMEDIATELY AFTER (the RLS-enable + policy + grant steps run as
the migration's caller). CRITICAL: the ``eval_results`` ALTERs (``add_column``
``suite_run_id``, the FK, the index, and ``ALTER COLUMN run_id DROP NOT NULL``)
MUST run as the migration CALLER, NOT under ``SET ROLE modulo_migrate`` —
``eval_results`` is OWNED BY ``modulo_app`` (migration 0003) and ``modulo_migrate``
is not its owner, so running those as ``modulo_migrate`` fails the break-glass
deploy gate with ``must be owner of table eval_results``. A post-create
ownership assertion verifies ``suite_runs`` owner is ``modulo_migrate``. The
ceremony is conditional on the roles existing (fresh dev/BDD DBs have none).

Reversible: downgrade drops the ``suite_runs`` table, removes the
``eval_results.suite_run_id`` column + index + FK, restores ``run_id`` NOT NULL
(no rows have NULL run_id at that point — suite-run outcomes were attributed to
``suite_run_id`` and are dropped with their run), and disables RLS.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0137_eval_suite_run"
down_revision: str | None = "0136_rename_remy_user_id_to_account_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "suite_runs"


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl=f"public.{table}"
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"{table} owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own suite_runs — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        if migrate_role:
            op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
            for tbl in ("organisations", "teams", "eval_suites", "eval_datasets", "model_backends"):
                op.execute(f"GRANT REFERENCES ON TABLE public.{tbl} TO {_MIGRATE_ROLE}")
    else:
        migrate_role = False
        app_role = False

    if pg and migrate_role:
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")

    op.create_table(
        "suite_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("owner_team_id", sa.Uuid(), nullable=True),
        sa.Column("suite_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("definition_checksum", sa.String(length=64), nullable=False),
        sa.Column("model_backend_id", sa.Uuid(), nullable=False),
        sa.Column("scenario_signature", sa.String(length=64), nullable=True),
        sa.Column("baseline_tuple", sa.JSON(), nullable=True),
        sa.Column("baseline_run_id", sa.Uuid(), nullable=True),
        sa.Column("baseline_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_cases", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded_case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Numeric(precision=14, scale=6), nullable=True),
        sa.Column("claimed_cost", sa.Numeric(precision=14, scale=6), nullable=True, server_default="0"),
        sa.Column("comparison_json", sa.JSON(), nullable=True),
        sa.Column("regressed", sa.Boolean(), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_detail", sa.String(length=2000), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_team_id"], ["teams.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["suite_id"], ["eval_suites.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dataset_id"], ["eval_datasets.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["model_backend_id"], ["model_backends.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["baseline_run_id"], ["suite_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('pending','running','completed','partial','failed','cancelled')",
            name="ck_suite_runs_state",
        ),
    )

    if pg and migrate_role:
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)

    # eval_results attribution: back-link per-case outcomes to their SuiteRun.
    # ``run_id`` is relaxed to NULL so a SuiteRun outcome is attributed to a
    # ``suite_run`` rather than a pipeline ``Run``.
    #
    # CRITICAL: these MUST run as the migration CALLER (the DATABASE_ADMIN_URL
    # role), NOT under SET ROLE modulo_migrate — eval_results is OWNED BY
    # modulo_app (migration 0003) and modulo_migrate is not its owner, so running
    # them as modulo_migrate fails the break-glass deploy gate with
    # "must be owner of table eval_results".
    op.add_column("eval_results", sa.Column("suite_run_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_eval_results_suite_run_id",
        "eval_results",
        "suite_runs",
        ["suite_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute("ALTER TABLE eval_results ALTER COLUMN run_id DROP NOT NULL")

    op.create_index("ix_suite_runs_organisation_id", "suite_runs", ["organisation_id"])
    op.create_index("ix_suite_runs_suite_dataset", "suite_runs", ["suite_id", "dataset_id"])
    op.create_index("ix_suite_runs_state_created", "suite_runs", ["organisation_id", "state", "created_at"])
    op.create_index("ix_eval_results_suite_run_id", "eval_results", ["suite_run_id"])

    if pg:
        op.execute("ALTER TABLE suite_runs ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE suite_runs FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
        if app_role:
            op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")

    if pg:
        op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON {_TABLE}")
        op.execute(f"ALTER TABLE {_TABLE} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_eval_results_suite_run_id", table_name="eval_results")
    op.drop_constraint("fk_eval_results_suite_run_id", "eval_results", type_="foreignkey")
    op.drop_column("eval_results", "suite_run_id")
    # Suite-run outcomes (attributed to suite_runs) were dropped with the table,
    # so no eval_results row carries a NULL run_id at this point — we can safely
    # restore the legacy NOT NULL invariant.
    op.execute("ALTER TABLE eval_results ALTER COLUMN run_id SET NOT NULL")

    op.drop_index("ix_suite_runs_state_created", table_name="suite_runs")
    op.drop_index("ix_suite_runs_suite_dataset", table_name="suite_runs")
    op.drop_index("ix_suite_runs_organisation_id", table_name="suite_runs")

    op.drop_table("suite_runs")
