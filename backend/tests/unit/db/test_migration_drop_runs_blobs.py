"""Structural unit tests for migration 0212 (FAR-583 B2b — the DROP migration).

Pins the statement-level invariants: the statement inventory in execution
order (drain assert -> jsonb assertion -> junk abort -> INSERT-only repair ->
content parity -> DROP INDEX + DROP COLUMN x3), the terminal-status literal
twin (sorted(TERMINAL_STATUSES)), the embedded B1 cutoff constant, the
INFLIGHT drain set (running/claimed/awaiting_human), the marker key-grammar
twin (byte-identical to the repo module), the quarantine table KEPT, and the
RAISING downgrade.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from modulo.db.crud.run_node_outputs import _MARKER_KEY_PREFIX_LEN
from modulo.db.models.run import TERMINAL_STATUSES

MIGRATION = Path("src/modulo/db/migrations/versions/0212_drop_runs_blob_columns.py")

code = MIGRATION.read_text(encoding="utf-8")

# The same byte-for-byte constant the repo module carries (pinned twin).
_MARKER_RE = r"^run:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:node:.+$"


class TestStructure:
    def test_statement_inventory_in_order(self) -> None:
        upgrade_body = code[code.index("def upgrade() -> None:") :]
        drain = upgrade_body.index("_DRAIN_COUNT_SQL")
        shape = upgrade_body.index("_SHAPE_ASSERT_SQL")
        junk = upgrade_body.index("_JUNK_SAMPLE_SQL")
        repair = upgrade_body.index("_repair_installation(")
        parity = upgrade_body.index("_parity_outputs_telemetry")
        drops = upgrade_body.index("DROP INDEX IF EXISTS")
        assert drain < shape < junk < repair < parity < drops

    def test_drop_columns(self) -> None:
        assert "ALTER TABLE runs DROP COLUMN {col}" in code
        assert "outputs_json" in code
        assert "node_telemetry_json" in code
        assert "raw_output_markers" in code

    def test_drop_index(self) -> None:
        assert "ix_runs_org_completed_at_terminal_sweep" in code

    def test_quarantine_table_is_kept(self) -> None:
        # B2b: the quarantine side table is NOT dropped (post-drop it is the
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
        inflight = re.search(r"_INFLIGHT_RUN_STATUSES = \((.*?)\)", code, re.DOTALL)
        assert inflight is not None
        assert set(re.findall(r'"([a-z_]+)"', inflight.group(1))) == {"running", "claimed", "awaiting_human"}

    def test_drain_bounded_to_pre_b1_created_runs(self) -> None:
        assert "r.created_at < :b1_cutoff" in code

    def test_repair_is_insert_only_never_overwrites(self) -> None:
        assert "ON CONFLICT DO NOTHING" in code

    def test_parity_is_legacy_authoritative_on_mismatch(self) -> None:
        assert "LEGACY-AUTHORITATIVE" in code
        assert "IS DISTINCT FROM" in code

    def test_markers_parity_is_jsonb_subset(self) -> None:
        assert "raw_output_markers <> '{}'::jsonb" in code

    def test_structural_anomaly_uses_limit_1_sample_and_separate_count(self) -> None:
        assert "_JUNK_SAMPLE_SQL = (" in code
        assert "_JUNK_POPULATION_SQL = (" in code
        assert "LIMIT 1" in code

    def test_lock_timeout_bounded_retry(self) -> None:
        assert "lock_timeout" in code
        assert "_DROP_COL_RETRIES = 3" in code
        assert "time.sleep" in code


class TestDowngrade:
    def load_module(self):
        spec = importlib.util.spec_from_file_location("migration0212", MIGRATION)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_downgrade_raises_never_rewinds(self) -> None:
        module = self.load_module()
        assert module.revision == "0212_drop_runs_blob_columns"
        assert module.down_revision == "0211_variant_batch_state"
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
