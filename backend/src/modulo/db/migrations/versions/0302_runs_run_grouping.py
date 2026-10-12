"""FAR-153 / §15.1: run grouping + resume-as-child thread model.

Revision ID: 0302_runs_run_grouping
Revises: 0301_oauth_consent_state_preauth_rls
Create Date: 2026-10-12

The agent-failure UX design (§15.1, v6) replaces the single global
``UNIQUE(langgraph_thread_id)`` with a group/attempt model:

* ``runs.run_group_id``  UUID   NULL — the ROOT run id this row's
  retry/resume group belongs to (indexed; NULL for roots, signal /
  correction / work-item children and legacy rows until FAR-147 backfills
  them).
* ``runs.run_group_kind`` VARCHAR(16) NULL — ``'retry'`` | ``'resume'`` |
  NULL, single-sourced in ``modulo.db.models.run.RUN_GROUP_KIND_VALUES``
  and validated at the ``create_run`` persist boundary (no DB CHECK: a
  validation scan over the hot ``runs`` table buys nothing the write site
  does not already guarantee — the 0288 ``execution_origin`` precedent).
* DROP ``runs_langgraph_thread_id_key`` — a resume child MUST share its
  parent's thread to continue the parent's checkpoint, which the global
  unique constraint made impossible.
* Two partial unique indexes replace it (both declared on the ``Run``
  model so ``create_all``-built schemas and ``compare_metadata`` stay in
  parity):
    - ``uq_runs_resume_parent``      UNIQUE (parent_run_id)
        WHERE run_group_kind = 'resume'
      — at most ONE resume per stalled parent (a second resume of the
      same stall is rejected at the DB / 409 at the API), while a resume
      child that itself stalls can be resumed again because its parent is
      a DIFFERENT row, so resume-of-resume chains work.
    - ``uq_runs_thread_non_resume``  UNIQUE (langgraph_thread_id)
        WHERE run_group_kind IS DISTINCT FROM 'resume'
      — global thread uniqueness for roots / retries / legacy rows is
      preserved exactly; only resume children may share a thread, and only
      their parent's.

Migration applies over existing rows: every pre-existing row has
``run_group_kind`` NULL, so ALL of them fall inside the thread partial
index (their threads are already globally unique under the constraint
being dropped — the index build cannot fail) and NONE fall inside the
resume-parent index (its predicate matches nothing, so the index builds
empty).

Downgrade reverses the DDL and restores ``runs_langgraph_thread_id_key`` —
but ONLY when no two rows share a thread (i.e. no resume child has been
created yet). It refuses with an actionable error rather than emitting a
cryptic unique violation or silently rewriting history: a shared thread
cannot be un-shared without destroying the resume attempt (the 0151 /
0298 downgrade-refusal precedent).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0302_runs_run_grouping"
down_revision: str | None = "0301_oauth_consent_state_preauth_rls"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# The constraint migration 0003 created inline (PG auto-name) and 0110
# re-asserted under the same name — see the guarded ADD in 0110.
_GLOBAL_THREAD_UNIQUE = "runs_langgraph_thread_id_key"

# Predicates, byte-identical to the ``Run`` model's ``__table_args__`` Index
# declarations (model/migration parity is what keeps ``test_initial_migration``
# green; the unit test compares them).
_RESUME_PARENT_PREDICATE = "run_group_kind = 'resume'"
_THREAD_NON_RESUME_PREDICATE_PG = "run_group_kind IS DISTINCT FROM 'resume'"
# SQLite has no ``IS DISTINCT FROM`` before 3.39; ``IS NOT`` is its null-safe
# twin with identical semantics for this predicate (NULL IS NOT 'resume' -> true).
_THREAD_NON_RESUME_PREDICATE_SQLITE = "run_group_kind IS NOT 'resume'"


def upgrade() -> None:
    # Columns FIRST: both partial-index predicates reference run_group_kind.
    # Nullable, no server default — metadata-only ADD COLUMN on Postgres, no
    # rewrite of the hot ``runs`` table (0288 precedent).
    op.add_column(
        "runs",
        sa.Column(
            "run_group_id",
            sa.Uuid(),
            nullable=True,
            comment="root run id of the retry/resume group this attempt belongs to (FAR-153 / 15.1)",
        ),
    )
    op.add_column(
        "runs",
        sa.Column(
            "run_group_kind",
            sa.String(16),
            nullable=True,
            comment="why this row joined its group: 'retry' | 'resume' | NULL (FAR-153 / 15.1)",
        ),
    )
    op.create_index("ix_runs_run_group_id", "runs", ["run_group_id"])

    # Replace the global unique with the two partial uniques. Dropping the
    # constraint first matches the spec order; env.py wraps the whole revision
    # in one transaction, so there is no observable window without uniqueness.
    op.drop_constraint(_GLOBAL_THREAD_UNIQUE, "runs", type_="unique")
    op.create_index(
        "uq_runs_resume_parent",
        "runs",
        ["parent_run_id"],
        unique=True,
        postgresql_where=sa.text(_RESUME_PARENT_PREDICATE),
        sqlite_where=sa.text(_RESUME_PARENT_PREDICATE),
    )
    op.create_index(
        "uq_runs_thread_non_resume",
        "runs",
        ["langgraph_thread_id"],
        unique=True,
        postgresql_where=sa.text(_THREAD_NON_RESUME_PREDICATE_PG),
        sqlite_where=sa.text(_THREAD_NON_RESUME_PREDICATE_SQLITE),
    )


def downgrade() -> None:
    op.drop_index("uq_runs_thread_non_resume", table_name="runs")
    op.drop_index("uq_runs_resume_parent", table_name="runs")
    op.drop_index("ix_runs_run_group_id", table_name="runs")
    op.drop_column("runs", "run_group_kind")
    op.drop_column("runs", "run_group_id")

    # Restore the global unique — but refuse loudly first if any thread is
    # shared (a resume child sharing its parent's thread). Emitting the raw
    # unique violation would bury the cause; silently dropping the child
    # would destroy a run row. Actionable refusal, data intact (0151/0298).
    bind = op.get_bind()
    shared = bind.execute(
        sa.text(
            "SELECT count(*) FROM ("
            "SELECT langgraph_thread_id FROM runs GROUP BY langgraph_thread_id HAVING count(*) > 1"
            ") AS shared_threads"
        )
    ).scalar()
    if shared:
        raise RuntimeError(
            f"cannot restore {_GLOBAL_THREAD_UNIQUE}: {shared} thread(s) are shared by multiple runs "
            "(resume children). Delete or resolve the resume child rows first — the downgrade "
            "refuses to rewrite or drop run history."
        )
    op.create_unique_constraint(_GLOBAL_THREAD_UNIQUE, "runs", ["langgraph_thread_id"])
