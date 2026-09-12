"""Drop the legacy ``runs`` blob columns — repair + content parity + DROP (FAR-583, the drop stage).

Revision ID: 0215_drop_runs_blob_columns
Revises: 0214_connector_instance_indexes_unique
Create Date: 2026-09-10

This is the FINAL stage of the FAR-583 extraction chain: after this migration
the three whole-run JSON blobs live ONLY in ``run_node_outputs`` and the
legacy ``runs`` columns are GONE. Built as the original 0212 revision (the
B2b/B2c split deferred it); renumbered here because 0212 and 0213/0214 merged
first — the content is the drop stage's gate-reviewed design.

One transaction, in order (Postgres path; SQLite drops the same objects):

1. **Pre-flight drain assertion** — any run still non-terminal (``running`` /
   ``claimed`` / ``awaiting_human`` / ``pending`` / ``hitl_parked``) with
   ``created_at <`` :data:`_B1_DEPLOY_CUTOFF` aborts the drop with the
   offending count. The drop must not proceed while pre-B1 runs are in flight:
   since B1 every write is new-table-only (legacy columns were never written
   again), so a still-in-flight pre-B1 run is a drain-gate defect whose
   evidence would be lost when the columns go. ``pending`` and
   ``hitl_parked`` are included as FAIL-SAFE (both were carved off B1's
   write-set — a run in either status cannot touch the legacy columns again,
   so the abort costs at most a delayed deploy and never spurious).
2. **INSERT-only repair** — TERMINAL runs (the markers leg also covers
   ``status = 'unknown'``; markers are durable mid-run evidence) with a
   legacy blob non-NULL AND the corresponding new-table rows absent get the
   missing rows INSERTed with ``ON CONFLICT DO NOTHING`` (never overwrites;
   full representation incl. metadata rows + ``'__unknown__'`` rows — the
   mapping semantics are the twin of migration 0192's backfill legs).
   Runs quarantined by 0192 (sentinel ``__``-prefixed keys) are EXCLUDED
   from every leg: their blobs cannot be represented in the new table
   (sentinel-squatting row ids). A jsonb ``'null'`` VALUE stored in a legacy
   side (0192's carved-out shape, qa M-guard here: the junk gate must not
   abort on it) is folded to the ABSENT-side semantic — it contributes no
   repair rows and never mismatches parity. The repair walk is BOUNDED per
   run (:data:`_MAX_REPAIR_ATTEMPTS`): a candidate whose repair legs insert
   ZERO rows on a pass (a non-quarantined blob the legs cannot map — e.g.
   a dict whose every key is ``__``-prefixed sentinel-squatting) is
   re-selected by the candidate walk FOREVER, so after the bound the
   migration aborts with the sample run id — the repair bound is the
   detector for un-round-trippable blobs (the parity overwrite bound below
   remains for the overwrite path).
3. **Content parity** — for PKs present in BOTH stores, scoped to runs
   TERMINAL and ``created_at < :b1_cutoff``:
   * ``outputs_json`` / ``node_telemetry_json``: the reassembled new-table
     value must ``IS NOT DISTINCT FROM`` the legacy value. The populations
     are reported (COUNT checked / COUNT excluded post-cutoff). A CONTENT
     mismatch is ROW-REPAIRED with a LEGACY-AUTHORITATIVE overwrite
     (terminal + pre-cutoff runs are immutable AFTER the drain gate — safe,
     self-healing): the run's ``__final__`` + metadata rows are deleted and
     re-mapped from the legacy column; the overwritten-run count is
     reported. The overwrite loop is BOUNDED per run
     (:data:`_MAX_OVERWRITE_ATTEMPTS`): a legacy blob that cannot round-trip
     through the 0192 mapping (e.g. a sentinel-squatting object key the
     `__`-prefixed filter drops) would otherwise re-flag on every pass and
     spin forever — the bound aborts with a LIMIT-1-style sample run id and
     the overflow count instead. (An unmapped blob whose new-table rows are
     entirely ABSENT never reaches this overwrite loop at all — the repair
     walk's own per-run bound aborts it first, see stage 2.)
   * ``raw_output_markers``: the legacy markers dict must be a jsonb SUBSET
     of the reassembled-new markers dict — post-B1 additions are legitimate
     (the new table may hold MORE keys), so a MISSING legacy key is
     repaired via the same INSERT-never-overwrites markers leg (STATUS-AWARE:
     an unknown-status run is repaired through the UNKNOWN filter variant —
     markers only — matching 0192's twin leg geometry, so the repair cannot
     be a guaranteed-zero-row statement misread as a divergence); a VALUE
     divergence on a PRESENT key aborts.
   * STRUCTURAL anomalies ABORT the migration with the anomaly count + one
     sample run id (LIMIT-1 short-circuit sample; the count from a separate
     aggregate query): a legacy blob that is non-NULL but NOT a jsonb object
     AND NOT a jsonb ``'null'`` VALUE (scalar / array — unparseable for the
     0192 dict semantics) cannot be re-mapped. The scan is NARROWED to the
     populations the repair/parity loops iterate (quarantine-excluded:
     terminal runs × outputs/telemetry; the markers walk's terminal+unknown
     population × markers) so quarantined evidence never spuriously aborts
     the irreversible migration.
4. **DROP ×4** — the 0193 sweep partial index
   (``ix_runs_org_completed_at_terminal_sweep``, its only consumer — the
   FAR-583 catch-up sweep — was removed at B2a) + ``DROP COLUMN`` x3.
   EVERY drop statement (index and columns alike) runs with ``lock_timeout``
   + a bounded retry inside a per-attempt SAVEPOINT (the 0129 stale-lock
   precedent; ACCESS EXCLUSIVE on ``runs`` can wedge the whole upward chain,
   so the index drop gets the same protection the columns already had). A
   refused attempt rolls back ONLY its savepoint and retries; after the
   final attempt the exception propagates (the whole chain aborts and
   release.sh retries the migration 3x).
5. **Downgrade = documented, RAISING no-op** ("never rewind past this
   migration" — emergency re-add-columns snippet in the downgrade docstring;
   release.sh's bounded migrator + the deploy workflow's build-SHA match own
   the DB-ahead-of-image condition, never a rewind).

QUARANTINE TABLE — KEPT, deliberately NOT dropped. Every remaining row is a
run 0192 quarantined because its legacy dicts carried ``__``-prefixed ids
(no lossless new-table representation exists). After this migration the
legacy columns are gone, so the quarantine side table is the ONLY surviving
copy of that evidence — dropping it would destroy data, so the B2a note's
"drop candidate" is DECIDED AGAINST. Its delete-on-purge consumer
(``run_retention._delete_quarantine_rows``) stays live; the drain-gate
"empty or dispositioned" instruction is satisfied by keeping the rows as
permanently-dispositioned evidence.

NO NUL PRE-SCAN: same disposition as 0192 — the columns are jsonb (asserted
again here before any leg) and jsonb structurally cannot contain ``\\u0000``.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.exc import OperationalError

revision: str = "0215_drop_runs_blob_columns"
down_revision: str | None = "0214_connector_instance_indexes_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "run_node_outputs"
_QUARANTINE_TABLE = "run_node_outputs_quarantine"
_SWEEP_INDEX_NAME = "ix_runs_org_completed_at_terminal_sweep"

# The three dropped runs blob columns (order used by every SQL fragment).
_RUNS_OUTPUT_COLS: tuple[str, ...] = ("outputs_json", "node_telemetry_json", "raw_output_markers")

# Inline terminal-status literal (migrations cannot import app constants).
# MUST equal sorted(db.models.run.TERMINAL_STATUSES) — pinned by the unit
# test (same twin discipline as 0192/0193).
_TERMINAL_RUN_STATUSES = (
    "budget_exceeded",
    "cancelled",
    "compensation_failed",
    "complete",
    "cost_ceiling_exceeded",
    "eval_failed",
    "failed",
    "router_no_match",
    "stalled",
)

_TERMINAL_SQL = "(" + ", ".join(f"'{status}'" for status in _TERMINAL_RUN_STATUSES) + ")"

# The non-terminal states the drain gate patrols. ``pending`` and
# ``hitl_parked`` (FAR-604 D2) are added for the same fail-safe reason as the
# original trio: neither status can write the legacy columns post-B1, so
# including them is zero-cost when the gate is clean and aborts the drop
# loudly when a pre-B1 parked run's legacy markers would otherwise be
# silently destroyed alongside the columns.
_INFLIGHT_RUN_STATUSES = ("awaiting_human", "claimed", "hitl_parked", "pending", "running")
_INFLIGHT_SQL = "(" + ", ".join(f"'{status}'" for status in _INFLIGHT_RUN_STATUSES) + ")"

# The pre-B1 statuses the REPAIR + MARKERS legs cover: terminal runs plus
# the 'unknown'-status evidence runs (0192's markers-leg twin).
_REPAIR_STATUS_SQL = f"r.status IN {_TERMINAL_SQL} OR r.status = 'unknown'"

# The B1 deploy cutoff — B1 merged 2026-09-10T11:25:42Z per PR #298 (kept
# in the comment so the pinned-cutoff unit test can grep the instant). The
# pre-flight drain assertion and the content-parity populations are both
# bounded to runs created BEFORE this instant (post-B1-created runs write
# the new table unconditionally and can never carry a legacy blob). Bound
# as a REAL datetime — asyncpg requires typed datetime params (a string
# would raise a DataError on the timestamp comparison under every driver
# whose DBAPI is not text-typed).
_B1_DEPLOY_CUTOFF = datetime(2026, 9, 10, 11, 25, 42, tzinfo=UTC)

_DROP_COL_LOCK_TIMEOUT = "30s"
_DROP_COL_RETRIES = 3
_DROP_COL_RETRY_SLEEP_SECONDS = 1.0
_PARITY_BATCH = 200

# qa gate, fix 7: the LEGACY-AUTHORITATIVE overwrite loop is bounded per run.
# A legacy blob that cannot round-trip through the 0192 mapping would re-flag
# on every pass (the overwrite re-maps the SAME bytes) — an unbounded loop
# spins forever. After this many per-run overwrite attempts the migration
# aborts with the sample run id instead.
_MAX_OVERWRITE_ATTEMPTS = 2

# Review gate (PR #400, MAJOR): the repair walk is bounded per run too. The
# candidate predicate keys on new-table rows being ABSENT, so a run whose
# repair legs insert ZERO rows every pass (a non-quarantined blob the legs
# cannot map — e.g. an out/tel dict whose every key is `__`-prefixed
# sentinel-squatting: the out/tel + markers legs filter `__`-prefixed keys
# and the metadata leg needs a '{}' side) is re-selected FOREVER — spinning
# the `while candidates` loop before the parity overwrite bound can ever run
# (under release.sh's 3x retry that is a deploy HANG, not a loud abort).
# Mirror of :data:`_MAX_OVERWRITE_ATTEMPTS`: a run still a candidate after
# this many zero-progress repair passes aborts the migration with the sample
# run id + count.
_MAX_REPAIR_ATTEMPTS = 2

_META_NODE_ID = "__run_meta__"
_FINAL_ATTEMPT_KEY = "__final__"
_UNKNOWN_NODE_ID = "__unknown__"

# Anchored marker-key grammar — MUST stay byte-identical to
# crud.run_node_outputs._MARKER_KEY_RE (pinned by
# tests/unit/db/test_migration_run_node_outputs.py). Bound everywhere as
# :marker_re, never interpolated.
_MARKER_KEY_RE = r"^run:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:node:.+$"
_MARKER_KEY_PREFIX_LEN = 46

# 0192-quarantined runs: excluded from every leg (their legacy blobs cannot
# map to the sentinel-namespace-ed representation; their evidence lives in
# the KEPT quarantine side table).
_NOT_QUARANTINED_SQL = f"NOT EXISTS (SELECT 1 FROM {_QUARANTINE_TABLE} q WHERE q.run_id = r.id)"  # noqa: S608  # nosec B608 - module constants only

# ---------------------------------------------------------------------------
# SQL constants. Every statement is a pure literal or composed ONLY from
# other module constants; caller data (run ids, the cutoff, the marker
# regex, batch sizes) rides in as bound parameters (0192 statement-assembly
# style).
#
# TENANCY SCOPING of the repair + parity statements below (qa, minors): the
# walk and per-run repair bodies scope by RUN ID ONLY and run under the
# migration's OWNER context (modulo_migrate / the console role a self-hosted
# release.sh runs under). Scoping every one-shot repair statement per org
# would require per-org thread runs for a statement that runs exactly ONCE
# over the whole table — so run-id scoping under the owner role IS the
# explicit tenancy model here (0192's chunked sweeps did the same). NEVER
# reuse these statements in a modulo_app (NOBYPASSRLS) session expecting
# per-org scoping: there is none — their population predicates bound them.
# ---------------------------------------------------------------------------

# 1) Pre-flight drain assertion: any pre-B1 in-flight run aborts the drop.
_DRAIN_COUNT_SQL = (
    "SELECT count(*) FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE r.status IN {_INFLIGHT_SQL} AND r.created_at < :b1_cutoff"
)

# jsonb-shape re-assertion before any leg (the no-NUL-pre-scan deviation of
# 0192 still depends on it). RAISE-format only (no string concatenation).
_SHAPE_ASSERT_SQL = (
    "DO $far583drop$ "  # noqa: S608  # nosec B608 - module constants only
    "DECLARE bad text; "
    "BEGIN "
    "SELECT string_agg(column_name, ', ') INTO bad FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'runs' "
    f"AND column_name IN ({', '.join(repr(col) for col in _RUNS_OUTPUT_COLS)}) "
    "AND data_type <> 'jsonb'; "
    "IF bad IS NOT NULL THEN "
    "RAISE EXCEPTION 'FAR-583 drop: runs blob column(s) % are not jsonb "
    "- the no-NUL-pre-scan deviation requires the jsonb invariant (0129/0147/0199)', bad; "
    "END IF; "
    "END $far583drop$;"
)

# jsonb-null fold (qa gate, fix 3): a legacy side holding the jsonb ``'null'``
# VALUE (column non-NULL, jsonb_typeof = 'null' — 0192 explicitly carved this
# shape out of junk) is folded to the ABSENT-side semantic everywhere: it
# counts as "no blob" in the walks and never mismatches parity (the folded
# comparison below NULL-folds it, so jsonb-null vs a row-absent reassembly is
# NOT an anomaly). SQL NULL and jsonb-null both mean the side is absent —
# exactly the 0192 _ANY_BLOB_OBJECT_SQL comment's semantics.
# adds an outermost-parentheses wrapper at EVERY splice site; these constants
# therefore carry their OWN outer parens (the 0192 module-constant assembly
# style splices with bare AND) — never splice without them.
_HAS_OUT_TEL_BLOB_SQL = (
    "((r.outputs_json IS NOT NULL AND jsonb_typeof(r.outputs_json) IS DISTINCT FROM 'null') "
    "OR (r.node_telemetry_json IS NOT NULL AND jsonb_typeof(r.node_telemetry_json) IS DISTINCT FROM 'null'))"
)
_FOLDED_OUT_SQL = "(CASE WHEN jsonb_typeof(r.outputs_json) = 'object' THEN r.outputs_json END)"
_FOLDED_TEL_SQL = "(CASE WHEN jsonb_typeof(r.node_telemetry_json) = 'object' THEN r.node_telemetry_json END)"

# Structural-anomaly predicates: non-NULL legacy blob that is neither a jsonb
# object NOR a jsonb 'null' VALUE (scalar / array — unparseable for the 0192
# dict semantics; the jsonb-null value itself is folded to absent, NOT junk).
# qa gate, fixes 2+3:
#   * each scan NEEDS the quarantine exclusion — quarantined runs are
#     excluded from every leg, so flagging (say) their unparseable blobs
#     would abort the migration on evidence that is by design unusable
#     here (and a legacy scalar 0192 would ALSO have quarantined — the
#     blanket scan counting those rows again is the spuriously-aborting
#     shape the gate called out);
#   * the scans are NARROWED to exactly the populations the loops iterate:
#     the outputs/telemetry legs run for TERMINAL pre-cutoff runs only, so
#     junk out/tel blobs on unknown-status runs cannot block them; the
#     markers loops iterate terminal+unknown runs, so junk markers scan
#     that population only.
_JUNK_OUT_TEL_GUARD_SQL = (
    "((r.outputs_json IS NOT NULL AND jsonb_typeof(r.outputs_json) IS DISTINCT FROM 'null' "
    " AND jsonb_typeof(r.outputs_json) IS DISTINCT FROM 'object') "
    "OR (r.node_telemetry_json IS NOT NULL AND jsonb_typeof(r.node_telemetry_json) IS DISTINCT FROM 'null' "
    " AND jsonb_typeof(r.node_telemetry_json) IS DISTINCT FROM 'object'))"
)
_JUNK_MARKERS_GUARD_SQL = (
    "r.raw_output_markers IS NOT NULL AND jsonb_typeof(r.raw_output_markers) IS DISTINCT FROM 'null' "
    "AND jsonb_typeof(r.raw_output_markers) IS DISTINCT FROM 'object'"
)
_JUNK_OUT_TEL_POPULATION_SQL = (
    "SELECT count(*) FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE {_JUNK_OUT_TEL_GUARD_SQL} "
    f"AND r.status IN {_TERMINAL_SQL} AND r.created_at < :b1_cutoff "
    f"AND {_NOT_QUARANTINED_SQL}"
)
_JUNK_OUT_TEL_SAMPLE_SQL = (
    "SELECT r.id FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE {_JUNK_OUT_TEL_GUARD_SQL} "
    f"AND r.status IN {_TERMINAL_SQL} AND r.created_at < :b1_cutoff "
    f"AND {_NOT_QUARANTINED_SQL} LIMIT 1"
)
_JUNK_MARKERS_POPULATION_SQL = (
    "SELECT count(*) FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE ({_JUNK_MARKERS_GUARD_SQL}) AND ({_REPAIR_STATUS_SQL}) "
    "AND r.created_at < :b1_cutoff "
    f"AND {_NOT_QUARANTINED_SQL}"
)
_JUNK_MARKERS_SAMPLE_SQL = (
    "SELECT r.id FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE ({_JUNK_MARKERS_GUARD_SQL}) AND ({_REPAIR_STATUS_SQL}) "
    "AND r.created_at < :b1_cutoff "
    f"AND {_NOT_QUARANTINED_SQL} LIMIT 1"
)
# Repair-leg run filters (per-run statements):
# * TERMINAL — repair the outputs/telemetry + metadata + markers legs.
# * UNKNOWN — markers only (0192's twin runs the MARKERS leg ONLY for
#   unknown-status runs: markers are the durable mid-run evidence, while a
#   still-live run's outputs/telemetry reassembly has no terminal semantic —
#   mapping them would plant '__final__' content rows the reader would then
#   serve as the run's authoritative final outputs).
_CUTOFF_PREDICATE = "created_at < :b1_cutoff"
_TERMINAL_RUN_FILTER_SQL = f"r.id = :run_id AND r.status IN {_TERMINAL_SQL} AND r.{_CUTOFF_PREDICATE} "
_UNKNOWN_RUN_FILTER_SQL = "r.id = :run_id AND r.status = 'unknown' AND r." + _CUTOFF_PREDICATE + " "

# Leg 2a: '__final__' outputs/telemetry rows from the UNION of both dicts'
# keys (0192 _OUTPUTS_TELEMETRY_LEG_SQL twin; window gone, per-run filter).
# The keys carry the __ SENTINEL FILTER (qa gate, fix 7): a legacy dict key
# that is __-prefixed would squat the reserved node-id namespace (a literal
# '__run_meta__' key collides with the metadata row's PK and its content is
# dropped by ON CONFLICT DO NOTHING, breaking the round-trip 0192-era rows
# already prevent — quarantine captured sentinel runs inside 0192's window;
# outside it the REPAIR LOOP's own per-run bound is the detector (a
# filter-everything blob leaves the run a zero-insertion candidate forever
# — the repair loop precedes parity, so the parity overwrite bound can
# never see that shape). Parseable
# keys ride through the UNION DISTINCT folding; jsonb-null sides yield no
# keys via the CASE folding (absent semantic).
_REPAIR_OUT_TEL_BODY_SQL = (
    "INSERT INTO run_node_outputs "  # noqa: S608  # nosec B608 - module constants only
    "(run_id, node_id, attempt_key, organisation_id, outputs_json, node_telemetry_json, created_at, updated_at) "
    "SELECT r.id, k, '__final__', r.organisation_id, "
    "CASE WHEN jsonb_typeof(r.outputs_json) = 'object' THEN r.outputs_json -> k END, "
    "CASE WHEN jsonb_typeof(r.node_telemetry_json) = 'object' THEN r.node_telemetry_json -> k END, "
    "now(), now() "
    "FROM runs r "
    "CROSS JOIN LATERAL (SELECT DISTINCT k FROM ("
    "SELECT jsonb_object_keys(CASE WHEN jsonb_typeof(r.outputs_json) = 'object' THEN r.outputs_json END) AS k "
    "UNION "
    "SELECT jsonb_object_keys(CASE WHEN jsonb_typeof(r.node_telemetry_json) = 'object' "
    "THEN r.node_telemetry_json END)"
    ") u WHERE left(u.k, 2) <> '__') keys "
    "WHERE :run_filter "
    f"AND {_NOT_QUARANTINED_SQL} "
    "AND (jsonb_typeof(r.outputs_json) = 'object' OR jsonb_typeof(r.node_telemetry_json) = 'object') "
    "ON CONFLICT DO NOTHING"
)

# Leg 2b: the metadata row for '{}' sides (0192 _METADATA_LEG_SQL twin).
_REPAIR_META_BODY_SQL = (
    "INSERT INTO run_node_outputs "  # noqa: S608  # nosec B608 - module constants only
    "(run_id, node_id, attempt_key, organisation_id, outputs_json, created_at, updated_at) "
    "SELECT r.id, '__run_meta__', '__final__', r.organisation_id, "
    "jsonb_build_object('empty_outputs', COALESCE((r.outputs_json = '{}'::jsonb), false), "
    "'empty_telemetry', COALESCE((r.node_telemetry_json = '{}'::jsonb), false)), "
    "now(), now() "
    "FROM runs r "
    "WHERE :run_filter "
    f"AND {_NOT_QUARANTINED_SQL} "
    "AND (r.outputs_json = '{}'::jsonb OR r.node_telemetry_json = '{}'::jsonb) "
    "ON CONFLICT DO NOTHING"
)

# Leg 2c: one marker row per legacy markers key (0192 marker-leg twin).
_MARKER_REMAINDER_SQL = f"substr(mk.key, {_MARKER_KEY_PREFIX_LEN + 1})"
_MARKER_LAST_COLON_SQL = f"position(':' IN reverse({_MARKER_REMAINDER_SQL}))"
# Anchored twin partial: parseable iff the anchored regex matches AND the
# remainder's LAST colon leaves a non-empty suffix AND a non-empty node id;
# unparseable keys become '__unknown__' with the FULL original key.
_MARKER_NODE_ID_SQL = (
    f"CASE WHEN mk.key ~ :marker_re "
    f"AND {_MARKER_LAST_COLON_SQL} BETWEEN 2 AND length({_MARKER_REMAINDER_SQL}) - 1 "
    f"THEN substr({_MARKER_REMAINDER_SQL}, 1, length({_MARKER_REMAINDER_SQL}) - {_MARKER_LAST_COLON_SQL}) "
    f"ELSE '{_UNKNOWN_NODE_ID}' END"
)
_REPAIR_MARKERS_BODY_SQL = (
    "INSERT INTO run_node_outputs "  # noqa: S608  # nosec B608 - module constants only
    "(run_id, node_id, attempt_key, organisation_id, raw_output_markers, created_at, updated_at) "
    f"SELECT r.id, {_MARKER_NODE_ID_SQL}, mk.key, r.organisation_id, mk.value, now(), now() "
    "FROM runs r "
    "CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(r.raw_output_markers) = 'object' "
    "THEN r.raw_output_markers END) AS mk(key, value) "
    "WHERE :run_filter "
    f"AND {_NOT_QUARANTINED_SQL} "
    "AND left(mk.key, 2) <> '__' "
    "ON CONFLICT DO NOTHING"
)

# Repair candidate walks: runs whose new-table rows are ABSENT.
_ABSENT_FINAL_ROW_PREDICATE_SQL = (
    f"NOT EXISTS (SELECT 1 FROM {_TABLE} n WHERE n.run_id = r.id AND n.attempt_key = '{_FINAL_ATTEMPT_KEY}')"  # noqa: S608  # nosec B608 - module constants only
)
_ABSENT_META_ROW_PREDICATE_SQL = (
    f"NOT EXISTS (SELECT 1 FROM {_TABLE} n WHERE n.run_id = r.id "  # noqa: S608  # nosec B608 - module constants only
    f"AND n.node_id = '{_META_NODE_ID}' AND n.attempt_key = '{_FINAL_ATTEMPT_KEY}')"
)
# A metadata row is RELEVANT only for a '{}' side: a run with a meta row
# absent but no '{}' side needs nothing (the metadata row exists iff a
# legacy side was '{}'). Without this coupling a meta-absent run that
# needs no metadata row would loop forever as a phantom repair candidate.
_ABSENT_META_RELEVANT_PREDICATE_SQL = (
    f"{_ABSENT_META_ROW_PREDICATE_SQL} AND (r.outputs_json = '{{}}'::jsonb OR r.node_telemetry_json = '{{}}'::jsonb)"
)
_REPAIR_CANDIDATES_TERMINAL_SQL = (
    "SELECT r.id FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE r.status IN {_TERMINAL_SQL} AND r.created_at < :b1_cutoff "
    f"AND {_HAS_OUT_TEL_BLOB_SQL} "  # jsonb-null folded to absent (no phantom candidates)
    f"AND {_NOT_QUARANTINED_SQL} "
    f"AND ({_ABSENT_FINAL_ROW_PREDICATE_SQL} OR {_ABSENT_META_RELEVANT_PREDICATE_SQL}) "
    "ORDER BY r.id LIMIT :batch"
)
# Some keys with no new-table row yet — the loop TERMINATION condition for
# the unknown-status population. (The '__final__'-absence predicate can
# never become FALSE for a markers-only run: the unknown population inserts
# marker rows keyed by their ORIGINAL attempt_key, never '__final__' rows,
# so a candidate filtered on __final__-absence would be selected forever.)
_MISSING_MARKER_KEY_PREDICATE_SQL = (
    "EXISTS (SELECT 1 FROM jsonb_each(r.raw_output_markers) AS e(key, value) "  # noqa: S608  # nosec B608 - module constants only
    "WHERE left(e.key, 2) <> '__' "
    f"AND NOT EXISTS (SELECT 1 FROM {_TABLE} n WHERE n.run_id = r.id AND n.attempt_key = e.key))"
)
_REPAIR_CANDIDATES_UNKNOWN_SQL = (
    "SELECT r.id FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    "WHERE r.status = 'unknown' AND r.created_at < :b1_cutoff "
    "AND jsonb_typeof(r.raw_output_markers) = 'object' AND r.raw_output_markers <> '{}'::jsonb "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"AND {_ABSENT_FINAL_ROW_PREDICATE_SQL} "
    f"AND {_MISSING_MARKER_KEY_PREDICATE_SQL} "
    "ORDER BY r.id LIMIT :batch"
)

# ---------------------------------------------------------------------------
# Parity-step SQL.
# ---------------------------------------------------------------------------


def _side_agg(col: str) -> str:
    """jsonb_object_agg of one '__final__' side for run r (server-side)."""
    return (
        "(SELECT jsonb_object_agg(n.node_id, n." + col + ") FROM " + _TABLE + " n "  # noqa: S608  # nosec B608 - module constants only
        f"WHERE n.run_id = r.id AND n.attempt_key = '{_FINAL_ATTEMPT_KEY}' "
        f"AND n.node_id <> '{_META_NODE_ID}' AND n." + col + " IS NOT NULL)"
    )


_SIDE_AGG_OUT_SQL = _side_agg("outputs_json")
_SIDE_AGG_TEL_SQL = _side_agg("node_telemetry_json")

# Reassembled side expressions (crud.run_node_outputs._assemble twin): the
# metadata empty-flag TRUE -> '{}'; else the per-node aggregation (an empty
# aggregation = None = the side is ABSENT).
_REASM_OUT_SQL = (
    f"(COALESCE("  # noqa: S608  # nosec B608 - module constants only
    f"(CASE WHEN EXISTS (SELECT 1 FROM {_TABLE} m WHERE m.run_id = r.id "
    f"AND m.attempt_key = '{_FINAL_ATTEMPT_KEY}' AND m.node_id = '{_META_NODE_ID}' "
    "AND m.outputs_json ->> 'empty_outputs' = 'true') THEN '{}'::jsonb END), "
    f"{_SIDE_AGG_OUT_SQL}))"
)
_REASM_TEL_SQL = (
    f"(COALESCE("  # noqa: S608  # nosec B608 - module constants only
    f"(CASE WHEN EXISTS (SELECT 1 FROM {_TABLE} m WHERE m.run_id = r.id "
    f"AND m.attempt_key = '{_FINAL_ATTEMPT_KEY}' AND m.node_id = '{_META_NODE_ID}' "
    "AND m.outputs_json ->> 'empty_telemetry' = 'true') THEN '{}'::jsonb END), "
    f"{_SIDE_AGG_TEL_SQL}))"
)

# Outputs/telemetry parity MISMATCH walk (pre-cutoff TERMINAL runs carrying
# a MEANINGFUL blob; quarantined runs are excluded; the LEGACY side is
# JSONB-NULL-FOLDED here so a stored jsonb 'null' value compares equal to an
# absent (row-less) reassembly instead of flagging → overwriting → STILL
# flagging → spinning forever).
_PARITY_OUT_TEL_MISMATCH_SQL = (
    "SELECT r.id FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE r.status IN {_TERMINAL_SQL} AND r.created_at < :b1_cutoff "
    f"AND {_HAS_OUT_TEL_BLOB_SQL} "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"AND ({_FOLDED_OUT_SQL} IS DISTINCT FROM {_REASM_OUT_SQL} "
    f"OR {_FOLDED_TEL_SQL} IS DISTINCT FROM {_REASM_TEL_SQL}) "
    "ORDER BY r.id LIMIT :batch"
)

# Populations (COUNT checked + COUNT post-cutoff), per blob family — folded
# to the same meaningful-blob semantics the walks use (reporting accuracy).
_PARITY_OUT_POPULATION_SQL = (
    "SELECT "  # noqa: S608  # nosec B608 - module constants only
    "count(*) FILTER (WHERE r.created_at < :b1_cutoff) AS checked, "
    "count(*) FILTER (WHERE r.created_at >= :b1_cutoff) AS post_cutoff "
    f"FROM runs r WHERE r.status IN {_TERMINAL_SQL} "
    f"AND {_HAS_OUT_TEL_BLOB_SQL}"
)
_PARITY_MARKERS_POPULATION_SQL = (
    "SELECT "  # noqa: S608  # nosec B608 - module constants only
    "count(*) FILTER (WHERE r.created_at < :b1_cutoff) AS checked, "
    "count(*) FILTER (WHERE r.created_at >= :b1_cutoff) AS post_cutoff "
    f"FROM runs r WHERE ({_REPAIR_STATUS_SQL}) "
    "AND r.raw_output_markers IS NOT NULL "
    "AND jsonb_typeof(r.raw_output_markers) IS DISTINCT FROM 'null'"
)

# Markers-subset violation walk. jsonb `<@` = deep subset; a MISSING key and
# a differing VALUE both fail. (Post-B1 marker ADDITIONS are legitimate —
# the new table may hold MORE keys.) The walk RETURNS THE RUN'S STATUS:
# the repair leg variant is STATUS-AWARE — an unknown-status run repairs
# through the UNKNOWN filter variant (markers only), so a zero-row insert
# on an unknown run means a genuine PRESENT-key divergence, never the
# guaranteed-zero-row repair the old TERMINAL-only variant produced for
# unknown runs (qa gate, fix 5).
_REASM_MARKERS_SQL = (
    f"(COALESCE("  # noqa: S608  # nosec B608 - module constants only
    f"(SELECT jsonb_object_agg(n.attempt_key, n.raw_output_markers) FROM {_TABLE} n "
    f"WHERE n.run_id = r.id AND n.attempt_key <> '{_FINAL_ATTEMPT_KEY}' "
    "AND n.raw_output_markers IS NOT NULL), "
    "'{}'::jsonb))"
)
_PARITY_MARKERS_MISMATCH_SQL = (
    "SELECT r.id, r.status FROM runs r "  # noqa: S608  # nosec B608 - module constants only
    f"WHERE ({_REPAIR_STATUS_SQL}) AND r.created_at < :b1_cutoff "
    "AND jsonb_typeof(r.raw_output_markers) = 'object' AND r.raw_output_markers <> '{}'::jsonb "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"AND NOT (r.raw_output_markers <@ {_REASM_MARKERS_SQL}) "
    "ORDER BY r.id LIMIT :batch"
)

# The overwrite reset: delete ONE run's '__final__' (incl. metadata) rows
# before the repair legs re-map the LEGACY values in.
_DELETE_FINAL_ROWS_SQL = f"DELETE FROM {_TABLE} WHERE run_id = :run_id AND attempt_key = '{_FINAL_ATTEMPT_KEY}'"  # noqa: S608  # nosec B608 - module constants only

# alembic routing (0192 note): INFO ONLY surfaces to `alembic upgrade`
# output via a child of `alembic.runtime`; the pinned module-level-getLogger
# lint exception is documented here.
_ALEMBIC_RUNTIME_LOGGER = "alembic.runtime.migration"
_log = logging.getLogger(_ALEMBIC_RUNTIME_LOGGER)


def _is_postgres(bind: sa.Connection) -> bool:
    """Postgres-only legs (drain/repair/parity) vs the SQLite parity path."""
    return bool(bind.dialect.name == "postgresql")


def _count_sql(bind: sa.Connection, sql: str, params: dict[str, object] | None = None) -> int:
    value = bind.execute(sa.text(sql), params or {}).scalar_one()
    return int(value or 0)


def _run_sql(bind: sa.Connection, sql: str, params: dict[str, object]) -> int:
    """Execute one mutation and return the affected-row count."""
    return int(bind.execute(sa.text(sql), params).rowcount or 0)


def _repair_params(run_id: uuid.UUID) -> dict[str, object]:
    """Binds only VALUES: :run_id the run, :b1_cutoff the pinned datetime,
    :marker_re the anchored grammar (module constants — never caller data)."""
    return {
        "run_id": run_id,
        "b1_cutoff": _B1_DEPLOY_CUTOFF,
        "marker_re": _MARKER_KEY_RE,
    }


def _repair_body(body: str, *, unknown_run: bool) -> str:
    """Inline the STATUS-variant per-run filter into a standby body SQL.

    A SQL fragment can never be a bound parameter (psycopg would bind it as
    a string and the boolean context would reject it), so the per-run
    filter — a MODULE CONSTANT either way — is selected and spliced into
    the WHERE clause here. The splice text is never caller data.
    """
    variant = _UNKNOWN_RUN_FILTER_SQL if unknown_run else _TERMINAL_RUN_FILTER_SQL
    return body.replace("WHERE :run_filter ", "WHERE " + variant + " ")


def _repair_run(bind: sa.Connection, run_id: uuid.UUID, *, unknown_run: bool) -> dict[str, int]:
    """INSERT-only repair of ONE run's absent rows (0192 leg twin).

    STATUS-AWARE (qa gate, fix 6): an UNKNOWN-status run repairs through the
    MARKERS leg ONLY — 0192's twin runs the markers leg only for unknown
    runs (markers are the durable mid-run evidence; an unknown-status run's
    outputs/telemetry have no terminal semantic and mapping them would plant
    '__final__' content rows the reader would serve as the run's final
    data). The repair body statements' legs are bounded per-run INSERTs with
    ``ON CONFLICT DO NOTHING`` — never overwrites.
    """
    params = _repair_params(run_id)
    if unknown_run:
        return {"markers": _run_sql(bind, _repair_body(_REPAIR_MARKERS_BODY_SQL, unknown_run=True), params)}
    out_tel = _run_sql(bind, _repair_body(_REPAIR_OUT_TEL_BODY_SQL, unknown_run=False), params)
    meta = _run_sql(bind, _repair_body(_REPAIR_META_BODY_SQL, unknown_run=False), params)
    markers = _run_sql(bind, _repair_body(_REPAIR_MARKERS_BODY_SQL, unknown_run=False), params)
    return {"final": out_tel, "meta": meta, "markers": markers}


def _repair_candidates(bind: sa.Connection, *, terminal: bool, batch: int) -> list[uuid.UUID]:
    sql = _REPAIR_CANDIDATES_TERMINAL_SQL if terminal else _REPAIR_CANDIDATES_UNKNOWN_SQL
    rows = bind.execute(sa.text(sql), {"b1_cutoff": _B1_DEPLOY_CUTOFF, "batch": batch})
    return [uuid.UUID(str(row[0])) for row in rows.all()]


def _repair_population(bind: sa.Connection, *, terminal: bool, inserted: dict[str, int]) -> int:
    """One bounded repair walk over ONE population (terminal or unknown).

    The candidate walk re-selects any run whose new-table rows are still
    ABSENT, so a run whose repair legs insert ZERO rows on a pass (a blob
    the legs cannot map — e.g. every out/tel dict key `__`-prefixed
    sentinel-squatting on a non-quarantined run) is re-selected FOREVER and
    the `while candidates` loop would spin before the parity overwrite bound
    could ever run (a deploy HANG under release.sh's retry, not a loud
    abort). BOUNDED per run (:data:`_MAX_REPAIR_ATTEMPTS`, mirror of the
    parity overwrite bound): a run that consumed
    :data:`_MAX_REPAIR_ATTEMPTS` passes while STILL a candidate aborts the
    migration with the sample run id + attempt count.

    Returns the number of rows the population's legs inserted; increments
    the caller's ``inserted`` tally per leg in place.
    """
    attempts: dict[uuid.UUID, int] = {}
    rows_inserted = 0
    while candidates := _repair_candidates(bind, terminal=terminal, batch=_PARITY_BATCH):
        for run_id in candidates:
            leg_counts = _repair_run(bind, run_id, unknown_run=not terminal)
            if not any(leg_counts.values()):
                attempts[run_id] = attempts.get(run_id, 0) + 1
                if attempts[run_id] >= _MAX_REPAIR_ATTEMPTS:
                    raise RuntimeError(
                        "FAR-583 drop repair bound exceeded: "
                        f"run_id={run_id} remained a repair candidate after {attempts[run_id]} pass(es) that "
                        f"inserted ZERO rows (the repair legs cannot map its legacy blob to the new table — "
                        "e.g. every key is `__`-prefixed sentinel-squatting on a non-quarantined run). "
                        "The blob is NOT round-trippable through the 0192 mapping semantics — "
                        "remediate by hand (or quarantine the run), then re-run."
                    )
            else:
                attempts.pop(run_id, None)
                for leg, count in leg_counts.items():
                    inserted[leg] += count
                    rows_inserted += count
    return rows_inserted


def _repair_installation(bind: sa.Connection) -> dict[str, int]:
    """INSERT-only repair of BOTH populations (terminal + unknown-status).

    The terminal population runs the FULL leg set (out/tel + metadata +
    markers); the unknown population runs the MARKERS LEG ONLY (0192 twin
    geometry — see :func:`_repair_run`). Both walks are BOUNDED per run
    (see :func:`_repair_population`).
    """
    inserted = {"final": 0, "meta": 0, "markers": 0}
    _repair_population(bind, terminal=True, inserted=inserted)
    _repair_population(bind, terminal=False, inserted=inserted)
    return inserted


def _parity_outputs_telemetry(bind: sa.Connection) -> int:
    """Content parity for outputs/telemetry — legacy-authoritative overwrite.

    Scopes: TERMINAL + pre-cutoff runs carrying a meaningful legacy blob.
    Returns the count of overwritten runs. BOUNDED per run: a run still
    mismatched after :data:`_MAX_OVERWRITE_ATTEMPTS` overwrite rounds aborts
    the migration (the legacy blob cannot round-trip through the 0192
    mapping — spinning forever is not an option on an irreversible stage).
    """
    overwritten = 0
    attempts: dict[uuid.UUID, int] = {}
    while True:
        rows = bind.execute(
            sa.text(_PARITY_OUT_TEL_MISMATCH_SQL), {"b1_cutoff": _B1_DEPLOY_CUTOFF, "batch": _PARITY_BATCH}
        ).all()
        run_ids = [uuid.UUID(str(row[0])) for row in rows]
        if not run_ids:
            return overwritten
        for run_id in run_ids:
            attempts[run_id] = attempts.get(run_id, 0) + 1
            if attempts[run_id] > _MAX_OVERWRITE_ATTEMPTS:
                raise RuntimeError(
                    "FAR-583 drop parity bound exceeded: "
                    f"run_id={run_id} still diverged after {_attempts_word(attempts[run_id])} LEGACY-AUTHORITATIVE "
                    f"overwrite(s) ({attempts[run_id]} total repair attempts on this run). The legacy blob is "
                    "NOT round-trippable through the 0192 mapping semantics — remediate by hand, then re-run."
                )
            # Legacy-authoritative overwrite: reset, then re-map (the delete
            # guarantees emptiness so the inserts cannot conflict).
            bind.execute(sa.text(_DELETE_FINAL_ROWS_SQL), {"run_id": run_id})
            _repair_run(bind, run_id, unknown_run=False)
            overwritten += 1


def _attempts_word(count: int) -> str:
    return "once" if count == 1 else f"{count} times"


def _parity_markers(bind: sa.Connection) -> tuple[int, int, uuid.UUID | None]:
    """Markers-subset parity: missing legacy keys INSERT-repaired; a value
    divergence on a PRESENT key aborts the scan (returned to raise).

    STATUS-AWARE (qa gate, fix 5): the walk returns each run's STATUS and
    the repair selects the matching variant — an unknown-status run repairs
    through the UNKNOWN filter variant, so a zero-row insert ALWAYS means a
    genuine PRESENT-key divergence, never the guaranteed-zero repair that a
    TERMINAL-only variant produced for unknown runs.

    Returns (repaired_key_row_count, divergent_run_count, first_divergent).
    The loop STOPS on the first divergent run — divining the full
    divergent-set size is impossible without re-scanning post-abort.
    """
    repaired = 0
    divergent = 0
    divergent_sample: uuid.UUID | None = None
    while True:
        rows = bind.execute(
            sa.text(_PARITY_MARKERS_MISMATCH_SQL), {"b1_cutoff": _B1_DEPLOY_CUTOFF, "batch": _PARITY_BATCH}
        ).all()
        if not rows:
            return (repaired, divergent, divergent_sample)
        for row in rows:
            run_id = uuid.UUID(str(row[0]))
            status = str(row[1])
            count = _run_sql(
                bind,
                _repair_body(_REPAIR_MARKERS_BODY_SQL, unknown_run=status == "unknown"),
                _repair_params(run_id),
            )
            if count == 0:
                # Nothing was inserted => the flagged legacy keys are
                # PRESENT keys with differing content — a real divergence,
                # never repairable by an INSERT-never-overwrites leg.
                divergent += 1
                if divergent_sample is None:
                    divergent_sample = run_id
        if divergent:
            return (repaired, divergent, divergent_sample)


def _drop_with_retry(bind: sa.Connection, sql: str, *, description: str) -> None:
    """One DDL drop — lock_timeout + bounded savepoint retry (0129 precedent).

    qa gate, fix 4: this helper is the SINGLE retry path for BOTH the 0193
    sweep-index DROP and the three runs-blob-column DROPs — an ACCESS
    EXCLUSIVE lock request with no timeout can wedge the whole upward chain
    behind it. The timeout is a module constant (never caller data — the
    SET LOCAL statement cannot take a bound parameter under every driver).
    A refused attempt rolls back ONLY its savepoint (re-arming the outer
    transaction's schema state) and retries; the FINAL failure propagates —
    release.sh's bounded migrator re-runs the whole chain.
    """
    lock_sql = "SET LOCAL lock_timeout = '" + _DROP_COL_LOCK_TIMEOUT + "'"
    for attempt in range(1, _DROP_COL_RETRIES + 1):
        try:
            with bind.begin_nested():
                bind.execute(sa.text(lock_sql))
                bind.execute(sa.text(sql))
            return
        except OperationalError:
            if attempt == _DROP_COL_RETRIES:
                raise
            _log.warning(
                "FAR-583 drop: %s refused (attempt %d/%d) — retrying",
                description,
                attempt,
                _DROP_COL_RETRIES,
            )
            time.sleep(_DROP_COL_RETRY_SLEEP_SECONDS)


def _upgrade_sqlite() -> None:
    """SQLite parity path: drop the sweep index + the three legacy columns.

    The quarantine table survives (see the module docstring for the
    retained-evidence rationale).
    """
    op.drop_index(_SWEEP_INDEX_NAME, table_name="runs")
    with op.batch_alter_table("runs") as batch:
        for col in _RUNS_OUTPUT_COLS:
            batch.drop_column(col)


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgres(bind):
        _upgrade_sqlite()
        return

    bind.execute(sa.text("SET search_path TO public"))

    # -- 1) Pre-flight drain assertion ---------------------------------------
    drain_count = _count_sql(bind, _DRAIN_COUNT_SQL, {"b1_cutoff": _B1_DEPLOY_CUTOFF})
    if drain_count > 0:
        raise RuntimeError(
            "FAR-583 drop pre-flight drain assertion FAILED: "
            f"{drain_count} run(s) created before the B1 deploy cutoff ({_B1_DEPLOY_CUTOFF.isoformat()}) are still "
            "non-terminal (running/claimed/awaiting_human/pending/hitl_parked). The drop must not proceed — every "
            "post-B1 write is new-table-only, so this is a drain-gate defect, never a transient."
        )
    _log.info("FAR-583 drop pre-flight drain gate: clean (0 pre-B1 in-flight run(s))")

    # jsonb invariant re-assertion before any leg.
    bind.execute(sa.text(_SHAPE_ASSERT_SQL))

    # Structural anomaly ABORT: junk-shaped legacy blobs in the NARROWED,
    # quarantine-excluded populations the loops actually iterate (LIMIT-1
    # sample + separate count query, twice: out/tel then markers).
    junk_out_tel_sample = bind.execute(sa.text(_JUNK_OUT_TEL_SAMPLE_SQL), {"b1_cutoff": _B1_DEPLOY_CUTOFF}).first()
    if junk_out_tel_sample is not None:
        junk_count = _count_sql(bind, _JUNK_OUT_TEL_POPULATION_SQL, {"b1_cutoff": _B1_DEPLOY_CUTOFF})
        raise RuntimeError(
            "FAR-583 drop structural anomaly: "
            f"{junk_count} pre-cutoff TERMINAL run(s) hold an outputs/telemetry legacy blob that is non-NULL but "
            f"NOT a jsonb object or a jsonb 'null' value (unparseable / unexpected shape; sample run_id="
            f"{junk_out_tel_sample[0]}). The 0192 dict semantics cannot re-map them losslessly — "
            "remediate by hand, then re-run."
        )
    junk_markers_sample = bind.execute(sa.text(_JUNK_MARKERS_SAMPLE_SQL), {"b1_cutoff": _B1_DEPLOY_CUTOFF}).first()
    if junk_markers_sample is not None:
        junk_count = _count_sql(bind, _JUNK_MARKERS_POPULATION_SQL, {"b1_cutoff": _B1_DEPLOY_CUTOFF})
        raise RuntimeError(
            "FAR-583 drop structural anomaly: "
            f"{junk_count} pre-cutoff terminal/unknown run(s) hold a raw_output_markers legacy blob that is "
            f"non-NULL but NOT a jsonb object or jsonb 'null' value (unparseable / unexpected shape; sample "
            f"run_id={junk_markers_sample[0]}). The 0192 dict semantics cannot re-map them losslessly — "
            "remediate by hand, then re-run."
        )

    # -- 2) INSERT-only repair -----------------------------------------------
    inserted = _repair_installation(bind)
    quarantined_remaining = _count_sql(bind, f"SELECT count(*) FROM {_QUARANTINE_TABLE}")  # noqa: S608  # nosec B608 - module constants only
    _log.info(
        "FAR-583 drop repair: %(final)d '__final__' row(s), %(meta)d metadata row(s), %(markers)d "
        "marker row(s) inserted; 0192-quarantined run(s) excluded but KEPT: %(quarantined)d",
        {**inserted, "quarantined": quarantined_remaining},
    )

    # -- 3) Content parity ----------------------------------------------------
    out_pop = bind.execute(sa.text(_PARITY_OUT_POPULATION_SQL), {"b1_cutoff": _B1_DEPLOY_CUTOFF}).one()
    markers_pop = bind.execute(sa.text(_PARITY_MARKERS_POPULATION_SQL), {"b1_cutoff": _B1_DEPLOY_CUTOFF}).one()
    _log.info(
        "FAR-583 drop parity populations: outputs/telemetry checked=%(out_checked)d "
        "post_cutoff=%(out_post)d; markers checked=%(mk_checked)d post_cutoff=%(mk_post)d",
        {
            "out_checked": int(out_pop.checked),
            "out_post": int(out_pop.post_cutoff),
            "mk_checked": int(markers_pop.checked),
            "mk_post": int(markers_pop.post_cutoff),
        },
    )

    overwritten = _parity_outputs_telemetry(bind)
    if overwritten:
        _log.info(
            "FAR-583 drop parity overwrite: %d pre-cutoff TERMINAL run(s) re-mapped LEGACY-AUTHORITATIVELY "
            "(terminal + pre-cutoff runs are immutable after the drain gate — safe, self-healing)",
            overwritten,
        )

    markers_repaired, markers_divergent, markers_divergent_sample = _parity_markers(bind)
    if markers_repaired:
        _log.info(
            "FAR-583 drop markers parity: %d missing legacy marker key row(s) repaired (never overwrites)",
            markers_repaired,
        )
    if markers_divergent:
        raise RuntimeError(
            "FAR-583 drop structural anomaly: "
            f"{markers_divergent} pre-cutoff run(s) hold a legacy markers dict that diverges from the "
            "reassembled new-table markers on a key BOTH stores claim (missing keys were repaired "
            f"already; a value conflict remains; sample run_id={markers_divergent_sample}) — aborting, "
            "remediate by hand"
        )

    # Post-parity completion summary.
    final_rows = _count_sql(bind, f"SELECT count(*) FROM {_TABLE} WHERE attempt_key = '{_FINAL_ATTEMPT_KEY}'")  # noqa: S608  # nosec B608 - module constants only
    marker_rows = _count_sql(bind, f"SELECT count(*) FROM {_TABLE} WHERE attempt_key <> '{_FINAL_ATTEMPT_KEY}'")  # noqa: S608  # nosec B608 - module constants only
    unknown_rows = _count_sql(bind, f"SELECT count(*) FROM {_TABLE} WHERE node_id = '{_UNKNOWN_NODE_ID}'")  # noqa: S608  # nosec B608 - module constants only
    _log.info(
        "FAR-583 drop completion: %(final)d '__final__' row(s); %(markers)d marker row(s); "
        "%(unknown)d '__unknown__' node row(s) (full original attempt keys preserved, FAR-188)",
        {"final": final_rows, "markers": marker_rows, "unknown": unknown_rows},
    )

    # -- 4) DROP ×4: the 0193 sweep index + the three columns -----------------
    # BOTH route through the bounded savepoint retry (qa gate, fix 4).
    _drop_with_retry(
        bind,
        f"DROP INDEX IF EXISTS {_SWEEP_INDEX_NAME}",  # nosec B608 - module constants only
        description="DROP of the 0193 sweep index " + _SWEEP_INDEX_NAME,
    )
    for col in _RUNS_OUTPUT_COLS:
        _drop_with_retry(
            bind,
            f"ALTER TABLE runs DROP COLUMN {col}",  # nosec B608 - module constants only
            description=f"DROP COLUMN runs.{col}",
        )
    _log.info(
        "FAR-583 drop: the 0193 sweep index + runs blob columns %s DROPPED (lock_timeout=%s, "
        "per-attempt savepoint retry on every DDL statement)",
        ", ".join(_RUNS_OUTPUT_COLS),
        _DROP_COL_LOCK_TIMEOUT,
    )


def downgrade() -> None:
    """NO-OP — NEVER rewound ("never rewind past this migration").

    The legacy ``runs`` blob columns are dropped by this migration; the
    surviving copies of pre-B1 runs' blobs are the ``run_node_outputs``
    rows (plus the 0192 ``run_node_outputs_quarantine`` evidence rows).
    Rewinding would destroy the drop's whole point. The
    DB-AHEAD-OF-IMAGE condition is owned by release.sh's bounded migrator
    (bare-FATAL after its 3 attempts — the explicit disposition, see the
    FAR-694 comment block in deploy/fly/release.sh) + the deploy workflow's
    build-SHA match, never by a chain rewind.

    Emergency re-add-columns snippet (restores EMPTY columns; re-folding
    the reassembled blobs back is the documented HAND-rollback path):

    .. code-block:: sql

        ALTER TABLE runs ADD COLUMN outputs_json jsonb;
        ALTER TABLE runs ADD COLUMN node_telemetry_json jsonb;
        ALTER TABLE runs ADD COLUMN raw_output_markers jsonb;
        -- Per run: fold the '__final__' rows back with the 0192 mapping
        -- semantics (UNION of both dicts, metadata empty-flags folded back
        -- to '{}', markers keyed by attempt_key). Quarantined runs restore
        -- from run_node_outputs_quarantine (Python-side, never raw SQL).

    Re-running THIS migration afterwards is idempotent (every leg is
    ON CONFLICT DO NOTHING) and re-verifies parity before returning.
    """
    raise RuntimeError(
        "FAR-583 drop: migration 0215 downgrade is a DOCUMENTED NO-OP "
        '("never rewind past this migration"); the emergency re-add-columns '
        "snippet lives in the docstring above"
    )
