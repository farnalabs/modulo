# Drop of the legacy `runs` blob columns — runbook (FAR-583 / FAR-694, migration 0215)

Migration `0215_drop_runs_blob_columns` is the FINAL stage of the FAR-583
extraction chain: after it lands, the three whole-run JSON blobs
(`outputs_json`, `node_telemetry_json`, `raw_output_markers`) live ONLY in
`run_node_outputs` (plus the KEPT `run_node_outputs_quarantine` side table),
and the legacy `runs` columns are GONE.

Everything since B1 (deploy 2026-09-10T11:25:42Z, PR #298) writes the new
table only — the legacy columns were unwritten long before they are dropped.

## Preconditions — ALL must hold before the drop deploys

1. **Verified backup within the last 24h.** This is an irreversible stage:
   the backup must be VERIFIED, not merely scheduled (the
   `db-backup-monitor` lesson: an unmonitored backup schedule silently
   produced backups that did not restore). Check
   `docs/operations/backup.md`; confirm the latest backup completed AND a
   verification/read-back succeeded.
2. **Drain gates pass.** Run `tools/drain-gate-check.sql` against the target
   database (the pre-drop schema — Gate 3 reads the legacy columns) with the
   owner context; all three gate families must return ZERO (Gate 3b's
   caveat explained in the SQL comments).
3. **No deploy of an image missing 0215 to a DB that already ran it**
   (DB-ahead-of-image condition is owned by release.sh + the build-SHA
   match, never by a rewind — see below).
4. The release migrator (release.sh) retries the migration 3x — each abort
   re-runs the full drain/repair/parity verification, so a transient lock
   refusal self-heals; three consecutive aborts FATAL the release
   (deliberate, see the disposition in `deploy/fly/release.sh`).

## What the migration guarantees

1. Pre-flight drain assertion — aborts on any pre-B1 non-terminal run
   (running/claimed/awaiting_human/pending/hitl_parked with
   `created_at <` cutoff).
2. JSONB shape re-assertion + structural-anomaly aborts (a legacy blob that
   is neither a jsonb object nor a jsonb `null` value cannot be re-mapped by
   the 0192 dict semantics; the scans are quarantine-excluded).
3. INSERT-only repair of terminal/unknown pre-cutoff runs whose legacy blobs
   lack new-table representation (never overwrites).
4. Content parity — LEGACY-AUTHORITATIVE overwrite for terminal pre-cutoff
   runs (immutable post-drain), bounded per run; markers-subset parity with
   status-aware repair; PRESENT-key value divergence aborts.
5. `DROP INDEX` (the 0193 sweep index) + `DROP COLUMN` x3, each with
   `lock_timeout` + bounded savepoint retry.

## Emergency re-add-columns (the documented hand-rollback path)

The downgrade RAISES ("never rewind past this migration"). If the post-drop
image must be rolled back to a pre-drop image, restore EMPTY columns so the
old image boots:

```sql
ALTER TABLE runs ADD COLUMN outputs_json jsonb;
ALTER TABLE runs ADD COLUMN node_telemetry_json jsonb;
ALTER TABLE runs ADD COLUMN raw_output_markers jsonb;
-- Per run: fold the '__final__' rows back with the 0192 mapping semantics
-- (UNION of both dicts, metadata empty-flags folded back to '{}', markers
-- keyed by attempt_key). Quarantined runs restore from
-- run_node_outputs_quarantine (Python-side, never raw SQL).
```

The re-add columns come back EMPTY; re-folding the reassembled blobs back is
a documented HAND-rollback path (the snippet above). Re-running migration
0215 afterwards is idempotent (every leg is `ON CONFLICT DO NOTHING`) and
re-verifies parity before returning.

## NEVER rewind past this migration

`alembic downgrade` from 0215 RAISES by design. The legacy columns' content
is gone after the drop; rewinding would destroy the drop's whole point. The
DB-ahead-of-image condition is owned by release.sh's bounded migrator (see
the explicit disposition comment in `deploy/fly/release.sh`) and the deploy
workflow's build-SHA match — not by a chain rewind.

## Quarantine table disposition

`run_node_outputs_quarantine` is deliberately KEPT. Post-drop it is the ONLY
surviving copy of the 0192-quarantined sentinel-squatting evidence; the
retention purge's delete stays live (`run_retention._delete_quarantine_rows`).
Product readers under-serve quarantined runs (absent blob sides) — decided,
documented in `crud.run_node_outputs.read_run_blobs`.
