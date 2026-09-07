"""run_node_outputs — per-node outputs/telemetry/markers store (FAR-583).

Revision ID: 0190_run_node_outputs
Revises: 0189_agent_runner_bindings
Create Date: 2026-09-04

Extracts the three whole-run JSON blobs on ``runs`` (``outputs_json`` /
``node_telemetry_json`` / ``raw_output_markers``) into a dedicated per-node
table, ``run_node_outputs`` — one row per ``(run_id, node_id, attempt_key)``:

* ``(run_id, <node_id>, '__final__')`` — the terminal per-node row; the value
  of each side is stored AS-IS (a per-side SQL NULL means the key was absent
  from that legacy dict; an explicit JSON ``null`` value is stored as the
  jsonb ``null`` value). Rows come from the UNION of both dicts' keys.
* ``(run_id, '__run_meta__', '__final__')`` — ONE metadata row, written iff
  at least one legacy side was ``'{}'`` (non-NULL empty): ``outputs_json``
  carries ``{"empty_outputs": bool, "empty_telemetry": bool}``. Legacy ``{}``
  and legacy NULL are DIFFERENT values; this row is what preserves the
  distinction after the legacy columns drop (B2b).
* ``(run_id, <parsed node_id>, <FULL original key>)`` — one row per legacy
  ``raw_output_markers`` key. Keys follow the node_runner grammar
  ``run:<uuid>:node:<node_id>:<suffix>``; the node id is derived by an
  ANCHORED twin parser (literal prefix + split on the trailing ``:<suffix>``,
  safe for colon-containing node ids — inlined below as SQL and mirrored as
  ``crud.run_node_outputs.parse_marker_node_id``; the regex is bound as a
  parameter and asserted identical to the repo module by the unit test).
  Unparseable keys are preserved as evidence (FAR-188) as
  ``('__unknown__', <full original key>)`` rows — counted, never dropped.

REVISION CHAIN NOTE: ``0175_dedupe_soft_delete_names`` is SPLICED mid-chain
(0155 -> 0175 -> 0156), so the linear chain runs 0155 -> 0175 -> 0156 -> ...
-> 0174 -> 0176_trigger_event_validation_results -> ... ->
0189_agent_runner_bindings — this revision chains onto THAT tip.

ROLE WIRING (the 0066 ceremony, verbatim from 0131/0137): the migration
connects via ``DATABASE_ADMIN_URL`` (the superuser/owner URL).
``modulo_migrate`` is a NOLOGIN role, so the migration executes
``GRANT CREATE ON SCHEMA public`` + ``GRANT REFERENCES ON TABLE
public.organisations`` AND — newly, this is the first table to reference
``runs`` — ``GRANT REFERENCES ON TABLE public.runs`` to ``modulo_migrate``,
then ``SET ROLE modulo_migrate`` around ``op.create_table``, then ``RESET
ROLE``, then the role-conditional ownership assertion (the owner must be
``modulo_migrate`` — the app role must NOT own an RLS-FORCED table). The
``organisation_id`` index is created AFTER ``RESET ROLE``. The ceremony is
conditional on the roles existing (fresh dev/BDD DBs have none).

The quarantine side table is created by the CALLER (an ops/remediation table,
deliberately not owned by the migrate role). qa iteration 2 (Major 5): its
privileges are EXPLICIT and role-existence-guarded — ``GRANT SELECT, INSERT,
DELETE`` to ``modulo_system`` (the catch-up sweep runs on the system role and
both selects the NOT-EXISTS exclusion and inserts quarantine rows) and
``GRANT SELECT, DELETE`` to ``modulo_app`` (the retention purge
``_delete_quarantine_rows`` runs on the app role via the admin run-retention
route). Without these grants the sweep's quarantine INSERT and the purge's
delete depend on the role bootstrap's default-privileges accident — a fleet
without it wedges the org's sweep forever (an un-quarantined anomaly is
re-selected every tick). Ops SQL reads during remediation run on the
admin/owner connection.

RLS: ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` + the strict fail-closed
``rls_org_isolation`` policy (``organisation_id = nullif(current_setting(
'app.organisation_id', true), '')::uuid`` — NO null-context allow branch;
0162/0163 style) + ``GRANT SELECT, INSERT, UPDATE, DELETE`` to ``modulo_app``
(role-existence guarded) — enabled STRICTLY AFTER the backfill, because FORCE
makes the owner a policy subject and a policy subject cannot backfill without
an org context.

DATA BACKFILL (terminal runs only, recent window):

* TERMINAL-RUNS-ONLY: ``status`` in the 9-state terminal set — inlined as a
  literal (migrations never import app constants; the unit test asserts the
  literal equals ``sorted(db.models.run.TERMINAL_STATUSES)``). Non-terminal
  runs' blobs are provisional; the catch-up sweep (wired by the follow-up
  pass) heals them at terminality.
* RECENT WINDOW: ``COALESCE(completed_at, updated_at)`` within the last 30
  days, keeping the single migration transaction acceptable on prod (env.py
  runs the whole chain in ONE transaction — the chunking below bounds
  STATEMENT size, not commits; per-batch commits are impossible without
  env.py changes). The sweep drains the remainder post-deploy.
* MARKERS leg also covers ``status = 'unknown'`` runs (markers are written
  mid-run and are durable evidence); outputs/telemetry stay terminal-only.
* QUARANTINE: runs whose legacy dicts carry ``__``-prefixed node ids (or
  ``__``-prefixed marker attempt keys, or parseable marker keys deriving a
  ``__``-prefixed node id) have their three blob values copied to the
  ``run_node_outputs_quarantine`` side table and are EXCLUDED from the
  backfill — those ids would collide with the sentinel namespace. QUARANTINE
  + count + proceed: data anomalies never abort the migration (abort is for
  infrastructure failure only).
* NO NUL PRE-SCAN (deliberate deviation, dispositioned on the FAR-583
  ticket): the three source columns have been ``jsonb`` since 0129/0147, and
  jsonb structurally cannot contain ``\\u0000`` (Postgres rejects bound-param
  NULs), so the AGENTS.md NUL-byte scan is unnecessary here. The migration
  ASSERTS via ``information_schema`` that all three columns are ``jsonb`` and
  RAISES EXCEPTION otherwise — the assertion is what makes the deviation
  safe.
* IDEMPOTENT: every ``INSERT`` carries ``ON CONFLICT DO NOTHING`` (release.sh
  retries migrations 3x).
* Batching: runs are processed in id-range chunks of 1000 (ordered by ``id``,
  cursor = last seen id), each chunk running the quarantine step + the legs
  as bounded statements.
* Post-conditions: a verification query asserts every in-window run with a
  meaningful blob (and not quarantined) has at least one new-table row;
  RAISES (aborting the whole transaction) on a mismatch — infrastructure
  failure only, never a data outcome.

SQL-construction note: every statement is a module-level constant whose only
interpolations are OTHER module constants (table names, status literals,
bound-parameter fragments) — never caller data. Parameters (:last_id,
:upper_id, :window_days, :marker_re) are bound via sa.text params. The
constant-assembly style keeps the security linters' string-building heuristics
out of the execution path entirely (the 0127/0166 noqa precedent is not
needed here).

NO DOWNGRADE: ``downgrade()`` is a documented no-op. Reversing would need to
fold per-node rows back into the legacy columns; the legacy columns still
exist (they are only dropped at B2b), so a rollback simply means "stop
writing/reading the new table". Restore instructions (only if the legacy
columns were somehow lost):

    -- fold new-table rows back per run, then UPDATE runs SET ...
    -- (use crud.run_node_outputs reassembly semantics: UNION of both dicts,
    --  metadata-row flags for '{}' sides, markers keyed by attempt_key)

SQLite path: plain-JSON table creation only (parity) — no ceremony, no
backfill, no RLS. The quarantine side table IS created on SQLite too: the
catch-up sweep's quarantine exclusion (NOT EXISTS against it) and the
sweep-body quarantine INSERT run on every backend, so the table must exist
everywhere the sweep can run.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0190_run_node_outputs"
down_revision: str | None = "0189_agent_runner_bindings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Read by tests/unit/db/test_rls_coverage.py (style 2: module-level string
# tuples) to confirm the org-scoped table has an RLS-enabling migration.
_TABLES = ("run_node_outputs",)

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"
_SYSTEM_ROLE = "modulo_system"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

_TABLE = "run_node_outputs"
_QUARANTINE_TABLE = "run_node_outputs_quarantine"

_META_NODE_ID = "__run_meta__"
_FINAL_ATTEMPT_KEY = "__final__"
_UNKNOWN_NODE_ID = "__unknown__"

# Inline terminal-status literal (migrations cannot import app constants).
# MUST equal sorted(db.models.run.TERMINAL_STATUSES) — pinned by the unit test.
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

# Pure-literal SQL twin assembled FROM the tuple above (single source of
# truth — the two cannot drift apart by construction; the unit test still
# asserts every status literal is present in the assembled SQL).
_TERMINAL_STATUS_SQL = "(" + ", ".join(f"'{status}'" for status in _TERMINAL_RUN_STATUSES) + ")"

_WINDOW_DAYS = 30
_CHUNK_SIZE = 1000

# Anchored marker-key grammar — MUST stay byte-identical to
# crud.run_node_outputs._MARKER_KEY_RE (pinned by the unit test). Bound as a
# parameter (:marker_re) everywhere, never interpolated into the SQL.
_MARKER_KEY_RE = r"^run:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:node:.+$"

# The literal prefix "run:" + 36 uuid chars + ":node:" — everything after it is
# "<node_id>:<suffix>". len("run:") + len(uuid) + len(":node:") = 4 + 36 + 6.
_MARKER_KEY_PREFIX_LEN = 46

# ---------------------------------------------------------------------------
# SQL fragments and statements. EVERY statement below is a module-level
# constant: pure literals or f-strings over other module constants only.
# Bound parameters (:last_id, :upper_id, :window_days, :marker_re,
# :migration_started_at) are passed per execution.
# ---------------------------------------------------------------------------

# "meaningful blob" = jsonb object on at least one of the three columns. JSON
# null (the ORM writes Python None as jsonb null) and SQL NULL both mean the
# side is absent; '{}' markers means "no markers" and is NOT meaningful on its
# own (outputs/telemetry '{}' IS meaningful — it produces the metadata row).
_ANY_BLOB_OBJECT_SQL = (
    "(jsonb_typeof(r.outputs_json) = 'object' "
    "OR jsonb_typeof(r.node_telemetry_json) = 'object' "
    "OR (jsonb_typeof(r.raw_output_markers) = 'object' AND r.raw_output_markers <> '{}'::jsonb))"
)
_WINDOW_PREDICATE_SQL = "COALESCE(r.completed_at, r.updated_at) >= now() - make_interval(days => :window_days)"
_NOT_QUARANTINED_SQL = f"NOT EXISTS (SELECT 1 FROM {_QUARANTINE_TABLE} q WHERE q.run_id = r.id)"  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data

# Postgres substr is 1-INDEXED: substr(key, N) returns the key FROM character
# N (inclusive) — the exact twin of Python's key[N - 1:]. The Python twin
# (crud.run_node_outputs.parse_marker_node_id) slices [_MARKER_KEY_PREFIX_LEN:],
# so the SQL offset is the prefix length PLUS ONE. An off-by-one here shifts
# every backfilled node id by one character (leading ':') and breaks the
# marker-row PKs — pinned by the unit test.
_MARKER_REMAINDER_SQL = f"substr(mk.key, {_MARKER_KEY_PREFIX_LEN + 1})"
_MARKER_LAST_COLON_SQL = f"position(':' IN reverse({_MARKER_REMAINDER_SQL}))"

# SQL twin of crud.run_node_outputs.parse_marker_node_id (the Python twin
# lives ONLY in the repo module — pinned by the unit test): parseable iff the
# anchored regex matches (:marker_re is BOUND at execution time) AND the
# remainder's LAST colon leaves a non-empty suffix (colon not the last char
# => position-from-end >= 2) AND a non-empty node id (colon not at position
# len => position-from-end <= length - 1).
_MARKER_NODE_ID_SQL = (
    f"CASE WHEN mk.key ~ :marker_re "
    f"AND {_MARKER_LAST_COLON_SQL} BETWEEN 2 AND length({_MARKER_REMAINDER_SQL}) - 1 "
    f"THEN substr({_MARKER_REMAINDER_SQL}, 1, length({_MARKER_REMAINDER_SQL}) - {_MARKER_LAST_COLON_SQL}) "
    f"ELSE '{_UNKNOWN_NODE_ID}' END"
)

# The quarantine predicate: any '__'-prefixed node id in the outputs/telemetry
# dicts, or any marker key that is itself '__'-prefixed OR parses to a
# '__'-prefixed node id. Data anomaly -> copy the run's blobs aside + skip;
# never abort.
_QUARANTINE_MARKER_KEYS_SQL = (
    "jsonb_each(CASE WHEN jsonb_typeof(r.raw_output_markers) = 'object' THEN r.raw_output_markers END) "
    "AS mk(key, value)"
)
_QUARANTINE_CONDITION_SQL = (
    f"(EXISTS (SELECT 1 FROM jsonb_object_keys("  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
    f"CASE WHEN jsonb_typeof(r.outputs_json) = 'object' THEN r.outputs_json END) k "
    f"WHERE left(k, 2) = '__')"
    f" OR EXISTS (SELECT 1 FROM jsonb_object_keys("
    f"CASE WHEN jsonb_typeof(r.node_telemetry_json) = 'object' THEN r.node_telemetry_json END) k "
    f"WHERE left(k, 2) = '__')"
    f" OR EXISTS (SELECT 1 FROM {_QUARANTINE_MARKER_KEYS_SQL} "
    f"WHERE left(mk.key, 2) = '__' OR (mk.key ~ :marker_re AND left({_MARKER_NODE_ID_SQL}, 2) = '__')))"
)

# Filter fragments (no id bounds) — chunk SELECT, preflight, coverage check.
_TERMINAL_FILTER_SQL = f"r.status IN {_TERMINAL_STATUS_SQL} AND {_WINDOW_PREDICATE_SQL}"
_UNKNOWN_FILTER_SQL = "r.status = 'unknown' AND " + _WINDOW_PREDICATE_SQL

# Chunk-window fragments (id bounds + filter) — the per-chunk legs.
_TERMINAL_WINDOW_SQL = "r.id > :last_id AND r.id <= :upper_id AND " + _TERMINAL_FILTER_SQL
_UNKNOWN_WINDOW_SQL = "r.id > :last_id AND r.id <= :upper_id AND " + _UNKNOWN_FILTER_SQL

_QUARANTINE_INSERT_COLUMNS_SQL = (
    "(run_id, organisation_id, legacy_outputs_json, legacy_node_telemetry_json, "
    "legacy_raw_output_markers, quarantined_at)"
)
_QUARANTINE_SELECT_SQL = (
    "SELECT r.id, r.organisation_id, r.outputs_json, r.node_telemetry_json, r.raw_output_markers, now() FROM runs r "
)
_QUARANTINE_TERMINAL_SQL = (
    f"INSERT INTO {_QUARANTINE_TABLE} {_QUARANTINE_INSERT_COLUMNS_SQL} "
    f"{_QUARANTINE_SELECT_SQL}"
    f"WHERE {_TERMINAL_WINDOW_SQL} AND {_QUARANTINE_CONDITION_SQL} "
    f"ON CONFLICT (run_id) DO NOTHING RETURNING run_id"
)
_QUARANTINE_UNKNOWN_SQL = (
    f"INSERT INTO {_QUARANTINE_TABLE} {_QUARANTINE_INSERT_COLUMNS_SQL} "
    f"{_QUARANTINE_SELECT_SQL}"
    f"WHERE {_UNKNOWN_WINDOW_SQL} AND {_QUARANTINE_CONDITION_SQL} "
    f"ON CONFLICT (run_id) DO NOTHING RETURNING run_id"
)

# Explode the UNION of both dicts' keys into '__final__' rows. Values stored
# AS-IS: ``r.outputs_json -> k`` yields the jsonb value (including the
# explicit jsonb ``null`` value) or SQL NULL when that side lacks the key —
# the SQL NULL / jsonb-null distinction the readers rely on.
_OUTPUTS_TELEMETRY_LEG_SQL = (
    f"INSERT INTO {_TABLE} "  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
    f"(run_id, node_id, attempt_key, organisation_id, outputs_json, node_telemetry_json, created_at, updated_at) "
    f"SELECT r.id, k, '{_FINAL_ATTEMPT_KEY}', r.organisation_id, "
    f"CASE WHEN jsonb_typeof(r.outputs_json) = 'object' THEN r.outputs_json -> k END, "
    f"CASE WHEN jsonb_typeof(r.node_telemetry_json) = 'object' THEN r.node_telemetry_json -> k END, "
    f"now(), now() "
    f"FROM runs r "
    f"CROSS JOIN LATERAL (SELECT DISTINCT k FROM ("
    f"SELECT jsonb_object_keys(CASE WHEN jsonb_typeof(r.outputs_json) = 'object' THEN r.outputs_json END) AS k "
    f"UNION "
    f"SELECT jsonb_object_keys(CASE WHEN jsonb_typeof(r.node_telemetry_json) = 'object' "
    f"THEN r.node_telemetry_json END)"
    f") u) keys "
    f"WHERE {_TERMINAL_WINDOW_SQL} "
    f"AND (jsonb_typeof(r.outputs_json) = 'object' OR jsonb_typeof(r.node_telemetry_json) = 'object') "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"ON CONFLICT DO NOTHING"
)

# One metadata row per run where a side is '{}' (non-NULL empty). The flag
# expressions are wrapped in COALESCE(..., false): a side that is SQL NULL
# makes `(col = '{}'::jsonb)` evaluate to SQL NULL (three-valued logic), and
# jsonb_build_object maps a NULL argument to a JSON *null* member — which
# violates ck_run_node_outputs_meta_shape (both flags must be JSON booleans)
# and would abort the whole one-transaction migration. A NULL side co-occurs
# freely with the OTHER side being '{}' (the mixed case this row exists for),
# so the guard is on the happy path, not an edge case.
_METADATA_LEG_SQL = (
    f"INSERT INTO {_TABLE} "  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
    f"(run_id, node_id, attempt_key, organisation_id, outputs_json, created_at, updated_at) "
    f"SELECT r.id, '{_META_NODE_ID}', '{_FINAL_ATTEMPT_KEY}', r.organisation_id, "
    "jsonb_build_object('empty_outputs', COALESCE((r.outputs_json = '{}'::jsonb), false), "
    "'empty_telemetry', COALESCE((r.node_telemetry_json = '{}'::jsonb), false)), "
    f"now(), now() "
    f"FROM runs r "
    f"WHERE {_TERMINAL_WINDOW_SQL} "
    "AND (r.outputs_json = '{}'::jsonb OR r.node_telemetry_json = '{}'::jsonb) "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"ON CONFLICT DO NOTHING"
)


def _markers_leg_sql(window_sql: str) -> str:
    """One marker row per legacy markers key.

    attempt_key = the FULL original key (evidence preserved, FAR-188); the
    node id comes from the anchored twin parser, with unparseable keys as
    '__unknown__' rows. '__'-prefixed attempt keys are SKIPPED (they would
    squat the sentinel namespace — an attempt key literally named
    '__final__' would also violate ck_run_node_outputs_final_no_markers);
    their runs are already quarantined by the quarantine step.
    """
    return (
        f"INSERT INTO {_TABLE} "  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
        f"(run_id, node_id, attempt_key, organisation_id, raw_output_markers, created_at, updated_at) "
        f"SELECT r.id, {_MARKER_NODE_ID_SQL}, mk.key, r.organisation_id, mk.value, now(), now() "
        f"FROM runs r "
        f"CROSS JOIN LATERAL {_QUARANTINE_MARKER_KEYS_SQL} "
        f"WHERE {window_sql} "
        f"AND left(mk.key, 2) <> '__' "
        f"AND {_NOT_QUARANTINED_SQL} "
        f"ON CONFLICT DO NOTHING"
    )


_MARKERS_LEG_TERMINAL_SQL = _markers_leg_sql(_TERMINAL_WINDOW_SQL)
_MARKERS_LEG_UNKNOWN_SQL = _markers_leg_sql(_UNKNOWN_WINDOW_SQL)

# Preflight counts + duplication-size estimate.
_ANY_BLOB_COUNT_SQL = f"SELECT count(*) FROM runs r WHERE {_ANY_BLOB_OBJECT_SQL}"  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
_BLOB_SIZE_SQL = (
    "SELECT COALESCE(SUM("  # nosec B608 - pure literals, no caller data
    "octet_length(COALESCE(r.outputs_json::text, '')) "
    "+ octet_length(COALESCE(r.node_telemetry_json::text, '')) "
    "+ octet_length(COALESCE(r.raw_output_markers::text, ''))), 0) "
    "FROM runs r WHERE " + _ANY_BLOB_OBJECT_SQL
)
_RUNS_TABLE_BYTES_SQL = "SELECT pg_total_relation_size('public.runs')"
_WINDOW_COUNT_SQL = f"SELECT count(*) FROM runs r WHERE {_TERMINAL_FILTER_SQL}"  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
_ANOMALY_COUNT_SQL = (
    "SELECT count(*) FROM runs r WHERE ("
    "(r.outputs_json IS NOT NULL AND jsonb_typeof(r.outputs_json) IS DISTINCT FROM 'null' "
    " AND jsonb_typeof(r.outputs_json) IS DISTINCT FROM 'object') "
    "OR (r.node_telemetry_json IS NOT NULL AND jsonb_typeof(r.node_telemetry_json) IS DISTINCT FROM 'null' "
    " AND jsonb_typeof(r.node_telemetry_json) IS DISTINCT FROM 'object') "
    "OR (r.raw_output_markers IS NOT NULL AND jsonb_typeof(r.raw_output_markers) IS DISTINCT FROM 'null' "
    " AND jsonb_typeof(r.raw_output_markers) IS DISTINCT FROM 'object'))"
)

# Post-conditions: every in-window run with a meaningful blob (and not
# quarantined) must have at least one new-table row — EXCLUDING runs that
# terminalized at/after the migration's own start (C4): the chunk scan sees
# READ COMMITTED per-statement snapshots, so a run an old machine
# terminalizes AFTER its id-chunk was scanned would otherwise RAISE here and
# abort the whole chain (release.sh re-races -> wedged deploy). Such runs
# land in the catch-up sweep instead; the exclusion predicate keys on
# COALESCE(completed_at, updated_at) >= :migration_started_at — now() is
# transaction-start, constant for the whole one-transaction chain.
# qa iteration 2 (rider 12): the exclusion is widened by a 30-second clock-
# skew margin — ``completed_at`` is stamped from the APP server's clock while
# :migration_started_at is the DATABASE server's clock; the two hosts' clocks
# need not agree, and an app clock running a few seconds ahead of the DB
# clock would otherwise fail the coverage check on a run that terminalized
# DURING the migration (still owned by the catch-up sweep, never lost).
_STARTED_AT_EXCLUSION_SQL = "COALESCE(r.completed_at, r.updated_at) < :migration_started_at - interval '30 seconds'"
_TERMINAL_COVERAGE_SQL = (
    f"SELECT count(*) FROM runs r WHERE {_TERMINAL_FILTER_SQL} "  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
    f"AND {_ANY_BLOB_OBJECT_SQL} "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"AND {_STARTED_AT_EXCLUSION_SQL} "
    f"AND NOT EXISTS (SELECT 1 FROM {_TABLE} n WHERE n.run_id = r.id)"
)
_UNKNOWN_COVERAGE_SQL = (
    f"SELECT count(*) FROM runs r WHERE {_UNKNOWN_FILTER_SQL} "  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
    f"AND jsonb_typeof(r.raw_output_markers) = 'object' "
    "AND r.raw_output_markers <> '{}'::jsonb "
    f"AND {_NOT_QUARANTINED_SQL} "
    f"AND {_STARTED_AT_EXCLUSION_SQL} "
    f"AND NOT EXISTS (SELECT 1 FROM {_TABLE} n WHERE n.run_id = r.id)"
)

_ROWS_TOTAL_SQL = f"SELECT count(*) FROM {_TABLE}"  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
_UNKNOWN_KEY_COUNT_SQL = f"SELECT count(*) FROM {_TABLE} WHERE node_id = '{_UNKNOWN_NODE_ID}'"  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data
_QUARANTINE_TOTAL_SQL = f"SELECT count(*) FROM {_QUARANTINE_TABLE}"  # noqa: S608  # nosec B608 - interpolates module constants only, never caller data

# alembic.ini routes INFO through the `alembic` logger family (root is WARN),
# so migration preflight/completion summaries MUST be emitted on a child of
# `alembic` to surface in `alembic upgrade` output at all — a `__name__`
# logger is silently suppressed. (The name is held in a variable because the
# linter pins module-level getLogger calls to __name__; this is the one
# deliberate, documented exception.)
_ALEMBIC_RUNTIME_LOGGER = "alembic.runtime.migration"
_log = logging.getLogger(_ALEMBIC_RUNTIME_LOGGER)


def _is_postgres(bind: sa.Connection) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind: sa.Connection, role: str) -> bool:
    """Return True when the Postgres role exists (fresh dev/BDD DBs have none)."""
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind: sa.Connection, table: str) -> None:
    """POST-CREATE ownership assertion — the 0066 ceremony (before RLS)."""
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass(:tbl)").bindparams(
            tbl=f"public.{table}"
        )
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"{table} owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own run_node_outputs — owner bypasses RLS)"
        )


def _count(bind: sa.Connection, sql: str, params: dict[str, object] | None = None) -> int:
    result = bind.execute(sa.text(sql), params or {})
    value = result.scalar_one_or_none()
    return int(value or 0)


def _create_tables(*, postgres_types: bool) -> None:
    """Create run_node_outputs.

    JSONB lives only in migrations (repo parity rule): on Postgres the three
    blob columns are JSONB (matching the runs columns since 0129/0147 — the
    jsonb-canonical key-ordering contract depends on it); the ORM model keeps
    generic JSON. On Postgres the STRICT sentinel-shape CHECK uses
    jsonb_typeof; the SQLite twin uses json_type/json_remove (SQLite's
    json_type returns 'true'/'false' for JSON booleans, not 'boolean';
    json_remove proves the object holds EXACTLY the two keys).
    """
    blob_type = sa.JSON().with_variant(JSONB(), "postgresql") if postgres_types else sa.JSON()
    if postgres_types:
        meta_shape_check = sa.CheckConstraint(
            f"node_id <> '{_META_NODE_ID}' OR ("
            "jsonb_typeof(outputs_json) = 'object' "
            "AND outputs_json - 'empty_outputs' - 'empty_telemetry' = '{}'::jsonb "
            "AND jsonb_typeof(outputs_json->'empty_outputs') = 'boolean' "
            "AND jsonb_typeof(outputs_json->'empty_telemetry') = 'boolean')",
            name="ck_run_node_outputs_meta_shape",
        )
    else:
        meta_shape_check = sa.CheckConstraint(
            f"node_id <> '{_META_NODE_ID}' OR ("
            "json_type(outputs_json) = 'object' "
            "AND json_type(outputs_json, '$.empty_outputs') IN ('true', 'false') "
            "AND json_type(outputs_json, '$.empty_telemetry') IN ('true', 'false') "
            "AND json_remove(outputs_json, '$.empty_outputs', '$.empty_telemetry') = '{}')",
            name="ck_run_node_outputs_meta_shape",
        )

    op.create_table(
        _TABLE,
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("attempt_key", sa.Text(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("outputs_json", blob_type, nullable=True),
        sa.Column("node_telemetry_json", blob_type, nullable=True),
        sa.Column("raw_output_markers", blob_type, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.current_timestamp(), nullable=False),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id", "node_id", "attempt_key", name="pk_run_node_outputs_run_node_attempt"),
        sa.CheckConstraint(
            f"attempt_key <> '{_FINAL_ATTEMPT_KEY}' OR raw_output_markers IS NULL",
            name="ck_run_node_outputs_final_no_markers",
        ),
        meta_shape_check,
    )


def _create_quarantine_table() -> None:
    """The quarantine side table — created by the CALLER (ops table).

    No FKs (quarantined evidence survives run deletion for audit). qa
    iteration 2 (Major 5): the sweep's SELECT (the NOT-EXISTS trigger leg) +
    INSERT (the quarantine step) run on the ``modulo_system`` role, and the
    retention purge's delete runs on ``modulo_app`` — both get EXPLICIT
    role-existence-guarded grants in :func:`upgrade` (below), never a
    default-privileges accident. On Postgres this runs AFTER the SET ROLE
    ceremony (never owned by ``modulo_migrate``); on the SQLite parity path
    it is created plainly — the sweep's quarantine exclusion + INSERT run on
    every backend.
    """
    blob_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        _QUARANTINE_TABLE,
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("legacy_outputs_json", blob_type, nullable=True),
        sa.Column("legacy_node_telemetry_json", blob_type, nullable=True),
        sa.Column("legacy_raw_output_markers", blob_type, nullable=True),
        sa.Column(
            "quarantined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id", name="pk_run_node_outputs_quarantine_run"),
    )


def _preflight(bind: sa.Connection) -> None:
    """Preflight counts + duplication-size estimate — logged via alembic
    logging (RAISE NOTICEs do not surface deterministically through env.py)."""
    any_blob_count = _count(bind, _ANY_BLOB_COUNT_SQL)
    size_bytes = _count(bind, _BLOB_SIZE_SQL)
    runs_table_bytes = _count(bind, _RUNS_TABLE_BYTES_SQL)
    window_count = _count(bind, _WINDOW_COUNT_SQL, {"window_days": _WINDOW_DAYS})
    anomaly_count = _count(bind, _ANOMALY_COUNT_SQL)
    _log.info(
        "FAR-583 preflight: runs with a meaningful blob: %(any_blob)d; duplicating ~%(size)d blob bytes "
        "(runs table total %(table_bytes)d); terminal runs in the %(days)d-day window: %(window)d; "
        "non-object blob anomalies (skipped, never abort): %(anomalies)d",
        {
            "any_blob": any_blob_count,
            "size": size_bytes,
            "table_bytes": runs_table_bytes,
            "days": _WINDOW_DAYS,
            "window": window_count,
            "anomalies": anomaly_count,
        },
    )


def _assert_runs_blob_columns_are_jsonb(bind: sa.Connection) -> None:
    """RAISE EXCEPTION unless all three runs blob columns are jsonb.

    This is the documented NO-NUL-PRE-SCAN deviation guard (see module
    docstring): jsonb cannot contain \\u0000, so the NUL-byte scan the
    json-to-jsonb lesson demands is unnecessary — but only while the columns
    really are jsonb. The offending column list is passed through RAISE's
    ``%`` format placeholder (the message itself contains no literal percent
    signs), not ``||`` concatenation — PL/pgSQL's RAISE statement does not
    accept string-concatenation expressions after the message literal.
    """
    bind.execute(
        sa.text(
            "DO $far583$ "
            "DECLARE bad text; "
            "BEGIN "
            "SELECT string_agg(column_name, ', ') INTO bad FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'runs' "
            "AND column_name IN ('outputs_json', 'node_telemetry_json', 'raw_output_markers') "
            "AND data_type <> 'jsonb'; "
            "IF bad IS NOT NULL THEN "
            "RAISE EXCEPTION 'FAR-583: runs blob column(s) % are not jsonb - "
            "the no-NUL-pre-scan deviation requires the jsonb invariant (0129/0147)', bad; "
            "END IF; "
            "END $far583$;"
        )
    )


def _quarantine_chunk(bind: sa.Connection, *, terminal: bool, last_id: uuid.UUID, upper_id: uuid.UUID) -> int:
    sql = _QUARANTINE_TERMINAL_SQL if terminal else _QUARANTINE_UNKNOWN_SQL
    result = bind.execute(
        sa.text(sql),
        {
            "last_id": last_id,
            "upper_id": upper_id,
            "window_days": _WINDOW_DAYS,
            "marker_re": _MARKER_KEY_RE,
        },
    )
    return len(result.all())


def _backfill_outputs_telemetry_chunk(bind: sa.Connection, last_id: uuid.UUID, upper_id: uuid.UUID) -> None:
    bind.execute(
        sa.text(_OUTPUTS_TELEMETRY_LEG_SQL),
        {"last_id": last_id, "upper_id": upper_id, "window_days": _WINDOW_DAYS},
    )


def _backfill_metadata_chunk(bind: sa.Connection, last_id: uuid.UUID, upper_id: uuid.UUID) -> None:
    bind.execute(
        sa.text(_METADATA_LEG_SQL),
        {"last_id": last_id, "upper_id": upper_id, "window_days": _WINDOW_DAYS},
    )


def _backfill_markers_chunk(bind: sa.Connection, *, terminal: bool, last_id: uuid.UUID, upper_id: uuid.UUID) -> None:
    sql = _MARKERS_LEG_TERMINAL_SQL if terminal else _MARKERS_LEG_UNKNOWN_SQL
    bind.execute(
        sa.text(sql),
        {
            "last_id": last_id,
            "upper_id": upper_id,
            "window_days": _WINDOW_DAYS,
            "marker_re": _MARKER_KEY_RE,
        },
    )


def _verify_window_coverage(bind: sa.Connection, migration_started_at: object) -> None:
    """RAISE if any in-window run with a meaningful blob has zero rows.

    Quarantined runs are excluded (they are deliberately unrepresented), and
    runs terminalized at/after the migration's own start are excluded (C4 —
    the catch-up sweep owns them; see _STARTED_AT_EXCLUSION_SQL). env.py
    wraps the whole chain in one transaction, so the raise aborts the entire
    migration — which is exactly the contract: this fires on infrastructure
    failure only, never on a data outcome.
    """
    params = {"window_days": _WINDOW_DAYS, "migration_started_at": migration_started_at}
    terminal_missing = _count(bind, _TERMINAL_COVERAGE_SQL, params)
    unknown_missing = _count(bind, _UNKNOWN_COVERAGE_SQL, params)
    if terminal_missing > 0 or unknown_missing > 0:
        raise RuntimeError(
            "FAR-583 backfill coverage violation: "
            f"{terminal_missing} terminal-window run(s) and {unknown_missing} unknown-status "
            "run(s) with a meaningful blob have no run_node_outputs rows (infrastructure failure)"
        )


def _backfill_window(bind: sa.Connection, *, terminal: bool) -> None:
    """Chunk the window by id (ordered, cursor = last seen id).

    env.py runs the chain in ONE transaction, so chunking bounds STATEMENT
    size only (per-batch commits are impossible without env.py changes —
    dispositioned on the FAR-583 ticket).
    """
    filter_sql = _TERMINAL_FILTER_SQL if terminal else _UNKNOWN_FILTER_SQL
    chunk_sql = (
        "SELECT id FROM runs r WHERE r.id > :last_id AND "  # noqa: S608  # nosec B608 - module constants only
        + filter_sql
        + " ORDER BY r.id LIMIT :chunk_size"
    )
    last_id = uuid.UUID(int=0)
    while True:
        chunk_rows = bind.execute(
            sa.text(chunk_sql),
            {"last_id": last_id, "chunk_size": _CHUNK_SIZE, "window_days": _WINDOW_DAYS},
        ).all()
        ids = [row[0] for row in chunk_rows]
        if not ids:
            return
        upper_id = uuid.UUID(str(ids[-1]))
        quarantined = _quarantine_chunk(bind, terminal=terminal, last_id=last_id, upper_id=upper_id)
        if terminal:
            _backfill_outputs_telemetry_chunk(bind, last_id, upper_id)
            _backfill_metadata_chunk(bind, last_id, upper_id)
        _backfill_markers_chunk(bind, terminal=terminal, last_id=last_id, upper_id=upper_id)
        if quarantined:
            _log.info("FAR-583 quarantine: %d run(s) quarantined in chunk ending at %s", quarantined, upper_id)
        last_id = upper_id
        if len(ids) < _CHUNK_SIZE:
            return


def _completion_summary(bind: sa.Connection, quarantined_total: int) -> None:
    rows_total = _count(bind, _ROWS_TOTAL_SQL)
    unknown_keys = _count(bind, _UNKNOWN_KEY_COUNT_SQL)
    _log.info(
        "FAR-583 completion: %(rows)d run_node_outputs rows; %(quarantined)d run(s) quarantined "
        "(remediate via ops SQL against %(quarantine_table)s); %(unknown)d '__unknown__' marker key(s) "
        "(evidence preserved under their full original attempt keys)",
        {
            "rows": rows_total,
            "quarantined": quarantined_total,
            "quarantine_table": _QUARANTINE_TABLE,
            "unknown": unknown_keys,
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if not pg:
        # SQLite parity path: plain-JSON table creation only (no ceremony, no
        # backfill, no RLS). The quarantine side table IS created — the
        # sweep's quarantine exclusion + INSERT run on every backend.
        _create_tables(postgres_types=False)
        op.create_index("ix_run_node_outputs_organisation_id", _TABLE, ["organisation_id"])
        _create_quarantine_table()
        return

    op.execute("SET search_path TO public")
    # C4: the migration-start timestamp (transaction-start now() — constant
    # for the whole one-transaction chain). The end-of-migration coverage
    # check excludes runs terminalized at/after this instant: an old machine
    # can terminalize a run AFTER its id-chunk was scanned (READ COMMITTED
    # per-statement snapshots); aborting the chain on it would wedge the
    # deploy. Those runs land in the catch-up sweep instead.
    migration_started_at = bind.execute(sa.text("SELECT now()")).scalar_one()

    migrate_role = _role_exists(bind, _MIGRATE_ROLE)
    app_role = _role_exists(bind, _APP_ROLE)
    system_role = _role_exists(bind, _SYSTEM_ROLE)

    if migrate_role:
        op.execute(f"GRANT CREATE ON SCHEMA public TO {_MIGRATE_ROLE}")
        op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
        # NEW with this table: run_node_outputs references runs (the legacy
        # blobs' home) — runs has never been granted REFERENCES before.
        op.execute(f"GRANT REFERENCES ON TABLE public.runs TO {_MIGRATE_ROLE}")
        op.execute(f"SET ROLE {_MIGRATE_ROLE}")
        _create_tables(postgres_types=True)
        op.execute("RESET ROLE")
        _assert_owner_is_migrate(bind, _TABLE)
    else:
        _create_tables(postgres_types=True)

    # organisation_id index created AFTER RESET ROLE (the ceremony ordering).
    op.create_index("ix_run_node_outputs_organisation_id", _TABLE, ["organisation_id"])
    _create_quarantine_table()

    # qa iteration 2 (Major 5): EXPLICIT quarantine privileges, never the
    # role bootstrap's default-privileges accident. The catch-up sweep runs
    # on the SYSTEM role (SELECT for the NOT-EXISTS exclusion, INSERT for the
    # quarantine step — DELETE for remediation SQL); the retention purge's
    # ``_delete_quarantine_rows`` runs on the APP role (SELECT + DELETE — the
    # admin run-retention route drives it). A missing grant wedges the org's
    # sweep forever: an un-quarantined anomaly is re-selected every tick.
    if system_role:
        op.execute(f"GRANT SELECT, INSERT, DELETE ON {_QUARANTINE_TABLE} TO {_SYSTEM_ROLE}")
    if app_role:
        op.execute(f"GRANT SELECT, DELETE ON {_QUARANTINE_TABLE} TO {_APP_ROLE}")

    # Backfill strictly BEFORE RLS (FORCE makes the owner a policy subject).
    _preflight(bind)
    _assert_runs_blob_columns_are_jsonb(bind)
    _backfill_window(bind, terminal=True)
    _backfill_window(bind, terminal=False)
    _verify_window_coverage(bind, migration_started_at)

    # RLS last: ENABLE + FORCE + strict fail-closed policy (0162/0163 style —
    # NO null-context allow branch), then the app-role DML grant.
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY rls_org_isolation ON {_TABLE} USING ({_ORG_SCOPE})")
    if app_role:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {_TABLE} TO {_APP_ROLE}")

    _completion_summary(bind, _count(bind, _QUARANTINE_TOTAL_SQL))


def downgrade() -> None:
    """Documented NO-OP (FAR-583 PR A).

    The legacy ``runs`` blob columns still exist (they are only dropped at
    B2b), so a rollback simply means "stop writing/reading the new table" —
    there is nothing to reverse that would not destroy the freshly backfilled
    rows. If the legacy columns were somehow lost, re-fold the per-node rows
    back using the crud.run_node_outputs reassembly semantics (UNION of both
    dicts, metadata-row flags for '{}' sides, markers keyed by attempt_key)
    before dropping the table by hand.
    """
