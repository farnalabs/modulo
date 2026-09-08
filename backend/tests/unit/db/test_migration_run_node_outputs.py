"""Structural unit tests for migration 0192_run_node_outputs (FAR-583).

These run WITHOUT a database. They pin the migration's data contract: the
inlined terminal-status literal (migrations cannot import app constants) AND
its assembled SQL twin (built FROM the tuple — single source of truth), the
anchored marker-key twin parser (the Python twin lives ONLY in
``crud.run_node_outputs`` — the regex and prefix-length constants are pinned
identical, and the migration's substr offset is pinned to prefix_len + 1 to
match Postgres' 1-indexed substr), the metadata flags COALESCE guard
(qa C1: an SQL-NULL side must not produce a JSON-null flag member), the
coverage-check started-at exclusion (qa C4), the quarantine behaviour markers
in the SQL, the jsonb invariant assertion (the documented NO-NUL-PRE-SCAN
deviation guard), the 0066/0131 ownership-ceremony grants (including the
FIRST-ever ``GRANT REFERENCES ON public.runs``), and the 30-day recent-window
+ idempotency SQL shape. The live-Postgres behaviour (ceremony, backfill
round-trip incl. the substr/parser parity, quarantine, metadata flags) is
covered by the testcontainers integration suite.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from modulo.db.crud.run_node_outputs import (
    _MARKER_KEY_PREFIX_LEN as REPO_MARKER_KEY_PREFIX_LEN,
)
from modulo.db.crud.run_node_outputs import (
    _MARKER_KEY_RE as REPO_MARKER_KEY_RE,
)
from modulo.db.crud.run_node_outputs import parse_marker_node_id
from modulo.db.models.run import TERMINAL_STATUSES
from modulo.db.models.run_node_outputs import UNKNOWN_NODE_ID

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0192_run_node_outputs"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """Return the migration's executable code, minus the module docstring.

    The docstring legitimately quotes the SQL forms under test (explaining
    them), so assertions must not match that historical prose.
    """
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def test_metadata_unchanged() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    # 0175_dedupe_soft_delete_names is SPLICED mid-chain (0155 -> 0175 -> 0156),
    # so the linear chain runs ... -> 0174 -> main's 0176..0189 -> 0190/0191 —
    # this revision chains onto the 0191_bundled_runner_seed_backfill tip.
    assert module.down_revision == "0191_bundled_runner_seed_backfill"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_and_downgrade_are_callable() -> None:
    module = _load_migration()
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_terminal_literal_equals_sorted_app_constant() -> None:
    """The inlined 9-state terminal list MUST equal sorted(TERMINAL_STATUSES)."""
    module = _load_migration()
    migration_literal = module._TERMINAL_RUN_STATUSES
    assert migration_literal == tuple(sorted(TERMINAL_STATUSES))


def test_terminal_status_sql_is_built_from_the_status_tuple() -> None:
    """qa M8: the SQL twin is ASSEMBLED from the status tuple (single source
    of truth) — every literal present, and byte-equal to the assembly."""
    module = _load_migration()
    expected = "(" + ", ".join(f"'{status}'" for status in module._TERMINAL_RUN_STATUSES) + ")"
    assert expected == module._TERMINAL_STATUS_SQL
    for status in module._TERMINAL_RUN_STATUSES:
        assert f"'{status}'" in module._TERMINAL_STATUS_SQL


def test_marker_regex_and_prefix_len_are_identical_to_repo_module() -> None:
    """The SQL-side regex is bound as a parameter from this constant; it must
    stay byte-identical to crud.run_node_outputs._MARKER_KEY_RE, and the
    prefix length must match the repo twin's (the substr offset derives from
    it) or the twin parsers diverge silently."""
    module = _load_migration()
    assert module._MARKER_KEY_RE == REPO_MARKER_KEY_RE
    assert module._MARKER_KEY_PREFIX_LEN == REPO_MARKER_KEY_PREFIX_LEN
    assert module._MARKER_KEY_PREFIX_LEN == 46


def test_marker_substr_offset_is_prefix_len_plus_one() -> None:
    """qa C2: Postgres substr is 1-indexed (substr(key, N) == key[N-1:]) while
    the Python twin slices key[46:] — the SQL offset MUST be prefix_len + 1.
    The old constant passed 46, shifting every backfilled node id by one
    character (leading ':') and breaking the marker-row PKs."""
    module = _load_migration()
    assert "substr(mk.key, 46)" not in module._MARKER_REMAINDER_SQL
    assert f"substr(mk.key, {module._MARKER_KEY_PREFIX_LEN + 1})" in module._MARKER_REMAINDER_SQL


def _assert_parses(attempt_key: str, expected_node_id: str) -> None:
    """The live Python twin (repo module) — the migration's SQL twin is
    round-tripped against it by the testcontainers integration tests."""
    assert parse_marker_node_id(attempt_key) == expected_node_id


def test_twin_parser_round_trips_plain_and_colon_node_ids() -> None:
    run_id = "01234567-89ab-cdef-0123-456789abcdef"
    _assert_parses(f"run:{run_id}:node:node1:3", "node1")
    # Colon-containing node ids are safe: the split is on the LAST colon.
    _assert_parses(f"run:{run_id}:node:my:weird:node:7", "my:weird:node")
    _assert_parses(f"run:{run_id}:node:n:{run_id}", "n")
    _assert_parses(f"run:{run_id}:node:a:b:claim-unknown", "a:b")
    _assert_parses(f"run:{run_id}:node:x:0123abcd", "x")


def test_twin_parser_maps_unparseable_keys_to_unknown_sentinel() -> None:
    run_id = "01234567-89ab-cdef-0123-456789abcdef"
    _assert_parses("junk", UNKNOWN_NODE_ID)
    _assert_parses("run:not-a-uuid:node:x:1", UNKNOWN_NODE_ID)
    _assert_parses(f"run:{run_id}:node:nosuffix", UNKNOWN_NODE_ID)
    _assert_parses(f"run:{run_id}:node:", UNKNOWN_NODE_ID)
    _assert_parses(f"run:{run_id}:node::suffix", UNKNOWN_NODE_ID)
    _assert_parses(f"run:{run_id}:node:x:", UNKNOWN_NODE_ID)


def test_twin_parser_keeps_sentinel_named_node_ids_parseable() -> None:
    """Sentinel-NAMED ids still parse (they are valid grammar); the
    quarantine step — not the parser — is what keeps those runs out."""
    run_id = "01234567-89ab-cdef-0123-456789abcdef"
    _assert_parses(f"run:{run_id}:node:__run_meta__:1", "__run_meta__")
    _assert_parses(f"run:{run_id}:node:__weird__:fallback", "__weird__")


def test_migration_has_no_dead_python_twin_parser() -> None:
    """qa C2: the migration's local Python twin is DELETED — the single live
    parser is crud.run_node_outputs.parse_marker_node_id (the stub existed
    only so old tests passed, never executed by the migration)."""
    module = _load_migration()
    assert not hasattr(module, "_parse_marker_node_id")


def test_metadata_flags_are_coalesced_against_sql_null_sides() -> None:
    """qa C1: a SQL-NULL side makes `(col = '{}'::jsonb)` evaluate to SQL
    NULL; jsonb_build_object maps NULL to a JSON null member, which violates
    ck_run_node_outputs_meta_shape and would abort the whole one-transaction
    migration. Both flag expressions MUST be COALESCE'd to false."""
    module = _load_migration()
    sql = module._METADATA_LEG_SQL
    assert "COALESCE((r.outputs_json = '{}'::jsonb), false)" in sql
    assert "COALESCE((r.node_telemetry_json = '{}'::jsonb), false)" in sql
    # No bare (un-coalesced) flag expression remains.
    assert "jsonb_build_object('empty_outputs', (r.outputs_json" not in sql
    assert "'empty_telemetry', (r.node_telemetry_json" not in sql


def test_coverage_check_excludes_runs_terminalized_after_migration_start() -> None:
    """qa C4 + iteration 2 rider 12: the end-of-migration coverage check must
    exclude runs whose COALESCE(completed_at, updated_at) is within 30s of
    the migration-start timestamp — an old machine terminalizing a run after
    its id-chunk was scanned must not abort the chain (the catch-up sweep
    owns it), and the 30-second margin absorbs app-clock vs DB-clock skew
    (completed_at is stamped from the APP server's clock, the exclusion
    compares against the DATABASE server's clock)."""
    module = _load_migration()
    exclusion = "COALESCE(r.completed_at, r.updated_at) < :migration_started_at - interval '30 seconds'"
    assert exclusion in module._TERMINAL_COVERAGE_SQL
    assert exclusion in module._UNKNOWN_COVERAGE_SQL


def test_tables_constant_exposes_rls_table_to_coverage_scanner() -> None:
    """test_rls_coverage.py style-2 collection reads module-level string
    tuples — run_node_outputs must be listed."""
    module = _load_migration()
    assert "run_node_outputs" in module._TABLES


def test_ownership_ceremony_grants_are_present() -> None:
    """The 0066/0131 ceremony verbatim — CREATE on schema, REFERENCES on the
    FK targets, and run_node_outputs is the FIRST table to reference runs."""
    code = _source_code()
    assert "GRANT CREATE ON SCHEMA public TO" in code
    assert "GRANT REFERENCES ON TABLE public.organisations TO" in code
    assert "GRANT REFERENCES ON TABLE public.runs TO" in code
    assert "SET ROLE" in code
    assert "RESET ROLE" in code
    assert "_assert_owner_is_migrate(bind, _TABLE)" in code
    # The organisation_id index is created AFTER the ceremony, outside SET
    # ROLE — scoped to the upgrade() body (the SQLite early-return path also
    # creates an index, before any ceremony).
    upgrade_src = code[code.index("def upgrade()") :]
    after_ceremony = upgrade_src[upgrade_src.index("RESET ROLE") :]
    assert 'op.create_index("ix_run_node_outputs_organisation_id"' in after_ceremony


def test_jsonb_invariant_assertion_is_present() -> None:
    """The NO-NUL-PRE-SCAN deviation guard: information_schema must report
    jsonb for all three runs blob columns or the migration RAISES EXCEPTION."""
    code = _source_code()
    assert "information_schema.columns" in code
    assert "data_type <> 'jsonb'" in code
    assert "RAISE EXCEPTION" in code
    assert "'outputs_json', 'node_telemetry_json', 'raw_output_markers'" in code


def test_rls_is_enabled_forced_and_fail_closed() -> None:
    module = _load_migration()
    code = _source_code()
    assert "ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY" in code
    assert "ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY" in code
    # Strict fail-closed policy — NO null-context allow branch.
    assert module._ORG_SCOPE == ("organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid")
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON" in code
    # RLS must come after the backfill (FORCE makes the owner a policy subject).
    assert code.index("ENABLE ROW LEVEL SECURITY") > code.index("_backfill_window(bind, terminal=True)")


def test_quarantine_privileges_are_explicit_and_role_guarded() -> None:
    """qa iteration 2 (Major 5): the quarantine table's privileges are
    EXPLICIT, role-existence-guarded — the sweep's SELECT/INSERT/DELETE on
    the system role and the retention purge's SELECT/DELETE on the app role.
    Without them both legs depend on the role bootstrap's default-privileges
    accident, and a fleet without it wedges the org's sweep forever."""
    module = _load_migration()
    code = _source_code()
    assert module._SYSTEM_ROLE == "modulo_system"
    assert "GRANT SELECT, INSERT, DELETE ON {_QUARANTINE_TABLE} TO {_SYSTEM_ROLE}" in code
    assert "GRANT SELECT, DELETE ON {_QUARANTINE_TABLE} TO {_APP_ROLE}" in code
    # Both grants are role-existence-guarded (fresh dev/BDD DBs have none).
    assert "if system_role:" in code
    assert "if app_role:" in code
    assert "_role_exists(bind, _SYSTEM_ROLE)" in code
    # No stale "no app-role grant" posture claims anywhere in the source.
    assert "no app-role grant" not in code


def test_quarantine_docstrings_do_not_claim_default_revoke_posture() -> None:
    """qa iteration 2 (Major 5): the helper docstrings must describe the
    EXPLICIT grants (the retention purge runs on modulo_app, the sweep on
    modulo_system) — the old 'no app-role grant / default REVOKE' claims
    drifted from the shipped ceremony."""
    retention_src = (
        Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "crud" / "run_retention.py"
    ).read_text(encoding="utf-8")
    assert "no app-role grant on Postgres (default" not in retention_src
    assert "migration 0192 grants" in retention_src, "the docstring names the explicit grants"


def test_sweep_index_migration_0193_chains_and_pins() -> None:
    """qa iteration 2 (Major 7): migration 0193 creates the sweep's partial
    index — revision chain (0192 -> 0193), the twin terminal literal, the
    assembled predicate, and the 0171-precedent deploy-safety shape (plain
    blocking ``CREATE INDEX IF NOT EXISTS`` — env.py's single externally-
    managed chain transaction makes CONCURRENTLY and the autocommit_block
    escape unavailable; see the 0193 docstring)."""
    index_migration_name = "0193_run_node_outputs_sweep_index"
    index_path = _VERSIONS / f"{index_migration_name}.py"
    assert index_path.exists(), f"Migration file missing: {index_path}"
    spec = importlib.util.spec_from_file_location(f"migration_{index_migration_name}", index_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == index_migration_name
    assert module.down_revision == "0192_run_node_outputs"

    # The twin discipline: the inlined literal equals sorted(TERMINAL_STATUSES)
    # and the predicate is assembled FROM the tuple (cannot drift by
    # construction).
    assert tuple(sorted(TERMINAL_STATUSES)) == module._TERMINAL_RUN_STATUSES
    expected_predicate = "status IN (" + ", ".join(f"'{s}'" for s in module._TERMINAL_RUN_STATUSES) + ")"
    assert expected_predicate == module._TERMINAL_PREDICATE_SQL
    for status in module._TERMINAL_RUN_STATUSES:
        assert f"'{status}'" in module._TERMINAL_PREDICATE_SQL

    # The CREATE statement carries the index columns + the assembled
    # predicate, and is idempotent (release.sh retries migrations 3x).
    assert module._INDEX_NAME == "ix_runs_org_completed_at_terminal_sweep"
    create_sql = module._CREATE_INDEX_SQL
    assert "CREATE INDEX IF NOT EXISTS ix_runs_org_completed_at_terminal_sweep" in create_sql
    assert "ON runs (organisation_id, completed_at)" in create_sql
    assert expected_predicate in create_sql
    # 0171-precedent deploy safety: NOT concurrent (see the module docstring),
    # and the downgrade mirrors the idempotency.
    index_code = index_path.read_text(encoding="utf-8").split('"""', 2)[2]
    assert "postgresql_concurrently" not in index_code
    assert "autocommit_block" not in index_code
    assert "DROP INDEX IF EXISTS" in index_code
    # Postgres-only guard; SQLite is a no-op — BOTH upgrade and downgrade.
    assert index_code.count("if not _is_postgres(bind):") == 2
    # The numbering-shift note (B2b drop -> 0194, PR C pointer -> 0195).
    assert "0194" in index_path.read_text(encoding="utf-8")
    assert "0195" in index_path.read_text(encoding="utf-8")


def test_quarantine_sql_shape_is_present() -> None:
    """Sentinel-named ids quarantine the run's blobs to the side table + the
    legs skip quarantined runs — data anomalies never abort."""
    code = _source_code()
    assert "_QUARANTINE_TABLE" in code
    assert "ON CONFLICT (run_id) DO NOTHING" in code
    assert "_NOT_QUARANTINED_SQL" in code
    assert "left(k, 2) = '__'" in code
    assert "left(mk.key, 2) = '__'" in code


def test_backfill_is_recent_windowed_and_idempotent() -> None:
    code = _source_code()
    assert "make_interval(days => :window_days)" in code
    assert "COALESCE(r.completed_at, r.updated_at)" in code
    assert code.count("ON CONFLICT") >= 5
    assert "_CHUNK_SIZE = 1000" in code
    assert "ORDER BY r.id LIMIT :chunk_size" in code


def test_markers_leg_covers_unknown_status_runs() -> None:
    code = _source_code()
    assert "r.status = 'unknown'" in code
    assert "left(mk.key, 2) <> '__'" in code
    assert "_UNKNOWN_NODE_ID" in code


def test_coverage_verification_aborts_on_infrastructure_failure() -> None:
    code = _source_code()
    assert "_verify_window_coverage" in code
    assert "backfill coverage violation" in code
