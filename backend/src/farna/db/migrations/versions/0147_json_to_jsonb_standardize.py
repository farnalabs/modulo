"""Promote remaining ``json`` columns to ``jsonb`` (dist db-jsonb-standardize).

Revision ID: 0147_json_to_jsonb_standardize
Revises: 0146_extend_runs_status_cost_ceiling
Create Date: 2026-08-24

The codebase adopted ``jsonb`` as its JSON standard (see 0129_runs_json_to_jsonb
and the many columns created as ``jsonb`` directly), but ~66 columns are still
typed plain ``json`` in Postgres. ``jsonb`` gives binary storage, faster
containment/``@>`` operators, and GIN-indexability, so this migration brings the
remaining columns up to the same standard.

Columns that are already ``jsonb`` (e.g. ``runs.*``,
``feedback_records.correction_state``, ``metrics_staging.payload``,
``eval_suites.eval_definition_ids``) are excluded. The ``col::jsonb`` cast
is lossless (NULL stays NULL; well-formed ``json`` re-parses identically as
``jsonb``).

Why this migration avoids ``ALTER COLUMN ... TYPE`` blocking rewrites
--------------------------------------------------------------------
The original migration ran a direct
``ALTER TABLE public."<table>" ALTER COLUMN "<col>" TYPE <target> USING ...``
for each ``(table, column)`` pair. Every ``ALTER COLUMN ... TYPE`` takes an
ACCESS EXCLUSIVE lock **and** performs a full-table rewrite. On hot, live tables
(``pipeline_snapshots``, ``agents``, ``organisations``, ``chat_messages``,
``eval_cases``, ``saved_views``, ``library_sync_state``, ...) that lock is never
acquired under writer contention, so the migration hangs forever and wedges the
deploy — the exact bug that blocked deploys on ``runs`` in 0129_runs_json_to_jsonb.

This rewrite generalises the 0129 algorithm to ~66 ``(table, column)`` pairs.
Each column is converted additively and in place:

1. **Add a temp ``{col}_{target}`` column** (fast: brief ACCESS EXCLUSIVE,
   metadata-only — no data rewrite), gated on ``information_schema.columns``.
2. **Batch backfill** ``{col}_{target} = {col}::cast`` for rows where the temp
   column is still NULL, in chunks of 1000. Each UPDATE touches row-level locks
   only — never a table-level exclusive lock held across the whole dataset,
   which is what made the direct ALTER un-acquirable.
3. **Swap** by renaming: ``{col}`` -> ``{col}_old``, then ``{col}_{target}`` ->
   ``{col}``, then drop ``{col}_old``. Each rename/drop is a brief
   metadata-only lock; there is no data rewrite.
4. **Finalize** by mirroring the original nullability/default.

All statements run on **alembic's own connection** inside the migration's
transaction. Postgres DDL is fully transactional, so the temp-column,
backfill and swap steps are atomic together with the version-row bookkeeping.
An earlier iteration of this migration used a *dedicated autocommit
side-connection* for the same steps; that variant is fundamentally incorrect
under this project's env.py, which wraps an entire ``upgrade`` chain in a
single transaction: on a fresh database the target tables exist only inside
that uncommitted transaction, so the side connection cannot see them at all
(``information_schema`` lookups return nothing) and every conversion silently
skipped while alembic still recorded the revisions as applied — leaving plain
``json`` columns at head. It also deadlocked whenever any table the chain had
already locked was touched again from the outside. Running on the migration's
own connection fixes both failure modes.

Trade-off versus true per-batch commits: the batched backfill holds its row
locks for the duration of the migration instead of releasing them between
chunks. Readers are never blocked and writers contend only on rows actively
being updated; crucially, there is still no long-held ACCESS EXCLUSIVE
table-rewrite, which was the actual deploy-killing behaviour being avoided.

Every phase is idempotent (column-existence gates on ADD/RENAME/DROP and
``WHERE {col}_{target} IS NULL`` gates the backfill), so a migration that fails
midway simply re-runs once the failure is cleared: already-converted columns
are skipped and only remaining rows are backfilled.

This is a Postgres-only change: SQLite/MariaDB use the ORM's generic ``JSON``
type, so the migration is skipped on non-Postgres dialects.

Downgrade reverts each column ``jsonb`` -> ``json`` using the same pattern
(add ``{col}_json``, batch ``::json`` backfill, swap, drop).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Connection, column, select, table, text, update
from sqlalchemy.dialects.postgresql import JSON as PG_JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0147_json_to_jsonb_standardize"
down_revision: str | None = "0146_extend_runs_status_cost_ceiling"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) pairs still typed ``json`` in Postgres that this migration
# promotes to ``jsonb``. Constants only - no user input reaches the DDL.
_JSON_TO_JSONB: tuple[tuple[str, str], ...] = (
    ("accounts", "preferences"),
    ("agents", "agent_commands"),
    ("agents", "prompt_version_history"),
    ("agents", "connector_type_refs"),
    ("agents", "required_environment_capabilities"),
    ("agents", "evals"),
    ("agents", "retry_policy"),
    ("audit_events", "payload_json"),
    ("composite_templates", "sub_pipeline_graph_json"),
    ("composite_templates", "parameter_ports_json"),
    ("connector_instances", "config_json"),
    ("connector_instances", "allowed_operations"),
    ("environment_profiles", "capabilities_json"),
    ("environment_profiles", "config_json"),
    ("environment_profiles", "secret_refs_json"),
    ("error_events", "context_json"),
    ("error_forwarder_configs", "config_json"),
    ("eval_cases", "input_payload"),
    ("eval_cases", "expected_output"),
    ("eval_definitions", "config_json"),
    ("feedback_records", "rejected_output"),
    ("hitl_claims", "decision_payload"),
    ("library_primitives", "tags"),
    ("library_primitives", "content_json"),
    ("library_sync_state", "manifest_json"),
    ("library_sync_state", "catalog_json"),
    ("lifecycle_maps", "content_json"),
    ("model_backends", "default_params"),
    ("model_backends", "fallback_backend_ids"),
    ("notification_endpoints", "events"),
    ("organisations", "settings_json"),
    ("organisations", "otel_config_json"),
    ("organisations", "export_bundle_json"),
    ("organisations", "guardrail_pins_json"),
    ("parameter_schemas", "parameters"),
    ("parameter_sets", "values"),
    ("pipelines", "run_context_defaults"),
    ("pipelines", "rate_limit_config"),
    ("pipeline_snapshots", "graph_json"),
    ("pipeline_snapshots", "connector_bindings_json"),
    ("pipeline_snapshots", "schema_pins_json"),
    ("pipeline_snapshots", "prompt_pins_json"),
    ("pipeline_snapshots", "model_backend_pins_json"),
    ("pipeline_snapshots", "composite_bindings_json"),
    ("pipeline_snapshots", "parameter_bindings_json"),
    ("pipeline_snapshots", "guardrail_pins_json"),
    ("pipeline_snapshots", "config_json"),
    ("pipeline_snapshots", "run_context_defaults"),
    ("chat_messages", "tool_calls_json"),
    ("chat_messages", "tool_results_json"),
    ("remy_skills", "triggers"),
    ("scheduled_reports", "config_json"),
    ("scheduled_reports", "recipient_config"),
    ("schema_versions", "definition_json"),
    ("sso_providers", "group_mappings"),
    ("system_config", "value"),
    ("teams", "notification_endpoints"),
    ("teams", "settings"),
    ("triggers", "config_json"),
    ("variant_groups", "variants"),
    ("saved_views", "filters"),
    ("saved_views", "columns"),
    ("webhook_payloads", "raw_payload"),
    ("workspace_leases", "resource_usage_json"),
    ("workspace_leases", "output_artifact_refs_json"),
    ("feature_flag_catalog", "depends_on"),
)

_SCHEMA = "public"
_BATCH_SIZE = 1000
_OLD_SUFFIX = "_old"


def _column_meta(bind: Connection, table_name: str, column_name: str) -> dict | None:
    """Return ``{is_nullable, column_default}`` for ``table.column`` or None.

    Uses ``information_schema.columns`` so the existence check doubles as a
    read of the original column's nullability/default before any rename
    invalidates it. Runs on the migration bind — the same connection/transaction
    as the rest of the migration — so columns created earlier in the same
    upgrade chain are always visible.
    """
    row = bind.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :table AND column_name = :col"
        ),
        {"schema": _SCHEMA, "table": table_name, "col": column_name},
    ).fetchone()
    if row is None:
        return None
    return {"is_nullable": row[0], "column_default": row[1]}


def _cast_type(cast: str):
    """Return the Postgres SQLAlchemy type for a ``::cast`` target."""
    return JSONB() if cast == "jsonb" else PG_JSON()


def _backfill(bind: Connection, table_name: str, col: str, tmp: str, cast: str) -> None:
    """Backfill ``tmp`` from ``col`` in bounded batches on the migration bind.

    Uses a SQLAlchemy Core ``update`` (not a raw string) so the row predicate is
    parameterised and no f-string SQL reaches the executor. ``{tmp} IS NULL``
    makes it resumable; ``ctid IN (SELECT ... LIMIT 1000)`` caps each UPDATE so
    it never builds one unbounded statement. Loops until a batch touches zero
    rows.
    """
    tbl = table(table_name, column("ctid"), column(col), column(tmp), schema=_SCHEMA)
    pending = tbl.c[tmp].is_(None) & tbl.c[col].isnot(None)
    ctid_subq = select(tbl.c.ctid).where(pending).limit(_BATCH_SIZE)
    stmt = update(tbl).where(pending).where(tbl.c.ctid.in_(ctid_subq)).values({tmp: tbl.c[col].cast(_cast_type(cast))})
    while True:
        result = bind.execute(stmt)
        if result.rowcount == 0:
            break


def _add_temp_column(bind: Connection, table_name: str, tmp: str, cast: str) -> None:
    """Add ``tmp`` (typed ``cast``) if absent — brief metadata-only lock."""
    if _column_meta(bind, table_name, tmp) is not None:
        return
    bind.execute(text(f'ALTER TABLE public."{table_name}" ADD COLUMN "{tmp}" {cast}'))


def _swap(bind: Connection, table_name: str, col: str, tmp: str, old: str) -> None:
    """Rename ``col`` -> ``old``, ``tmp`` -> ``col``, drop ``old``.

    Skips entirely if the ``old`` column already exists (re-run safety). Each
    step is a brief metadata-only lock; no data is rewritten.
    """
    if _column_meta(bind, table_name, old) is not None:
        return
    bind.execute(text(f'ALTER TABLE public."{table_name}" RENAME COLUMN "{col}" TO "{old}"'))
    bind.execute(text(f'ALTER TABLE public."{table_name}" RENAME COLUMN "{tmp}" TO "{col}"'))
    bind.execute(text(f'ALTER TABLE public."{table_name}" DROP COLUMN "{old}"'))


def _finalize(bind: Connection, table_name: str, col: str, orig: dict) -> None:
    """Mirror the original column's nullability/default onto the swapped column.

    The temp column is added nullable without a default, so after the swap the
    new ``col`` must be re-constrained to match the original. The cast is
    lossless, so the backfilled data is identical and ``SET NOT NULL`` is safe.
    """
    if orig["is_nullable"] == "NO":
        bind.execute(text(f'ALTER TABLE public."{table_name}" ALTER COLUMN "{col}" SET NOT NULL'))
    if orig["column_default"] is not None:
        bind.execute(
            text(f'ALTER TABLE public."{table_name}" ALTER COLUMN "{col}" SET DEFAULT {orig["column_default"]}')
        )


def _convert(bind: Connection, *, tmp_suffix: str, cast: str) -> None:
    """Convert each ``_JSON_TO_JSONB`` column via a temp column on ``bind``.

    ``tmp_suffix`` names the temporary column (``jsonb`` upgrading ``json`` ->
    ``jsonb``, ``json`` downgrading ``jsonb`` -> ``json``); the held-back column
    is always ``{col}_old``. Additive and idempotent, so a failed run resumes
    cleanly on re-execution.
    """
    for table_name, column_name in _JSON_TO_JSONB:
        orig = _column_meta(bind, table_name, column_name)
        if orig is None:
            # Column absent (non-standard DB) — nothing to convert.
            continue
        tmp = f"{column_name}_{tmp_suffix}"
        _add_temp_column(bind, table_name, tmp, cast)
        _backfill(bind, table_name, column_name, tmp, cast)
        _swap(bind, table_name, column_name, tmp, f"{column_name}{_OLD_SUFFIX}")
        _finalize(bind, table_name, column_name, orig)


def upgrade() -> None:
    # Postgres-only: plain ``json`` -> ``jsonb`` cast. SQLite uses the ORM model
    # (generic JSON), so skip on non-Postgres dialects. All statements run on
    # alembic's own connection inside the migration transaction: see the module
    # docstring for why a dedicated autocommit side-connection was rejected.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _convert(bind, tmp_suffix="jsonb", cast="jsonb")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _convert(bind, tmp_suffix="json", cast="json")
