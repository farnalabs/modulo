"""Unit tests for pure/leaf helpers in node_runner.

Covers the self-contained functions that need no DB, no sandbox, and no
LangGraph runtime: the claim attempt-key discriminator, the self-reported cost
clamp/extraction authority, sandbox cost estimation, log-entry combining,
marker text normalisation, the delivery-sentinel matcher, and the HITL
required-team-id normaliser.
"""

import uuid
from typing import Any

import pytest

from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    _build_model_cost_fields,
    _build_sandbox_node_envelope,
    _build_token_usage_fields,
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
    _run_identity_strs,
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
        first = _claim_token_attempt_suffix("token-abc")
        second = _claim_token_attempt_suffix("token-abc")
        assert first == second
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
        assert not _build_model_cost_fields({})

    def test_build_model_cost_fields_populated(self) -> None:
        fields = _build_model_cost_fields({"model_cost_usd": 5})
        assert fields["model_cost_usd"] == 5
        assert fields["model_cost_raw_usd"] == 5
        assert fields["model_cost_clamped"] is False
        assert fields["model_cost_out_of_band_high"] is False


# ---------------------------------------------------------------------------
# _build_token_usage_fields — agent-reported token usage (FAR-491)
# ---------------------------------------------------------------------------


class TestAgentReportedTokenUsage:
    def test_non_dict_output_json_omits_everything(self) -> None:
        assert not _build_token_usage_fields(None)
        assert not _build_token_usage_fields("nope")
        assert not _build_token_usage_fields(42)

    def test_token_usage_absent_or_non_dict_omits_everything(self) -> None:
        assert not _build_token_usage_fields({})
        assert not _build_token_usage_fields({"other": 1})
        assert not _build_token_usage_fields({"token_usage": "not-a-dict"})
        assert not _build_token_usage_fields({"token_usage": None})

    def test_valid_report_full_including_cache_keys(self) -> None:
        fields = _build_token_usage_fields(
            {"token_usage": {"input": 1234, "output": 567, "total": 1801, "cache_read": 100, "cache_write": 8}}
        )
        assert fields == {
            "model_tokens_input": 1234,
            "model_tokens_output": 567,
            "model_tokens_total": 1801,
            "model_tokens_cache_read": 100,
            "model_tokens_cache_write": 8,
        }

    def test_valid_report_without_cache_keys_omits_cache_fields(self) -> None:
        fields = _build_token_usage_fields({"token_usage": {"input": 10, "output": 5, "total": 15}})
        assert fields == {"model_tokens_input": 10, "model_tokens_output": 5, "model_tokens_total": 15}
        assert "model_tokens_cache_read" not in fields
        assert "model_tokens_cache_write" not in fields

    @pytest.mark.parametrize("bad_value", ["123", 1.5, None, True, False, {"a": 1}, [1]])
    def test_invalid_value_omits_only_that_key(self, bad_value: object) -> None:
        fields = _build_token_usage_fields({"token_usage": {"input": bad_value, "output": 5, "total": 15}})
        assert "model_tokens_input" not in fields
        assert fields["model_tokens_output"] == 5
        assert fields["model_tokens_total"] == 15

    @pytest.mark.parametrize("negative_field", ["input", "output", "total", "cache_read", "cache_write"])
    def test_negative_value_omits_that_key(self, negative_field: str) -> None:
        usage: dict[str, int] = {"input": 10, "output": 5, "total": 15, "cache_read": 2, "cache_write": 1}
        usage[negative_field] = -1
        fields = _build_token_usage_fields({"token_usage": usage})
        producer_to_field = {
            "input": "model_tokens_input",
            "output": "model_tokens_output",
            "total": "model_tokens_total",
            "cache_read": "model_tokens_cache_read",
            "cache_write": "model_tokens_cache_write",
        }
        assert producer_to_field[negative_field] not in fields
        # The remaining four keys still extract.
        assert len(fields) == 4

    def test_valid_zero_is_a_real_report_and_is_written(self) -> None:
        fields = _build_token_usage_fields({"token_usage": {"input": 0, "output": 0, "total": 0}})
        assert fields == {"model_tokens_input": 0, "model_tokens_output": 0, "model_tokens_total": 0}

    def test_envelope_carries_reported_tokens_in_both_views(self) -> None:
        """The envelope's inner (artifact) and outer (telemetry) views both
        carry the extracted fields; a node without a report carries none."""
        output = nr._SandboxNodeOutput(
            status="completed",
            summary="did the thing",
            exit_code=0,
            wall_clock_time_ms=1200,
            cost_estimate_usd=0.01,
            cost_source={"token_usage": {"input": 1234, "output": 567, "total": 1801, "cache_read": 100}},
        )
        envelope = _build_sandbox_node_envelope(node_id="n1", output=output)
        inner = envelope["artifacts"][0]["output"]
        outer = envelope["output"]
        for view in (inner, outer):
            assert view["model_tokens_input"] == 1234
            assert view["model_tokens_output"] == 567
            assert view["model_tokens_total"] == 1801
            assert view["model_tokens_cache_read"] == 100
            assert "model_tokens_cache_write" not in view

        silent = nr._SandboxNodeOutput(
            status="completed",
            summary="no report",
            exit_code=0,
            wall_clock_time_ms=50,
            cost_estimate_usd=0.0,
            cost_source={"summary": "nothing"},
        )
        quiet_envelope = _build_sandbox_node_envelope(node_id="n2", output=silent)
        assert not any(key.startswith("model_tokens_") for key in quiet_envelope["artifacts"][0]["output"])
        assert not any(key.startswith("model_tokens_") for key in quiet_envelope["output"])

    def test_schema_drift_suppresses_token_report(self) -> None:
        """A truthy producer ``schema_drift`` flag suppresses the token report
        entirely (returns ``{}``) — mirroring ``_extract_reported_cost``: a
        drifted-schema node reports NO tokens."""
        assert not _build_token_usage_fields(
            {"schema_drift": True, "token_usage": {"input": 10, "output": 5, "total": 15}}
        )

    def test_clean_producer_report_extracts_normally(self) -> None:
        fields = _build_token_usage_fields(
            {"schema_drift": False, "token_usage": {"input": 10, "output": 5, "total": 15}}
        )
        assert fields == {"model_tokens_input": 10, "model_tokens_output": 5, "model_tokens_total": 15}

    def test_drifted_producer_tokens_not_folded_into_envelope(self) -> None:
        """A drifted producer's ``token_usage`` is NOT folded into the node
        output (no ``model_tokens_*`` in either view — so no ``reported_*``
        keys ever fold downstream), while a clean producer's is."""
        drifted = nr._SandboxNodeOutput(
            status="completed",
            summary="drifted producer",
            exit_code=0,
            wall_clock_time_ms=1200,
            cost_estimate_usd=0.01,
            cost_source={"schema_drift": True, "token_usage": {"input": 1234, "output": 567, "total": 1801}},
        )
        drifted_envelope = _build_sandbox_node_envelope(node_id="n1", output=drifted)
        for view in (drifted_envelope["artifacts"][0]["output"], drifted_envelope["output"]):
            assert not any(key.startswith("model_tokens_") for key in view)

        clean = nr._SandboxNodeOutput(
            status="completed",
            summary="clean producer",
            exit_code=0,
            wall_clock_time_ms=1200,
            cost_estimate_usd=0.01,
            cost_source={"schema_drift": False, "token_usage": {"input": 1234, "output": 567, "total": 1801}},
        )
        clean_envelope = _build_sandbox_node_envelope(node_id="n2", output=clean)
        for view in (clean_envelope["artifacts"][0]["output"], clean_envelope["output"]):
            assert view["model_tokens_input"] == 1234
            assert view["model_tokens_output"] == 567
            assert view["model_tokens_total"] == 1801


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
        assert not _log_entry_text({})
        assert not _log_entry_text({"message": None, "fields": None})

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
        assert not _normalize_marker_text(None)
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


# ---------------------------------------------------------------------------
# _run_identity_strs
# ---------------------------------------------------------------------------


class TestRunIdentityStrs:
    """gh-1802: internal state identity keys never render as the string "None".

    An explicit ``None`` value in state must produce the same result a missing
    key produces (the empty string) — the sandbox agent's run/pipeline/org
    identity strings are derived from these keys on every node execution.
    """

    def test_missing_keys_yield_empty_strings(self) -> None:
        assert _run_identity_strs({}) == ("", "", "")

    def test_explicit_none_yields_empty_strings(self) -> None:
        state: dict[str, Any] = {"_run_id": None, "_pipeline_id": None, "_org_id": None}
        assert _run_identity_strs(state) == ("", "", "")

    def test_string_values_pass_through(self) -> None:
        state = {"_run_id": "run-1", "_pipeline_id": "pipe-2", "_org_id": "org-3"}
        assert _run_identity_strs(state) == ("run-1", "pipe-2", "org-3")

    def test_uuid_and_int_values_coerce_to_str(self) -> None:
        run_uuid = uuid.uuid4()
        state: dict[str, Any] = {"_run_id": run_uuid, "_pipeline_id": 7, "_org_id": None}
        assert _run_identity_strs(state) == (str(run_uuid), "7", "")
