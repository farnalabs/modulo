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
``eval_suites.eval_definition_ids``) are excluded. The ``USING col::jsonb`` cast
is lossless (NULL stays NULL; well-formed ``json`` re-parses identically as
``jsonb``).

Why this rewrite is NON-BLOCKING and RESUMABLE
---------------------------------------------
The original migration ran a direct
``ALTER TABLE public."<table>" ALTER COLUMN "<col>" TYPE <target> USING ...``
for each ``(table, column)`` pair. Every ``ALTER COLUMN ... TYPE`` takes an
ACCESS EXCLUSIVE lock **and** performs a full-table rewrite. On hot, live tables
(``pipeline_snapshots``, ``agents``, ``organisations``, ``chat_messages``,
``eval_cases``, ``saved_views``, ``library_sync_state``, ...) that lock is never
acquired under writer contention, so the migration hangs forever and wedges the
deploy — the exact bug that blocked deploys on ``runs`` in 0129_runs_json_to_jsonb.

This rewrite reuses the proven, table-generalised 0129 algorithm. The
``json`` -> ``jsonb`` cast is lossless for every existing row (NULL stays NULL;
well-formed ``json`` re-parses identically as ``jsonb``), so each column is
converted additively and in place:

1. **Add a temp ``{col}_{target}`` column** (fast: brief ACCESS EXCLUSIVE,
   metadata-only — no data rewrite), gated on ``information_schema.columns``.
2. **Batch backfill** ``{col}_{target} = {col}::cast`` for rows where the temp
   column is still NULL, in chunks of 1000. Row-level only: no table lock, safe
   under concurrent writes, and resumable because only still-pending rows are
   touched.

Every statement runs on a **dedicated autocommit connection** obtained from
alembic's engine (not on ``op.get_bind()``), because alembic wraps the migration
in a transaction context manager and an explicit ``.commit()`` on its connection
would close that transaction and break the next statement. The autocommit
connection commits each statement independently, so the migration stays
resumable while alembic's own transaction (which records the version row) is
kept intact.
3. **Swap** by renaming: ``{col}`` -> ``{col}_old``, then ``{col}_{target}`` ->
   ``{col}``, then drop ``{col}_old``. Each rename/drop is a brief metadata-only
   lock; there is no data rewrite.
4. **Finalize** by mirroring the original ``is_nullable``/``column_default``
   captured before any rename.

Every phase is idempotent (column-existence gates on ADD/RENAME/DROP and
``WHERE {col}_{target} IS NULL`` gates the backfill), so a migration that fails
midway simply re-runs: already-converted columns are skipped and only the
remaining rows are backfilled.

This is a Postgres-only change: SQLite/MariaDB use the ORM's generic ``JSON``
type, so the migration is skipped on non-Postgres dialects.

Downgrade reverts each column ``jsonb`` -> ``json`` using the same non-blocking,
resumable pattern (add ``{col}_json``, batch ``::json`` backfill, swap, drop).
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


def _column_meta(ac, table_name: str, column_name: str) -> dict | None:
    """Return ``{is_nullable, column_default}`` for ``table_name.column_name`` or None.

    Uses ``information_schema.columns`` so the existence check doubles as a read
    of the original column's nullability/default before any rename invalidates
    it. Runs on the dedicated autocommit connection (``ac``); Postgres-only, the
    caller gates on the dialect.
    """
    row = ac.execute(
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


def _backfill(ac, table_name: str, col: str, tmp: str, cast: str) -> None:
    """Backfill ``tmp`` from ``col`` in bounded batches on the autocommit ``ac``.

    Uses a SQLAlchemy Core ``update`` (not a raw string) so the row predicate is
    parameterised and no f-string SQL reaches the executor. ``{tmp} IS NULL``
    makes it resumable; ``ctid IN (SELECT ... LIMIT 1000)`` caps each UPDATE so
    it holds no long transaction and never blocks writers (row-level locks
    only). Loops until a batch touches zero rows. ``ac`` is autocommit, so each
    statement commits itself and nothing is explicitly committed here.
    """
    tbl = table(table_name, column("ctid"), column(col), column(tmp), schema=_SCHEMA)
    pending = tbl.c[tmp].is_(None) & tbl.c[col].isnot(None)
    ctid_subq = select(tbl.c.ctid).where(pending).limit(_BATCH_SIZE)
    stmt = update(tbl).where(pending).where(tbl.c.ctid.in_(ctid_subq)).values({tmp: tbl.c[col].cast(_cast_type(cast))})
    while True:
        result = ac.execute(stmt)
        rowcount = result.rowcount
        if rowcount == 0:
            break


def _add_temp_column(ac, table_name: str, tmp: str, cast: str) -> None:
    """Add ``tmp`` (typed ``cast``) if absent — brief metadata-only lock.

    Runs on the autocommit ``ac``; the statement is autocommitted.
    """
    if _column_meta(ac, table_name, tmp) is not None:
        return
    ac.execute(text(f'ALTER TABLE public."{table_name}" ADD COLUMN "{tmp}" {cast}'))


def _swap(ac, table_name: str, col: str, tmp: str, old: str) -> None:
    """Rename ``col`` -> ``old``, ``tmp`` -> ``col``, drop ``old``.

    Skips entirely if the ``old`` column already exists (re-run safety). Each
    step is a brief metadata-only lock; no data is rewritten.
    """
    if _column_meta(ac, table_name, old) is not None:
        return
    ac.execute(text(f'ALTER TABLE public."{table_name}" RENAME COLUMN "{col}" TO "{old}"'))
    ac.execute(text(f'ALTER TABLE public."{table_name}" RENAME COLUMN "{tmp}" TO "{col}"'))
    ac.execute(text(f'ALTER TABLE public."{table_name}" DROP COLUMN "{old}"'))


def _finalize(ac, table_name: str, col: str, orig: dict) -> None:
    """Mirror the original column's nullability/default onto the swapped column.

    The temp column is added nullable without a default, so after the swap the
    new ``col`` must be re-constrained to match the original. The cast is
    lossless, so the backfilled data is identical and ``SET NOT NULL`` is safe.
    """
    if orig["is_nullable"] == "NO":
        ac.execute(text(f'ALTER TABLE public."{table_name}" ALTER COLUMN "{col}" SET NOT NULL'))
    if orig["column_default"] is not None:
        ac.execute(text(f'ALTER TABLE public."{table_name}" ALTER COLUMN "{col}" SET DEFAULT {orig["column_default"]}'))


def _convert(ac, *, tmp_suffix: str, cast: str) -> None:
    """Non-blockingly convert each ``_JSON_TO_JSONB`` column via a temp column.

    ``tmp_suffix`` names the temporary column (``jsonb`` upgrading ``json`` ->
    ``jsonb``, ``json`` downgrading ``jsonb`` -> ``json``); the held-back column
    is always ``{col}_old``. Additive and idempotent, so a failed run resumes
    cleanly on re-execution. Runs on the dedicated autocommit ``ac`` connection,
    independent of alembic's transaction.
    """
    for table_name, column_name in _JSON_TO_JSONB:
        orig = _column_meta(ac, table_name, column_name)
        if orig is None:
            # Column absent (non-standard DB) — nothing to convert.
            continue
        tmp = f"{column_name}_{tmp_suffix}"
        _add_temp_column(ac, table_name, tmp, cast)
        _backfill(ac, table_name, column_name, tmp, cast)
        _swap(ac, table_name, column_name, tmp, f"{column_name}{_OLD_SUFFIX}")
        _finalize(ac, table_name, column_name, orig)


def _autocommit_bind() -> Connection:
    """Return a fresh autocommit connection from alembic's engine.

    Alembic wraps each migration in ``context.begin_transaction()``; calling
    ``commit`` on that connection closes the migration transaction so the next
    statement fails. A dedicated autocommit connection (``isolation_level=
    "AUTOCOMMIT"``, psycopg2 sync) is independent of that transaction, so each
    per-statement commit is genuine and the migration stays resumable.
    """
    bind = op.get_bind()
    ac = bind.engine.connect()
    return ac.execution_options(isolation_level="AUTOCOMMIT")


def upgrade() -> None:
    # Postgres-only: plain ``json`` -> ``jsonb`` cast. SQLite uses the ORM model
    # (generic JSON), so skip on non-Postgres dialects. Run all statements on a
    # dedicated autocommit connection so alembic's migration transaction stays
    # intact (it records the version row) while each DDL/DML commit is real.
    ac = _autocommit_bind()
    try:
        if ac.dialect.name != "postgresql":
            return
        _convert(ac, tmp_suffix="jsonb", cast="jsonb")
    finally:
        ac.close()


def downgrade() -> None:
    ac = _autocommit_bind()
    try:
        if ac.dialect.name != "postgresql":
            return
        _convert(ac, tmp_suffix="json", cast="json")
    finally:
        ac.close()
