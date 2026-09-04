"""Structural unit tests for migration 0176_run_node_outputs (FAR-583).

These run WITHOUT a database. They pin the migration's data contract: the
inlined terminal-status literal (migrations cannot import app constants), the
anchored marker-key twin parser (Python twin + the bound regex shared with the
repo module), the quarantine behaviour markers in the SQL, the jsonb
invariant assertion (the documented NO-NUL-PRE-SCAN deviation guard), the
0066/0131 ownership-ceremony grants (including the FIRST-ever
``GRANT REFERENCES ON public.runs``), and the 30-day recent-window +
idempotency SQL shape. The live-Postgres behaviour (ceremony, backfill
round-trip, quarantine) is covered by the testcontainers integration suite.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from modulo.db.crud.run_node_outputs import _MARKER_KEY_RE as REPO_MARKER_KEY_RE
from modulo.db.models.run import TERMINAL_STATUSES
from modulo.db.models.run_node_outputs import UNKNOWN_NODE_ID

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0176_run_node_outputs"
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
    # so the current alembic heads tip is 0174 — NOT 0175.
    assert module.down_revision == "0174_per_org_last_admin_guard"
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


def test_marker_regex_is_identical_to_repo_module() -> None:
    """The SQL-side regex is bound as a parameter from this constant; it must
    stay byte-identical to crud.run_node_outputs._MARKER_KEY_RE or the twin
    parsers diverge silently."""
    module = _load_migration()
    assert module._MARKER_KEY_RE == REPO_MARKER_KEY_RE


def _assert_parses(module: ModuleType, attempt_key: str, expected_node_id: str) -> None:
    assert module._parse_marker_node_id(attempt_key) == expected_node_id


def test_twin_parser_round_trips_plain_and_colon_node_ids() -> None:
    module = _load_migration()
    run_id = "01234567-89ab-cdef-0123-456789abcdef"
    _assert_parses(module, f"run:{run_id}:node:node1:3", "node1")
    # Colon-containing node ids are safe: the split is on the LAST colon.
    _assert_parses(module, f"run:{run_id}:node:my:weird:node:7", "my:weird:node")
    _assert_parses(module, f"run:{run_id}:node:n:{run_id}", "n")
    _assert_parses(module, f"run:{run_id}:node:a:b:claim-unknown", "a:b")
    _assert_parses(module, f"run:{run_id}:node:x:0123abcd", "x")


def test_twin_parser_maps_unparseable_keys_to_unknown_sentinel() -> None:
    module = _load_migration()
    run_id = "01234567-89ab-cdef-0123-456789abcdef"
    _assert_parses(module, "junk", UNKNOWN_NODE_ID)
    _assert_parses(module, "run:not-a-uuid:node:x:1", UNKNOWN_NODE_ID)
    _assert_parses(module, f"run:{run_id}:node:nosuffix", UNKNOWN_NODE_ID)
    _assert_parses(module, f"run:{run_id}:node:", UNKNOWN_NODE_ID)
    _assert_parses(module, f"run:{run_id}:node::suffix", UNKNOWN_NODE_ID)
    _assert_parses(module, f"run:{run_id}:node:x:", UNKNOWN_NODE_ID)


def test_twin_parser_keeps_sentinel_named_node_ids_parseable() -> None:
    """Sentinel-NAMED ids still parse (they are valid grammar); the
    quarantine step — not the parser — is what keeps those runs out."""
    module = _load_migration()
    run_id = "01234567-89ab-cdef-0123-456789abcdef"
    _assert_parses(module, f"run:{run_id}:node:__run_meta__:1", "__run_meta__")
    _assert_parses(module, f"run:{run_id}:node:__weird__:fallback", "__weird__")


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
