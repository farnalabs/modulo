"""FAR-1100 chunk 3 (PR-1): backfill Eval/PolicyGate, repoint FK, recreate trigger.

Backfills every ``eval_definitions`` row into the new ``evals`` table (1:1 UUID
reuse) and synthesises ``policy_gates`` rows for live, node-scoped, non-guardrail
evals.  Validates each candidate PolicyGate binding via ``validate_binding`` and
records rejections in a dedicated audit table (``eval_backfill_violations``).

After backfill: drops and recreates the ``eval_results.eval_id`` FK to target
``evals.id`` (same UUID, zero data migration) and recreates the
``trg_eval_results_eval_id_tenant`` trigger to resolve via ``evals`` instead of
``eval_definitions``.

**config_json type promotion:** 0147_json_to_jsonb_standardize promoted
``eval_definitions.config_json`` to ``jsonb``, but 0250 created the new
``evals.config_json`` as plain ``json`` (generic ``sa.JSON()``).  Postgres has
no ``json = jsonb`` operator, so the content-correctness verification (and any
future cross-table comparison) is un-parseable until the new column is
promoted to the same ``jsonb`` standard.  This migration promotes
``evals.config_json`` to ``jsonb`` before the backfill copy (and demotes it
back in downgrade) — matching the repo parity rule that Postgres DDL carries
JSONB while ORM models map generic ``JSON``.

**CO-7 (accepted no-decision-records window):** Between this chunk's read
cutover and chunk 4's landing, governance decisions produce NO decision records.
This is not a regression — the legacy path was equally unaudited — and chunk 4
closes it.  Recorded here per the doc-truth gate.

The migration is a no-op on non-Postgres (SQLite relies on ORM ``create_all``).

Revision ID: 0251_eval_backfill_cutover
Revises: 0250_eval_policy_gate
Create Date: 2026-09-21
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

revision = "0251_eval_backfill_cutover"
down_revision = "0250_eval_policy_gate"
branch_labels = None
depends_on = None

logger = logging.getLogger(f"alembic.{revision}")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BATCH_SIZE = 500
_DRAIN_POLL_INTERVAL_S = 5
_DRAIN_TIMEOUT_S = 600  # 10 minutes

# Active (in-flight) run statuses — from modulo.db.models.run.ACTIVE_RUN_STATUSES.
_ACTIVE_RUN_STATUSES = (
    "pending",
    "running",
    "awaiting_human",
    "claimed",
    "unknown",
    "hitl_parked",
)

# Columns copied from eval_definitions → evals.
_EVAL_COPY_COLUMNS = (
    "organisation_id",
    "pipeline_id",
    "node_id",
    "name",
    "eval_type",
    "config_json",
    "pass_threshold",
    "suite_id",
    "eval_suite_id",
    "account_id",
    "version",
    "pre_version_raw",
    "deleted_at",
    "deleted_by",
)

# FK / trigger names (verified against 0110_schema_pipeline_runtime.py).
_FK_EVAL_RESULTS_EVAL_ID = "eval_results_eval_id_fkey"
_TRIGGER_EVAL_RESULTS_EVAL_ID_TENANT = "trg_eval_results_eval_id_tenant"

# Known-expected object disposition (spec §3.2 Step 5a, §3.2 known-objects table).
_KNOWN_TRIGGERS_ON_EVAL_RESULTS: set[str] = {
    _TRIGGER_EVAL_RESULTS_EVAL_ID_TENANT,  # drop + recreate
    "trg_eval_results_run_id_tenant",  # leave unchanged
}
_KNOWN_TRIGGERS_ON_EVAL_DEFINITIONS: set[str] = {
    "trg_eval_definitions_account_id_tenant",  # defer to purge chunk
    "trg_eval_definitions_pipeline_id_tenant",  # defer to purge chunk
}

# Known-expected violation exclusion names (spec §6.3).
_KNOWN_EXPECTED_EXCLUSIONS = {"cross_tenancy", "node_id_mismatch"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_postgres() -> bool:
    return str(op.get_bind().dialect.name).startswith("postgres")


def _execute(sql: str) -> Any:
    return op.execute(text(sql))


def _scalar(sql: str) -> Any:
    return op.get_bind().execute(text(sql)).scalar()


# ---------------------------------------------------------------------------
# Step 0: Precondition drain check
# ---------------------------------------------------------------------------


def _drain_check() -> None:
    """Poll for in-flight runs; abort if any remain after timeout."""
    deadline = time.monotonic() + _DRAIN_TIMEOUT_S
    active_sql = (
        "SELECT id FROM runs WHERE status IN ("  # nosec B608
        + ", ".join(f"'{s}'" for s in _ACTIVE_RUN_STATUSES)  # nosec B608
        + ") LIMIT 20"
    )
    while True:
        stuck_ids = [str(r[0]) for r in op.get_bind().execute(text(active_sql)).fetchall()]
        if not stuck_ids:
            return
        if time.monotonic() >= deadline:
            msg = (
                f"Migration abort: {len(stuck_ids)} run(s) still in active state "
                f"after {_DRAIN_TIMEOUT_S}s timeout.  "
                f"Stuck run IDs: {stuck_ids}"
            )
            logger.error(msg)
            raise RuntimeError(msg)
        logger.warning(
            "Drain check: %d run(s) still active; retrying in %ds …",
            len(stuck_ids),
            _DRAIN_POLL_INTERVAL_S,
        )
        time.sleep(_DRAIN_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Step 1: Object enumeration (spec §3.2 Step 5a)
# ---------------------------------------------------------------------------


def _enumerate_objects() -> None:
    """Query Postgres catalog and assert only known objects exist."""
    # FKs INTO eval_definitions
    fk_rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT tc.constraint_name "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name = ccu.constraint_name "
                "WHERE tc.constraint_type = 'FOREIGN KEY' "
                "  AND ccu.table_name = 'eval_definitions'"
            )
        )
        .fetchall()
    )
    discovered_fks = {r[0] for r in fk_rows}
    # The only FK INTO eval_definitions that we care about is eval_results_eval_id_fkey,
    # which we will repoint.  Any other FK is unexpected.
    unexpected_fks = discovered_fks - {_FK_EVAL_RESULTS_EVAL_ID}
    if unexpected_fks:
        raise RuntimeError(
            f"Unexpected FK(s) into eval_definitions: {unexpected_fks}. Add them to the known-objects table or abort."
        )

    # Non-internal triggers on eval_results and eval_definitions
    trigger_rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT t.tgname, c.relname "
                "FROM pg_trigger t "
                "JOIN pg_class c ON t.tgrelid = c.oid "
                "WHERE c.relname IN ('eval_results', 'eval_definitions') "
                "  AND NOT t.tgisinternal"
            )
        )
        .fetchall()
    )
    all_expected = _KNOWN_TRIGGERS_ON_EVAL_RESULTS | _KNOWN_TRIGGERS_ON_EVAL_DEFINITIONS
    discovered_triggers = {(r[0], r[1]) for r in trigger_rows}
    unexpected_triggers = {(name, tbl) for name, tbl in discovered_triggers if name not in all_expected}
    if unexpected_triggers:
        raise RuntimeError(
            f"Unexpected trigger(s): {unexpected_triggers}. Add them to the known-objects table or abort."
        )


# ---------------------------------------------------------------------------
# Step 2: Violation inventory table
# ---------------------------------------------------------------------------


def _create_violation_table() -> None:
    """Create the audit table for binding-validation rejections (idempotent)."""
    inspector = sa.inspect(op.get_bind())
    if "eval_backfill_violations" not in inspector.get_table_names():
        op.create_table(
            "eval_backfill_violations",
            sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("migration_revision", sa.Text(), nullable=False),
            sa.Column("eval_definition_id", sa.Uuid(), nullable=False),
            sa.Column("eval_name", sa.Text(), nullable=True),
            sa.Column("eval_type", sa.Text(), nullable=False),
            sa.Column("node_id", sa.Uuid(), nullable=True),
            sa.Column("violated_exclusions", postgresql.JSONB(), nullable=False),
            sa.Column(
                "created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False
            ),
            sa.UniqueConstraint(
                "migration_revision",
                "eval_definition_id",
                name="uq_eval_backfill_violations_rev_eval",
            ),
        )
    else:
        # Table exists — add the unique constraint if missing (idempotent re-run).
        _execute(
            "DO $$ BEGIN "
            "IF NOT EXISTS ("
            "  SELECT 1 FROM pg_constraint WHERE conname = 'uq_eval_backfill_violations_rev_eval'"
            ") THEN "
            "  ALTER TABLE eval_backfill_violations "
            "    ADD CONSTRAINT uq_eval_backfill_violations_rev_eval "
            "    UNIQUE (migration_revision, eval_definition_id); "
            "END IF; END $$;"
        )


# ---------------------------------------------------------------------------
# Step 2b: Binding validation
# ---------------------------------------------------------------------------


def _validate_bindings() -> list[dict[str, Any]]:
    """Run validate_binding for each candidate PolicyGate row.

    Returns the list of rejection records.
    """
    # Lazy import — module is in the package path.
    from modulo.core.eval_engine.policy_gate import (
        PolicyGateBindingViolationError,
        validate_binding,
    )

    # Candidate rows: live, node-scoped, non-guardrail
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT id, organisation_id, node_id, name, eval_type, failure_behaviour "
                "FROM eval_definitions "
                "WHERE deleted_at IS NULL AND node_id IS NOT NULL AND eval_type != 'guardrail'"
            )
        )
        .fetchall()
    )

    violations: list[dict[str, Any]] = []
    for row in rows:
        eval_definition_id = row[0]
        organisation_id = row[1]
        node_id = row[2]
        eval_name = row[3]
        eval_type = row[4]
        # failure_behaviour is the source for PolicyGate.action
        try:
            validate_binding(
                policy_gate_fields={
                    "id": uuid.uuid4(),  # placeholder — not persisted yet
                    "organisation_id": organisation_id,
                    "node_id": node_id,
                },
                eval_fields={
                    "id": eval_definition_id,
                    "organisation_id": organisation_id,
                    "node_id": node_id,
                    "eval_type": eval_type,
                    "deleted_at": None,  # filtered to IS NULL
                },
            )
        except PolicyGateBindingViolationError as exc:
            violated_names = [v["exclusion"] for v in exc.violations]
            violations.append(
                {
                    "eval_definition_id": eval_definition_id,
                    "eval_name": eval_name,
                    "eval_type": eval_type,
                    "node_id": node_id,
                    "violated_exclusions": violated_names,
                }
            )
    return violations


def _record_violations(violations: list[dict[str, Any]]) -> None:
    """Write violation records to the audit table (idempotent — ON CONFLICT DO NOTHING)."""
    if not violations:
        return
    for v in violations:
        op.get_bind().execute(
            text(
                "INSERT INTO eval_backfill_violations "
                "(migration_revision, eval_definition_id, eval_name, eval_type, node_id, violated_exclusions) "
                "VALUES (:rev, :eid, :ename, :etype, :nid, :vej::jsonb) "
                "ON CONFLICT ON CONSTRAINT uq_eval_backfill_violations_rev_eval DO NOTHING"
            ),
            {
                "rev": revision,
                "eid": v["eval_definition_id"],
                "ename": v["eval_name"],
                "etype": v["eval_type"],
                "nid": v["node_id"],
                "vej": json.dumps(v["violated_exclusions"]),
            },
        )


# ---------------------------------------------------------------------------
# Step 2c: config_json type promotion (json -> jsonb)
# ---------------------------------------------------------------------------


def _promote_evals_config_json() -> None:
    """Promote ``evals.config_json`` from ``json`` to ``jsonb``.

    0147 promoted ``eval_definitions.config_json`` to ``jsonb`` while 0250
    created ``evals.config_json`` as plain ``json``.  Postgres defines no
    ``json = jsonb`` operator, so the content-correctness verification below
    (and any future cross-table comparison) fails at parse time until the
    columns share a type.  Promote the new column to the 0147 standard — the
    backfill copy then runs jsonb -> jsonb.  Idempotent: re-casting jsonb as
    jsonb is a no-op.
    """
    _execute("ALTER TABLE evals ALTER COLUMN config_json TYPE jsonb USING config_json::jsonb")


def _demote_evals_config_json() -> None:
    """Reverse the upgrade-time promotion (downgrade restores the 0250 type).

    Backfilled rows are deleted before this runs (the guarded delete above),
    so the demotion operates on at most the dead rows that survived the
    FK-RESTRICT guard.  ``text`` round-trip is the safe jsonb -> json route.
    """
    _execute("ALTER TABLE evals ALTER COLUMN config_json TYPE json USING config_json::text::json")


# ---------------------------------------------------------------------------
# Step 3–4: Batched backfill
# ---------------------------------------------------------------------------


def _backfill_evals() -> int:
    """Insert Eval rows (1:1 UUID reuse). Returns total inserted."""
    total = 0
    offset = 0
    # JSON columns are cast to text on the way out: the driver may hand back
    # parsed JSON objects for json/jsonb columns (codec-dependent), and a
    # parsed object cannot be re-bound as a parameter for the INSERT.  A str
    # binds fine into the (jsonb) target — Postgres coerces the literal.
    _json_text_cast = {"config_json", "pre_version_raw"}
    select_cols_sql = ", ".join((f"{c}::text AS {c}" if c in _json_text_cast else c) for c in _EVAL_COPY_COLUMNS)
    cols_sql = ", ".join(_EVAL_COPY_COLUMNS)
    while True:
        rows = (
            op.get_bind()
            .execute(
                text("SELECT id, " + select_cols_sql + " FROM eval_definitions ORDER BY id LIMIT :lim OFFSET :off"),  # nosec B608
                {"lim": _BATCH_SIZE, "off": offset},
            )
            .fetchall()
        )
        if not rows:
            break
        # Build parameterised insert — ON CONFLICT DO NOTHING for idempotency.
        insert_sql = (
            "INSERT INTO evals (id, " + cols_sql + ") "  # nosec B608
            "VALUES (:id, " + ", ".join(f":{c}" for c in _EVAL_COPY_COLUMNS) + ") "
            "ON CONFLICT (id) DO NOTHING"
        )
        batch = []
        for row in rows:
            params = {"id": row[0]}
            for i, col in enumerate(_EVAL_COPY_COLUMNS):
                params[col] = row[i + 1]
            batch.append(params)
        # Use a savepoint per batch.
        sp = op.get_bind().begin_nested()
        try:
            for params in batch:
                op.get_bind().execute(text(insert_sql), params)
            sp.commit()
            total += len(batch)
        except Exception:
            sp.rollback()
            logger.warning(
                "Eval backfill batch starting at offset %d failed (%d rows); "
                "rolled back.  Re-run the migration to retry.",
                offset,
                len(batch),
            )
        offset += _BATCH_SIZE
    return total


def _backfill_policy_gates(violation_ids: set[uuid.UUID]) -> int:
    """Insert PolicyGate rows for candidates not rejected by validation.

    Returns total inserted.
    """
    total = 0
    offset = 0
    while True:
        rows = (
            op.get_bind()
            .execute(
                text(
                    "SELECT id, organisation_id, node_id, failure_behaviour "
                    "FROM eval_definitions "
                    "WHERE deleted_at IS NULL "
                    "  AND node_id IS NOT NULL "
                    "  AND eval_type != 'guardrail' "
                    "ORDER BY id LIMIT :lim OFFSET :off"
                ),
                {"lim": _BATCH_SIZE, "off": offset},
            )
            .fetchall()
        )
        if not rows:
            break
        batch = [
            {
                "eval_id": r[0],
                "organisation_id": r[1],
                "node_id": r[2],
                "action": r[3],
            }
            for r in rows
            if r[0] not in violation_ids
        ]
        if batch:
            insert_sql = (
                "INSERT INTO policy_gates (eval_id, node_id, action, organisation_id) "
                "VALUES (:eval_id, :node_id, :action, :organisation_id) "
                "ON CONFLICT (eval_id) WHERE deleted_at IS NULL DO NOTHING"
            )
            sp = op.get_bind().begin_nested()
            try:
                for params in batch:
                    op.get_bind().execute(text(insert_sql), params)
                sp.commit()
                total += len(batch)
            except Exception:
                sp.rollback()
                logger.warning(
                    "PolicyGate backfill batch starting at offset %d failed; rolled back.",
                    offset,
                )
        offset += _BATCH_SIZE
    return total


# ---------------------------------------------------------------------------
# Step 5: FK repoint
# ---------------------------------------------------------------------------


def _repoint_fk() -> None:
    """Drop the old FK on eval_results.eval_id and recreate against evals.id."""
    # Drop if it exists (idempotent).
    _execute(f'ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "{_FK_EVAL_RESULTS_EVAL_ID}"')
    # Recreate.
    _execute(
        f'ALTER TABLE eval_results ADD CONSTRAINT "{_FK_EVAL_RESULTS_EVAL_ID}" '
        "FOREIGN KEY (eval_id) REFERENCES evals(id) ON DELETE CASCADE"
    )


# ---------------------------------------------------------------------------
# Step 5a: Trigger recreation
# ---------------------------------------------------------------------------


def _recreate_trigger() -> None:
    """Drop and recreate trg_eval_results_eval_id_tenant → evals."""
    _execute(f'DROP TRIGGER IF EXISTS "{_TRIGGER_EVAL_RESULTS_EVAL_ID_TENANT}" ON eval_results')
    _execute(
        f'CREATE TRIGGER "{_TRIGGER_EVAL_RESULTS_EVAL_ID_TENANT}" '
        "BEFORE INSERT OR UPDATE OF eval_id, organisation_id ON public.eval_results "
        "FOR EACH ROW "
        "EXECUTE FUNCTION public.enforce_same_organisation('evals', 'eval_id')"
    )


# ---------------------------------------------------------------------------
# Step 6–7: Verification assertions
# ---------------------------------------------------------------------------


def _verify_counts(expected_pg_count: int) -> None:
    """Run row-count assertions (spec §3.2 Step 6)."""

    # Every EvalDefinition → one Eval
    eval_count = _scalar("SELECT COUNT(*) FROM evals")
    ed_count = _scalar("SELECT COUNT(*) FROM eval_definitions")
    if eval_count != ed_count:
        raise RuntimeError(f"Eval count mismatch: evals={eval_count}, eval_definitions={ed_count}")

    # PolicyGate count matches expectation
    pg_count = _scalar("SELECT COUNT(*) FROM policy_gates")
    if pg_count != expected_pg_count:
        raise RuntimeError(f"PolicyGate count mismatch: policy_gates={pg_count}, expected={expected_pg_count}")

    # No PolicyGate for soft-deleted Evals
    bad_soft_deleted = _scalar(
        "SELECT COUNT(*) FROM policy_gates pg JOIN evals e ON pg.eval_id = e.id WHERE e.deleted_at IS NOT NULL"
    )
    if bad_soft_deleted:
        raise RuntimeError(f"{bad_soft_deleted} PolicyGate(s) linked to soft-deleted Eval(s)")

    # No PolicyGate for guardrail-typed Evals
    bad_guardrail = _scalar(
        "SELECT COUNT(*) FROM policy_gates pg JOIN evals e ON pg.eval_id = e.id WHERE e.eval_type = 'guardrail'"
    )
    if bad_guardrail:
        raise RuntimeError(f"{bad_guardrail} PolicyGate(s) linked to guardrail-typed Eval(s)")

    # No PolicyGate for suite-scoped Evals (node_id IS NULL)
    bad_suite = _scalar(
        "SELECT COUNT(*) FROM policy_gates pg JOIN evals e ON pg.eval_id = e.id WHERE e.node_id IS NULL"
    )
    if bad_suite:
        raise RuntimeError(f"{bad_suite} PolicyGate(s) linked to suite-scoped Eval(s)")

    # Anti-join: every eligible EvalDefinition produced a PolicyGate
    missing = _scalar(
        "SELECT COUNT(*) FROM eval_definitions ed "
        "LEFT JOIN policy_gates pg ON pg.eval_id = ed.id "
        "WHERE pg.eval_id IS NULL "
        "  AND ed.deleted_at IS NULL "
        "  AND ed.node_id IS NOT NULL "
        "  AND ed.eval_type != 'guardrail'"
    )
    if missing:
        raise RuntimeError(f"Anti-join assertion failed: {missing} EvalDefinition(s) missing a PolicyGate")


def _verify_content() -> None:
    """Verify every copied field matches its source (spec §3.2 Step 7)."""
    mismatches = (
        op.get_bind()
        .execute(
            text(
                "SELECT e.id FROM evals e "
                "JOIN eval_definitions ed ON e.id = ed.id "
                "WHERE e.pipeline_id IS DISTINCT FROM ed.pipeline_id "
                "   OR e.node_id IS DISTINCT FROM ed.node_id "
                "   OR e.eval_type IS DISTINCT FROM ed.eval_type "
                "   OR e.config_json IS DISTINCT FROM ed.config_json "
                "   OR e.pass_threshold IS DISTINCT FROM ed.pass_threshold "
                "   OR e.suite_id IS DISTINCT FROM ed.suite_id "
                "   OR e.deleted_at IS DISTINCT FROM ed.deleted_at"
            )
        )
        .fetchall()
    )
    if mismatches:
        bad_ids = [str(r[0]) for r in mismatches]
        raise RuntimeError(
            f"Content-correctness assertion failed for {len(bad_ids)} Eval(s): "
            f"{bad_ids[:10]}{'…' if len(bad_ids) > 10 else ''}"
        )

    # PolicyGate.action matches source failure_behaviour
    bad_actions = (
        op.get_bind()
        .execute(
            text(
                "SELECT pg.id FROM policy_gates pg "
                "JOIN eval_definitions ed ON pg.eval_id = ed.id "
                "WHERE pg.action != ed.failure_behaviour"
            )
        )
        .fetchall()
    )
    if bad_actions:
        bad_ids = [str(r[0]) for r in bad_actions]
        raise RuntimeError(
            f"PolicyGate.action mismatch for {len(bad_ids)} row(s): {bad_ids[:10]}{'…' if len(bad_ids) > 10 else ''}"
        )


def _verify_fk_target() -> None:
    """Verify the FK now targets evals (spec criterion 8)."""
    result = _scalar(
        "SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'eval_results'::regclass "
        "  AND confrelid = 'evals'::regclass"
    )
    if not result:
        raise RuntimeError("FK eval_results→evals not found after repoint")


# ---------------------------------------------------------------------------
# upgrade()
# ---------------------------------------------------------------------------


def upgrade() -> None:
    if not _is_postgres():
        return

    # ---- Step 0: drain check (before any DDL / locks) ----
    logger.info("Step 0: drain check — waiting for in-flight runs to finish …")
    _drain_check()

    # ---- Step 1: object enumeration ----
    logger.info("Step 1: enumerating database objects referencing eval_definitions …")
    _enumerate_objects()

    # ---- Step 2: violation inventory table ----
    _create_violation_table()

    # ---- Step 2b: binding validation ----
    logger.info("Step 2b: validating PolicyGate bindings …")
    violations = _validate_bindings()
    violation_ids: set[uuid.UUID] = {v["eval_definition_id"] for v in violations}

    if violations:
        # Check for unexpected exclusions (not in known-expected set)
        for v in violations:
            unexpected = set(v["violated_exclusions"]) - _KNOWN_EXPECTED_EXCLUSIONS
            if unexpected:
                raise RuntimeError(
                    f"Unexpected binding violation(s) for eval {v['eval_definition_id']} "
                    f"({v['eval_name']}): {unexpected}. "
                    "Investigate before proceeding."
                )
        logger.warning(
            "Recorded %d known-expected binding violation(s). See eval_backfill_violations table.",
            len(violations),
        )
        _record_violations(violations)

    # Count expected PolicyGate rows (candidates minus rejections)
    expected_pg_count = _scalar(
        "SELECT COUNT(*) FROM eval_definitions "
        "WHERE deleted_at IS NULL AND node_id IS NOT NULL AND eval_type != 'guardrail'"
    ) - len(violations)

    # ---- Step 2c: promote evals.config_json to jsonb (0147 parity) ----
    logger.info("Step 2c: promoting evals.config_json json -> jsonb (0147 parity) …")
    _promote_evals_config_json()

    # ---- Steps 3–4: batched backfill ----
    logger.info("Step 3: backfilling evals …")
    eval_count = _backfill_evals()
    logger.info("Step 4: backfilling policy_gates …")
    pg_count = _backfill_policy_gates(violation_ids)

    # ---- Step 5: FK repoint ----
    logger.info("Step 5: repointing eval_results.eval_id FK → evals …")
    _repoint_fk()

    # ---- Step 5a: trigger recreation ----
    logger.info("Step 5a: recreating trg_eval_results_eval_id_tenant → evals …")
    _recreate_trigger()

    # ---- Steps 6–7: verification ----
    logger.info("Step 6: verifying row counts …")
    _verify_counts(expected_pg_count=expected_pg_count)
    logger.info("Step 7: verifying content correctness …")
    _verify_content()
    _verify_fk_target()

    # ---- Summary ----
    total_ed = _scalar("SELECT COUNT(*) FROM eval_definitions")
    logger.info(
        "Backfill complete.  eval_definitions=%d, evals_inserted=%d, policy_gates_inserted=%d, violations=%d",
        total_ed,
        eval_count,
        pg_count,
        len(violations),
    )


# ---------------------------------------------------------------------------
# downgrade()
# ---------------------------------------------------------------------------


def downgrade() -> None:
    if not _is_postgres():
        return

    # ---- Restore trigger → eval_definitions ----
    _execute(f'DROP TRIGGER IF EXISTS "{_TRIGGER_EVAL_RESULTS_EVAL_ID_TENANT}" ON eval_results')
    _execute(
        f'CREATE TRIGGER "{_TRIGGER_EVAL_RESULTS_EVAL_ID_TENANT}" '
        "BEFORE INSERT OR UPDATE OF eval_id, organisation_id ON public.eval_results "
        "FOR EACH ROW "
        "EXECUTE FUNCTION public.enforce_same_organisation('eval_definitions', 'eval_id')"
    )

    # ---- Restore FK → eval_definitions ----
    _execute(f'ALTER TABLE eval_results DROP CONSTRAINT IF EXISTS "{_FK_EVAL_RESULTS_EVAL_ID}"')
    _execute(
        f'ALTER TABLE eval_results ADD CONSTRAINT "{_FK_EVAL_RESULTS_EVAL_ID}" '
        "FOREIGN KEY (eval_id) REFERENCES eval_definitions(id) ON DELETE CASCADE"
    )

    # ---- Delete backfilled rows (guarded: skip PolicyGates with decisions) ----
    # policy_gate_decisions FK is RESTRICT, so we can only delete gates with
    # zero decisions.
    gates_with_decisions = _scalar(
        "SELECT COUNT(DISTINCT pg.id) FROM policy_gates pg JOIN policy_gate_decisions pgd ON pgd.policy_gate_id = pg.id"
    )
    if gates_with_decisions:
        logger.warning(
            "Cannot delete %d PolicyGate(s) that have decision records (FK RESTRICT).  They will remain as dead data.",
            gates_with_decisions,
        )
    _execute("DELETE FROM policy_gates WHERE id NOT IN (SELECT DISTINCT policy_gate_id FROM policy_gate_decisions)")

    # policy_gate_decisions also has ON DELETE RESTRICT to evals.id, so guard
    # the eval delete the same way.
    evals_with_decisions = _scalar("SELECT COUNT(DISTINCT pgd.eval_id) FROM policy_gate_decisions pgd")
    if evals_with_decisions:
        logger.warning(
            "Cannot delete %d Eval(s) referenced by policy_gate_decisions (FK RESTRICT).  "
            "They will remain as dead data.",
            evals_with_decisions,
        )
    _execute("DELETE FROM evals WHERE id NOT IN (SELECT DISTINCT eval_id FROM policy_gate_decisions)")

    # ---- Demote evals.config_json back to json (reverse Step 2c) ----
    _demote_evals_config_json()

    # ---- Drop audit table ----
    inspector = sa.inspect(op.get_bind())
    if "eval_backfill_violations" in inspector.get_table_names():
        op.drop_table("eval_backfill_violations")
