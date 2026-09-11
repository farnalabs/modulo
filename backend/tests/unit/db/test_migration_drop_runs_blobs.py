"""Structural unit tests for migration 0215 (FAR-583 — the DROP migration).

Pins the statement-level invariants: the statement inventory in execution
order (drain assert -> jsonb assertion -> junk aborts -> INSERT-only repair ->
content parity -> DROP INDEX + DROP COLUMN x3), the terminal-status literal
twin (sorted(TERMINAL_STATUSES)), the embedded B1 cutoff constant, the
INFLIGHT drain set (running/claimed/awaiting_human/pending/hitl_parked), the
marker key-grammar twin (byte-identical to the repo module), the quarantine
table KEPT, the qa-gate fixes (quarantine-excluded + narrowed junk scans,
jsonb-null folds, the __ sentinel filter on the out/tel leg, the bounded
overwrite loop, the shared retry helper for the index AND column drops,
status-aware markers parity), and the RAISING downgrade.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from modulo.db.crud.run_node_outputs import _MARKER_KEY_PREFIX_LEN
from modulo.db.models.run import TERMINAL_STATUSES

MIGRATION = Path("src/modulo/db/migrations/versions/0215_drop_runs_blob_columns.py")

code = MIGRATION.read_text(encoding="utf-8")

# The same byte-for-byte constant the repo module carries (pinned twin).
_MARKER_RE = r"^run:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:node:.+$"


class TestStructure:
    def test_statement_inventory_in_order(self) -> None:
        upgrade_body = code[code.index("def upgrade() -> None:") :]
        drain = upgrade_body.index("_DRAIN_COUNT_SQL")
        shape = upgrade_body.index("_SHAPE_ASSERT_SQL")
        junk_out_tel = upgrade_body.index("_JUNK_OUT_TEL_SAMPLE_SQL")
        junk_markers = upgrade_body.index("_JUNK_MARKERS_SAMPLE_SQL")
        repair = upgrade_body.index("_repair_installation(")
        parity = upgrade_body.index("_parity_outputs_telemetry")
        drops = upgrade_body.index("DROP INDEX IF EXISTS")
        assert drain < shape < junk_out_tel < junk_markers < repair < parity < drops

    def test_drop_columns(self) -> None:
        assert "ALTER TABLE runs DROP COLUMN {col}" in code
        assert "outputs_json" in code
        assert "node_telemetry_json" in code
        assert "raw_output_markers" in code

    def test_drop_index(self) -> None:
        assert "ix_runs_org_completed_at_terminal_sweep" in code

    def test_both_drops_share_the_retry_helper(self) -> None:
        # qa gate fix 4: BOTH the swept index drop AND each column drop route
        # through the shared lock_timeout + bounded savepoint retry helper.
        assert "_drop_with_retry(" in code
        assert 'description="DROP of the 0193 sweep index "' in code
        assert 'description=f"DROP COLUMN runs.{col}"' in code

    def test_quarantine_table_is_kept(self) -> None:
        # The quarantine side table is NOT dropped (post-drop it is the
        # 0192-quarantined evidence's only surviving copy).
        assert "run_node_outputs_quarantine" in code

    def test_cutoff_embedded_with_comment(self) -> None:
        assert "2026-09-10T11:25:42" in code
        assert "PR #298" in code

    def test_terminal_literal_equals_sorted_terminal_statuses(self) -> None:
        literal = re.search(r"_TERMINAL_RUN_STATUSES = \((.*?)\)", code, re.DOTALL)
        assert literal is not None
        statuses = re.findall(r'"([a-z_]+)"', literal.group(1))
        assert statuses == sorted(TERMINAL_STATUSES)

    def test_drain_covers_inflight_statuses(self) -> None:
        # qa gate fix 1: pending + hitl_parked added — a pre-B1 parked run's
        # legacy markers would otherwise be silently destroyed; pending is
        # fail-safe zero-cost.
        inflight = re.search(r"_INFLIGHT_RUN_STATUSES = \((.*?)\)", code, re.DOTALL)
        assert inflight is not None
        assert set(re.findall(r'"([a-z_]+)"', inflight.group(1))) == {
            "awaiting_human",
            "claimed",
            "hitl_parked",
            "pending",
            "running",
        }

    def test_drain_bounded_to_pre_b1_created_runs(self) -> None:
        assert "r.created_at < :b1_cutoff" in code

    def test_repair_is_insert_only_never_overwrites(self) -> None:
        assert "ON CONFLICT DO NOTHING" in code

    def test_parity_is_legacy_authoritative_on_mismatch(self) -> None:
        assert "LEGACY-AUTHORITATIVE" in code
        assert "IS DISTINCT FROM" in code

    def test_markers_parity_is_jsonb_subset(self) -> None:
        assert "raw_output_markers <> '{}'::jsonb" in code

    def test_junk_scan_is_quarantined_excluded_and_narrowed(self) -> None:
        # qa gate fix 2: the junk scans carry the quarantine exclusion and are
        # narrowed to the loop-covering populations (out/tel -> terminal only;
        # markers -> terminal + unknown).
        assert "_JUNK_OUT_TEL_SAMPLE_SQL = (" in code
        assert "_JUNK_OUT_TEL_POPULATION_SQL = (" in code
        assert "_JUNK_MARKERS_SAMPLE_SQL = (" in code
        assert "_JUNK_MARKERS_POPULATION_SQL = (" in code
        assert "LIMIT 1" in code
        out_tel_block = code[code.index("_JUNK_OUT_TEL_POPULATION_SQL") : code.index("_JUNK_OUT_TEL_SAMPLE_SQL")]
        assert "_NOT_QUARANTINED_SQL" in out_tel_block
        assert "r.status IN {_TERMINAL_SQL}" in out_tel_block
        markers_block = code[code.index("_JUNK_MARKERS_POPULATION_SQL") : code.index("_JUNK_MARKERS_SAMPLE_SQL")]
        assert "_NOT_QUARANTINED_SQL" in markers_block
        assert "_REPAIR_STATUS_SQL" in markers_block

    def test_junk_scan_carves_out_jsonb_null(self) -> None:
        # qa gate fix 3: jsonb_typeof <> 'object' alone aborts on a stored
        # jsonb 'null' VALUE — 0192 explicitly carved that out (IS DISTINCT
        # FROM 'null'); the drop scans must too.
        assert len(re.findall(r"jsonb_typeof\(r\.[a-z_]+\) IS DISTINCT FROM 'null'", code)) >= 6

    def test_jsonb_null_folds_to_absent_in_walks_and_parity(self) -> None:
        # qa gate fix 3 (folding): the blob-bearing walks use the folded
        # has-blob predicate; the parity comparison NULL-folds the legacy
        # sides so a stored jsonb 'null' never mismatches an absent side.
        assert "_HAS_OUT_TEL_BLOB_SQL" in code
        assert "_FOLDED_OUT_SQL" in code
        assert "_FOLDED_TEL_SQL" in code
        mismatch_block = code[code.index("_PARITY_OUT_TEL_MISMATCH_SQL") : code.index("_PARITY_OUT_POPULATION_SQL")]
        assert "_FOLDED_OUT_SQL} IS DISTINCT FROM {_REASM_OUT_SQL}" in mismatch_block
        candidates_block = code[
            code.index("_REPAIR_CANDIDATES_TERMINAL_SQL") : code.index("_REPAIR_CANDIDATES_UNKNOWN_SQL")
        ]
        assert "_HAS_OUT_TEL_BLOB_SQL} " in candidates_block

    def test_out_tel_repair_leg_has_the_sentinel_filter(self) -> None:
        # qa gate fix 7: the out/tel repair leg's key projection excludes
        # __-prefixed keys (the sentinel-namespace squatting shape that a
        # metadata-row PK collision turns into a non-round-trippable blob).
        out_tel_block = code[code.index("_REPAIR_OUT_TEL_BODY_SQL") : code.index("_REPAIR_META_BODY_SQL")]
        assert "WHERE left(u.k, 2) <> '__'" in out_tel_block

    def test_overwrite_loop_is_bounded_per_run(self) -> None:
        # qa gate fix 7: a non-round-trippable blob must abort, not spin.
        assert "_MAX_OVERWRITE_ATTEMPTS = 2" in code
        assert "attempts[run_id] = attempts.get(run_id, 0) + 1" in code
        parity_body = code[code.index("def _parity_outputs_telemetry") : code.index("def _parity_markers")]
        assert "NOT round-trippable" in parity_body

    def test_unknown_status_repairs_markers_leg_only(self) -> None:
        # qa gate fix 6: 0192's twin geometry is markers-only for the
        # unknown-status population.
        repair_run_body = code[code.index("def _repair_run") : code.index("def _repair_candidates")]
        assert "if unknown_run:" in repair_run_body
        assert "markers" in repair_run_body
        assert "_repair_body(_REPAIR_OUT_TEL_BODY_SQL, unknown_run=False), params)" in repair_run_body

    def test_markers_parity_walk_is_status_aware(self) -> None:
        # qa gate fix 5: the walk returns the run's status and selects the
        # matching repair variant.
        walk_block = code[code.index("_PARITY_MARKERS_MISMATCH_SQL") : code.index("_DELETE_FINAL_ROWS_SQL")]
        assert "SELECT r.id, r.status" in walk_block
        parity_body = code[code.index("def _parity_markers") : code.index("def _drop_with_retry")]
        assert 'unknown_run=status == "unknown"' in parity_body

    def test_tenancy_scoping_documented(self) -> None:
        # qa minors: the repair/parity SQL runs run-id-scoped under the OWNER
        # role — the explicit tenancy model, documented on the SQL block.
        sql_block_header = code[code.index("TENANCY SCOPING") : code.index("# 1) Pre-flight drain assertion")]
        assert "TENANCY SCOPING" in sql_block_header
        assert "OWNER context" in sql_block_header
        assert "explicit tenancy model" in sql_block_header

    def test_lock_timeout_bounded_retry(self) -> None:
        assert "lock_timeout" in code
        assert "_DROP_COL_RETRIES = 3" in code
        assert "time.sleep" in code


class TestDowngrade:
    def load_module(self):
        spec = importlib.util.spec_from_file_location("migration0215", MIGRATION)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_downgrade_raises_never_rewinds(self) -> None:
        module = self.load_module()
        assert module.revision == "0215_drop_runs_blob_columns"
        assert module.down_revision == "0214_connector_instance_indexes_unique"
        with pytest.raises(RuntimeError, match="never rewind past this migration"):
            module.downgrade()

    def test_downgrade_docstring_carries_the_emergency_snippet(self) -> None:
        assert "ALTER TABLE runs ADD COLUMN outputs_json jsonb" in code
        assert "run_node_outputs_quarantine" in code


class TestTwinParser:
    """The migration's marker-node-id parser twin is the repo module's."""

    def test_marker_regex_is_byte_identical_to_the_repo_module(self) -> None:
        from modulo.db.crud.run_node_outputs import _MARKER_KEY_RE

        assert _MARKER_RE == _MARKER_KEY_RE
        assert _MARKER_RE in code

    def test_prefix_len_matches_the_repo_module(self) -> None:
        assert f"_MARKER_KEY_PREFIX_LEN = {_MARKER_KEY_PREFIX_LEN}" in code
        assert _MARKER_KEY_PREFIX_LEN == 46
