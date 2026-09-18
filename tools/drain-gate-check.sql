-- ============================================================================
-- drain-gate-check.sql (FAR-583 / FAR-694) — the reduced post-B2a gates.
--
-- Run these against the PRODUCTION (or staging) database BEFORE the drop
-- migration 0215 (the runs blob columns must still exist for Gate 3). Every
-- result must be ZERO. Run it as an owner-context role (modulo_migrate or
-- console): the queries scope the whole runs table deliberately — this is a
-- one-shot ops gate, not an app-role query.
--
-- If any gate returns nonzero: DO NOT deploy the drop revision. Escalate per
-- docs/operations/drop-runs-blob-columns.md (preconditions + remediation).
-- ============================================================================

-- B1 deploy cutoff (S1192: single definition used by all gates below).
-- Materialised as a session-scoped temporary VIEW so every gate below can
-- reference `b1_cutoff.cutoff` without re-declaring the literal four times.
-- A bare CTE only attaches to the single statement that follows it, which left
-- Gates 2/3a/3b referencing an undefined relation; a temp view is visible to
-- every statement run in this psql session.
DROP VIEW IF EXISTS b1_cutoff;
CREATE TEMPORARY VIEW b1_cutoff AS
  SELECT '2026-09-10 11:25:42+00'::timestamptz AS cutoff;

-- ---------------------------------------------------------------------------
-- Gate 1: zero dual_write_failed error_events since the B1 deploy cutoff.
-- B1 SHA 2026-09-10T11:25:42Z per PR #298. A dual-write failure after B1
-- means a run's store write failed-closed and its legacy columns were never
-- re-touched — every survivor must be verified before the columns drop.
-- ---------------------------------------------------------------------------
SELECT count(*) AS dual_write_failed_since_b1
FROM error_events, b1_cutoff
WHERE source = 'run_outputs_dual_write'
  AND position('dual-write failed' in message) > 0
  AND created_at >= b1_cutoff.cutoff;

-- ---------------------------------------------------------------------------
-- Gate 2: zero pre-B1 non-terminal runs (the drain gate the migration itself
-- re-asserts, with the full fail-safe status set incl. pending + hitl_parked).
-- ---------------------------------------------------------------------------
SELECT count(*) AS pre_b1_inflight
FROM runs r, b1_cutoff
WHERE r.status IN ('running', 'claimed', 'awaiting_human', 'pending', 'hitl_parked')
  AND r.created_at < b1_cutoff.cutoff;

-- ---------------------------------------------------------------------------
-- Gate 3a: post-B1-created runs carry NO legacy blobs (B1's write-cut makes
-- every legacy blob on a post-cutoff-created run a contract violation).
-- ---------------------------------------------------------------------------
SELECT count(*) AS post_b1_legacy_blobs
FROM runs r, b1_cutoff
WHERE r.created_at >= b1_cutoff.cutoff
  AND (r.outputs_json IS NOT NULL
       OR r.node_telemetry_json IS NOT NULL
       OR r.raw_output_markers IS NOT NULL);

-- ---------------------------------------------------------------------------
-- Gate 3b: post-B1-created terminalized runs must have NEW-TABLE rows.
-- NOTE: a terminal run whose agents produced NO output at all legitimately
-- has zero run_node_outputs rows — a nonzero result needs MANUAL review
-- (sample the run and its terminalization error) before treating it as a
-- blocker; do not abort the deploy on this query alone.
-- ---------------------------------------------------------------------------
SELECT count(*) AS post_b1_terminal_no_store_rows
FROM runs r
LEFT JOIN run_node_outputs n ON n.run_id = r.id, b1_cutoff
WHERE r.created_at >= b1_cutoff.cutoff
  AND r.status IN (
      'budget_exceeded', 'cancelled', 'compensation_failed', 'complete',
      'cost_ceiling_exceeded', 'eval_failed', 'failed', 'router_no_match', 'stalled'
  )
  AND n.run_id IS NULL;
