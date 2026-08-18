"""Unit tests for pure/leaf helpers in node_runner.

Covers the self-contained functions that need no DB, no sandbox, and no
LangGraph runtime: the claim attempt-key discriminator, the self-reported cost
clamp/extraction authority, sandbox cost estimation, log-entry combining,
marker text normalisation, the delivery-sentinel matcher, and the HITL
required-team-id normaliser.
"""

import uuid

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    _build_model_cost_fields,
    _claim_token_attempt_suffix,
    _combine_log_entries,
    _compile_delivery_sentinel_pattern,
    _compute_sandbox_cost,
    _dispatch_marker_json,
    _effective_self_reported_cap,
    _extract_reported_cost,
    _log_entry_text,
    _marker_delivery_done_for_node,
    _normalize_marker_text,
    _normalize_required_team_id,
    _source_contains_delivery_sentinel,
)

# ---------------------------------------------------------------------------
# _normalize_required_team_id
# ---------------------------------------------------------------------------


class TestNormalizeRequiredTeamId:
    def test_none_returns_none(self) -> None:
        assert _normalize_required_team_id("gate-1", None) is None

    def test_uuid_passthrough(self) -> None:
        team_id = uuid.uuid4()
        assert _normalize_required_team_id("gate-1", team_id) == str(team_id)

    def test_valid_string(self) -> None:
        team_id = uuid.uuid4()
        assert _normalize_required_team_id("gate-1", str(team_id)) == str(team_id)

    def test_invalid_string_returns_none(self) -> None:
        assert _normalize_required_team_id("gate-1", "not-a-uuid") is None

    def test_garbage_type_returns_none(self) -> None:
        assert _normalize_required_team_id("gate-1", object()) is None
        assert _normalize_required_team_id("gate-1", b"bytes") is None


# ---------------------------------------------------------------------------
# _claim_token_attempt_suffix / _dispatch_marker_json
# ---------------------------------------------------------------------------


class TestClaimTokenAttemptSuffix:
    def test_no_lease_returns_unknown(self) -> None:
        assert _claim_token_attempt_suffix(None) == "claim-unknown"
        assert _claim_token_attempt_suffix("") == "claim-unknown"

    def test_lease_is_truncated_sha(self) -> None:
        suffix = _claim_token_attempt_suffix("token-abc")
        assert len(suffix) == 16
        assert suffix != "token-abc"
        # Deterministic per token.
        assert _claim_token_attempt_suffix("token-abc") == _claim_token_attempt_suffix("token-abc")
        assert _claim_token_attempt_suffix("token-abc") != _claim_token_attempt_suffix("token-abd")

    def test_dispatch_marker_json_embeds_attempt_key(self) -> None:
        marker = _dispatch_marker_json("run:1:node:a:2")
        assert marker == '{"state": "dispatching", "attempt_key": "run:1:node:a:2"}'


# ---------------------------------------------------------------------------
# _effective_self_reported_cap / _extract_reported_cost / _build_model_cost_fields
# ---------------------------------------------------------------------------


class TestSelfReportedCost:
    def test_effective_cap_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Settings:
            effective_max_self_reported_usd = 123.5

        monkeypatch.setattr("modulo.settings.get_settings", lambda: _Settings())
        assert _effective_self_reported_cap() == 123.5

    def test_effective_cap_falls_back_to_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise ImportError("no settings")

        monkeypatch.setattr("modulo.settings.get_settings", _boom)
        from modulo.core.cost_controller.breakdown.constants import MAX_SELF_REPORTED_USD

        assert _effective_self_reported_cap() == float(MAX_SELF_REPORTED_USD)

    def test_extract_rejects_non_dict(self) -> None:
        assert _extract_reported_cost(None) is None
        assert _extract_reported_cost("nope") is None

    def test_extract_rejects_schema_drift(self) -> None:
        assert _extract_reported_cost({"schema_drift": True, "model_cost_usd": 5}) is None

    def test_extract_missing_key_returns_none(self) -> None:
        assert _extract_reported_cost({}) is None
        assert _extract_reported_cost({"other": 1}) is None

    def test_extract_rejects_bool(self) -> None:
        assert _extract_reported_cost({"model_cost_usd": True}) is None

    def test_extract_rejects_non_numeric(self) -> None:
        assert _extract_reported_cost({"model_cost_usd": "abc"}) is None
        assert _extract_reported_cost({"model_cost_usd": float("nan")}) is None
        assert _extract_reported_cost({"model_cost_usd": float("inf")}) is None

    def test_extract_rejects_non_positive(self) -> None:
        assert _extract_reported_cost({"model_cost_usd": 0}) is None
        assert _extract_reported_cost({"model_cost_usd": -5}) is None

    def test_extract_reads_raw_then_legacy(self) -> None:
        raw, clamped, was_clamped, oob = _extract_reported_cost(
            {"model_cost_raw_usd": "7.5", "model_cost_usd": 1},
            per_node_cap=100.0,
        )
        assert raw == 7.5
        assert clamped == 7.5
        assert was_clamped is False
        assert oob is False

    def test_extract_clamps_at_band(self) -> None:
        raw, clamped, was_clamped, oob = _extract_reported_cost(
            {"model_cost_usd": 200},
            per_node_cap=1000.0,
            max_reportable_band_usd=50.0,
        )
        assert raw == 200
        assert clamped == 50.0
        assert was_clamped is True
        assert oob is True

    def test_extract_clamps_at_per_node_cap(self) -> None:
        raw, clamped, was_clamped, oob = _extract_reported_cost(
            {"model_cost_usd": 200},
            per_node_cap=50.0,
            max_reportable_band_usd=1000.0,
        )
        assert raw == 200
        assert clamped == 50.0
        assert was_clamped is True
        assert oob is False

    def test_extract_floor(self) -> None:
        assert _extract_reported_cost({"model_cost_usd": 0.000001}, max_reportable_usd_min=0.01) is None

    def test_build_model_cost_fields_empty_without_report(self) -> None:
        assert _build_model_cost_fields({}) == {}

    def test_build_model_cost_fields_populated(self) -> None:
        fields = _build_model_cost_fields({"model_cost_usd": 5})
        assert fields["model_cost_usd"] == 5
        assert fields["model_cost_raw_usd"] == 5
        assert fields["model_cost_clamped"] is False
        assert fields["model_cost_out_of_band_high"] is False


# ---------------------------------------------------------------------------
# _compute_sandbox_cost
# ---------------------------------------------------------------------------


class TestComputeSandboxCost:
    def test_zero_cost_with_no_reports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("modulo.settings.get_settings", lambda: type("S", (), {"e2b_sandbox_usd_per_hour": 0.13})())
        assert _compute_sandbox_cost(0.0, {}) == 0.0

    def test_non_finite_total_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("modulo.settings.get_settings", lambda: type("S", (), {"e2b_sandbox_usd_per_hour": 0.13})())
        assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": float("nan")}) == 0.0
        assert _compute_sandbox_cost(0.0, {"cost_estimate_usd": "not-a-number"}) == 0.0

    def test_combines_sandbox_and_agent_costs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("modulo.settings.get_settings", lambda: type("S", (), {"e2b_sandbox_usd_per_hour": 0.12})())
        total = _compute_sandbox_cost(3600.0, {"cost_estimate_usd": 1.5})
        assert total == pytest.approx(0.12 + 1.5, abs=1e-6)

    def test_rate_lookup_failure_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise ImportError("no settings")

        monkeypatch.setattr("modulo.settings.get_settings", _boom)
        total = _compute_sandbox_cost(3600.0, {})
        assert total == pytest.approx(nr._E2B_SANDBOX_USD_PER_HOUR, abs=1e-6)


# ---------------------------------------------------------------------------
# Log entry helpers
# ---------------------------------------------------------------------------


class TestLogEntryHelpers:
    def test_log_entry_text_priority(self) -> None:
        assert _log_entry_text({"message": "hello"}) == "hello"
        assert _log_entry_text({"fields": {"k": "v"}}) == str({"k": "v"})
        assert _log_entry_text({}) == ""
        assert _log_entry_text({"message": None, "fields": None}) == ""

    def test_combine_prefers_informative_levels(self) -> None:
        entries = [
            {"level": "debug", "message": "debug line"},
            {"level": "error", "message": "error line"},
            {"message": "bare line"},
            "not-a-dict",
        ]
        combined = _combine_log_entries(entries, limit=10)
        assert combined == ["error line", "debug line", "bare line"]

    def test_combine_tails_to_limit(self) -> None:
        entries = [{"message": f"line-{i}"} for i in range(10)]
        combined = _combine_log_entries(entries, limit=3)
        assert combined == ["line-7", "line-8", "line-9"]

    def test_combine_skips_empty(self) -> None:
        entries = [{"message": ""}, {"message": None}, {"message": "real"}]
        assert _combine_log_entries(entries, 10) == ["real"]


# ---------------------------------------------------------------------------
# Marker text / delivery sentinel
# ---------------------------------------------------------------------------


class TestMarkerTextAndSentinel:
    def test_normalize_marker_text(self) -> None:
        assert _normalize_marker_text(None) == ""
        assert _normalize_marker_text(b"bytes") == "bytes"
        assert _normalize_marker_text(123) == "123"

    def test_compile_sentinel_pattern(self) -> None:
        pattern = _compile_delivery_sentinel_pattern("DONE")
        assert pattern is not None
        assert pattern.search("DONE") is not None
        assert pattern.search("DONE\r") is not None
        assert pattern.search("prefix DONE suffix") is None

    def test_compile_sentinel_pattern_none(self) -> None:
        assert _compile_delivery_sentinel_pattern(None) is None
        assert _compile_delivery_sentinel_pattern("") is None
        assert _compile_delivery_sentinel_pattern(b"bytes") is None

    def test_source_contains_delivery_sentinel(self) -> None:
        assert _source_contains_delivery_sentinel("log line\nDONE\nnext", "DONE") is True
        assert _source_contains_delivery_sentinel("mid DONE line", "DONE") is False
        assert _source_contains_delivery_sentinel("anything", None) is False
        assert _source_contains_delivery_sentinel(None, "DONE") is False

    def test_marker_delivery_done_for_node(self) -> None:
        markers = {
            "k1": {
                "delivery_done": True,
                "attempt_key": "run:run-1:node:node-a:1",
            },
            "k2": {
                "delivery_done": True,
                "attempt_key": "run:run-1:node:node-b:1",
            },
            "k3": {"delivery_done": False, "attempt_key": "run:run-1:node:node-a:2"},
            "k4": "not-a-dict",
        }
        assert _marker_delivery_done_for_node(markers, "run-1", "node-a") is True
        assert _marker_delivery_done_for_node(markers, "run-1", "node-c") is False
        assert _marker_delivery_done_for_node(None, "run-1", "node-a") is False

    def test_marker_delivery_done_delimiter_trap(self) -> None:
        markers = {"k1": {"delivery_done": True, "attempt_key": "run:run-11:node:node-a:1"}}
        # run-1 must NOT match run-11 (delimiter trap).
        assert _marker_delivery_done_for_node(markers, "run-1", "node-a") is False
