"""Unit tests for pure/leaf helpers in node_runner.

Covers the self-contained functions that need no DB, no sandbox, and no
LangGraph runtime: the claim attempt-key discriminator, the self-reported cost
clamp/extraction authority, sandbox cost estimation, log-entry combining,
marker text normalisation, the delivery-sentinel matcher, and the HITL
required-team-id normaliser.
"""

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes.pipelines import PipelineGraphNode
from modulo.cli.apply.models import ApplyGraphNode
from modulo.core.artifacts.store import LocalArtifactStore
from modulo.core.cost_controller.breakdown.params import MAX_REPORTABLE_TOKEN_COUNT, REPORTED_TOKEN_CHAIN
from modulo.core.pipeline_engine import node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    _FULL_MODE_DEFAULT_MAX_BYTES,
    _MAX_ARTIFACT_LOG,
    _build_model_cost_fields,
    _build_sandbox_node_envelope,
    _build_token_usage_fields,
    _claim_token_attempt_suffix,
    _coerce_stdout_max_bytes,
    _coerce_stdout_retention_mode,
    _combine_log_entries,
    _compile_delivery_sentinel_pattern,
    _compute_sandbox_cost,
    _effective_self_reported_cap,
    _extract_reported_cost,
    _log_entry_text,
    _marker_delivery_done_for_node,
    _normalize_marker_text,
    _normalize_required_team_id,
    _persist_full_stdout_artifact,
    _read_org_stdout_retention_ceiling,
    _resolve_stdout_cap,
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
        # D8 (FAR-594): the marker vocabulary moved to
        # runner_capacity.build_dispatch_marker — the tier-less shape is
        # unchanged (no provider key → Docker-tier attribution, fail-safe).
        from modulo.core.runner_capacity import build_dispatch_marker

        marker = build_dispatch_marker("run:1:node:a:2")
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
        # FAR-653: a zero WITHOUT the zero-token proof is unproven — no report;
        # negatives stay rejected outright.
        assert _extract_reported_cost({"model_cost_usd": 0}) is None
        assert _extract_reported_cost({"model_cost_usd": -5}) is None

    def test_extract_accepts_genuine_zero_with_proven_token_usage(self) -> None:
        raw, clamped, was_clamped, oob = _extract_reported_cost(
            {"model_cost_usd": 0.0, "token_usage": {"input": 0, "output": 0, "total": 0}}
        )
        assert raw == 0.0
        assert clamped == 0.0
        assert was_clamped is False
        assert oob is False

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

    def test_integral_float_token_counts_are_accepted(self) -> None:
        """FAR-532 wave-2: a JSON float-encoding of a token count (e.g.
        ``1234.0``) is tolerated — an integral float normalises to ``int``
        (mirroring the cost extractor's finite-numeric tolerance); a
        non-integral float and non-finite floats stay invalid."""
        fields = _build_token_usage_fields(
            {"token_usage": {"input": 1234.0, "output": 5.0, "total": 1801.0, "cache_read": 100.0}}
        )
        assert fields == {
            "model_tokens_input": 1234,
            "model_tokens_output": 5,
            "model_tokens_total": 1801,
            "model_tokens_cache_read": 100,
        }
        assert all(isinstance(value, int) for value in fields.values())

    def test_magnitude_ceiling_omits_implausible_token_counts(self) -> None:
        """FAR-532 wave-2: a pathological 10**18 token report is rejected
        tri-state at extraction (a display-only trust-boundary bound,
        mirroring the cost path's clamp stack); a value AT the ceiling is a
        real report and is kept."""
        fields = _build_token_usage_fields({"token_usage": {"input": 10**18, "output": 5, "total": 15}})
        assert "model_tokens_input" not in fields
        assert fields["model_tokens_output"] == 5
        assert fields["model_tokens_total"] == 15
        at_ceiling = _build_token_usage_fields(
            {"token_usage": {"input": MAX_REPORTABLE_TOKEN_COUNT, "output": 1, "total": 2}}
        )
        assert at_ceiling["model_tokens_input"] == MAX_REPORTABLE_TOKEN_COUNT

    def test_token_usage_field_map_derives_from_shared_chain(self) -> None:
        """FAR-532 wave-2: the extraction map is DERIVED from the shared
        ``REPORTED_TOKEN_CHAIN`` in (src, dst) reading order — the three
        layers (producer key -> ``model_tokens_*`` -> ``reported_*`` ->
        counter) cannot drift apart."""
        assert (
            tuple((binding.producer_key, binding.node_field) for binding in REPORTED_TOKEN_CHAIN)
            == nr._TOKEN_USAGE_FIELD_MAP
        )

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


# ---------------------------------------------------------------------------
# FAR-792: per-node stdout/stderr retention — coercers, resolver, envelope key
# ---------------------------------------------------------------------------


class TestCoerceStdoutRetentionMode:
    """The retention-mode coercer must recognise exactly "tail" | "full" and
    fall back to the SAFE legacy "tail" for anything else — a smuggled value
    can never silently raise the retention cap."""

    def test_recognised_modes_pass_through(self) -> None:
        assert _coerce_stdout_retention_mode("tail") == "tail"
        assert _coerce_stdout_retention_mode("full") == "full"

    def test_missing_and_unknown_fall_back_to_tail(self) -> None:
        assert _coerce_stdout_retention_mode(None) == "tail"
        assert _coerce_stdout_retention_mode("all") == "tail"
        assert _coerce_stdout_retention_mode("Full") == "tail"
        assert _coerce_stdout_retention_mode(True) == "tail"
        assert _coerce_stdout_retention_mode(1) == "tail"
        assert _coerce_stdout_retention_mode([""]) == "tail"


class TestCoerceStdoutMaxBytes:
    """Only a genuine positive byte count can raise the retention cap; every
    malformed value (bool, float, 0, negative, non-numeric) coerces to None."""

    def test_positive_ints_and_numeric_strings_pass(self) -> None:
        assert _coerce_stdout_max_bytes(5_000_000) == 5_000_000
        assert _coerce_stdout_max_bytes("1024") == 1024
        assert _coerce_stdout_max_bytes("100") == 100

    def test_none_passes_through(self) -> None:
        assert _coerce_stdout_max_bytes(None) is None

    def test_booleans_rejected(self) -> None:
        assert _coerce_stdout_max_bytes(True) is None
        assert _coerce_stdout_max_bytes(False) is None

    def test_non_positive_values_rejected(self) -> None:
        assert _coerce_stdout_max_bytes(0) is None
        assert _coerce_stdout_max_bytes(-100) is None
        assert _coerce_stdout_max_bytes("-5") is None

    def test_non_integral_floats_rejected(self) -> None:
        assert _coerce_stdout_max_bytes(5.5) is None
        assert _coerce_stdout_max_bytes("10.25") is None

    def test_integral_float_accepted(self) -> None:
        assert _coerce_stdout_max_bytes(5000.0) == 5000

    def test_non_numeric_rejected(self) -> None:
        assert _coerce_stdout_max_bytes("lots") is None
        assert _coerce_stdout_max_bytes(object()) is None


class TestResolveStdoutCap:
    def test_tail_keeps_legacy_512kb_and_ignores_max_bytes(self) -> None:
        assert _resolve_stdout_cap("tail", None) == _MAX_ARTIFACT_LOG
        assert _resolve_stdout_cap("tail", 9_999_999) == _MAX_ARTIFACT_LOG

    def test_full_without_max_bytes_uses_5mb_default(self) -> None:
        assert _resolve_stdout_cap("full", None) == _FULL_MODE_DEFAULT_MAX_BYTES

    def test_full_honours_max_bytes(self) -> None:
        assert _resolve_stdout_cap("full", 1_024) == 1_024
        assert _resolve_stdout_cap("full", 5_000_000) == 5_000_000

    def test_cap_never_below_512kb_in_full(self) -> None:
        # A full-mode cap smaller than the tail bound is still honoured as-is
        # (the node asked for less retention); the drain window mirrors it.
        assert _resolve_stdout_cap("full", 10_000) == 10_000


class TestResolveStdoutCapOrgCeiling:
    """FAR-811: the org-level ceiling (system_config
    sandbox_stdout_retention_max_bytes) hard-clamps a node's retention cap."""

    def test_org_ceiling_clamps_over_large_node_max_bytes(self) -> None:
        # A node asking for 20MB retention is clamped to the 5MB org ceiling.
        assert _resolve_stdout_cap("full", 20_000_000, org_ceiling=5_000_000) == 5_000_000

    def test_org_ceiling_unset_leaves_node_cap_unchanged(self) -> None:
        # No ceiling configured (None) is a no-op — node behaviour is unchanged.
        assert _resolve_stdout_cap("full", 2_000_000, org_ceiling=None) == 2_000_000
        assert _resolve_stdout_cap("full", 20_000_000, org_ceiling=None) == 20_000_000
        assert _resolve_stdout_cap("tail", None, org_ceiling=None) == _MAX_ARTIFACT_LOG

    def test_org_ceiling_does_not_raise_cap_without_ceiling(self) -> None:
        # A ceiling never raises a node's cap — it can only clamp it down.
        assert _resolve_stdout_cap("full", 1_024, org_ceiling=5_000_000) == 1_024

    def test_org_ceiling_below_tail_cap_clamps_tail_too(self) -> None:
        # A ceiling below the legacy 512KB bound clamps even tail mode.
        assert _resolve_stdout_cap("tail", None, org_ceiling=100_000) == 100_000

    def test_org_ceiling_applies_to_full_default(self) -> None:
        # The 5MB full-mode default is clamped when the ceiling is lower.
        assert _resolve_stdout_cap("full", None, org_ceiling=1_000_000) == 1_000_000


class _AsyncCtx:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def __aenter__(self) -> Any:
        return self._value

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeDbSession:
    def begin(self) -> _AsyncCtx:
        return _AsyncCtx(None)


def _fake_session_factory() -> _AsyncCtx:
    return _AsyncCtx(_FakeDbSession())


class TestReadOrgStdoutRetentionCeiling:
    """FAR-811: the org ceiling is read from system_config via the same
    read_system_config accessor product analytics uses, and is fail-open."""

    async def test_reads_ceiling_from_system_config(self) -> None:
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(return_value=5_000_000),
        ) as read:
            ceiling = await _read_org_stdout_retention_ceiling(_fake_session_factory)
        read.assert_awaited_once()
        session_arg, key_arg = read.await_args.args
        assert isinstance(session_arg, _FakeDbSession)
        assert key_arg == "sandbox_stdout_retention_max_bytes"
        assert ceiling == 5_000_000

    async def test_unset_key_returns_none(self) -> None:
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(return_value=None),
        ):
            assert await _read_org_stdout_retention_ceiling(_fake_session_factory) is None

    async def test_no_session_factory_returns_none(self) -> None:
        assert await _read_org_stdout_retention_ceiling(None) is None

    async def test_malformed_value_coerces_to_none(self) -> None:
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(return_value="lots"),
        ):
            assert await _read_org_stdout_retention_ceiling(_fake_session_factory) is None

    async def test_read_failure_is_fail_open(self) -> None:
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ):
            assert await _read_org_stdout_retention_ceiling(_fake_session_factory) is None

    async def test_non_scalar_read_is_ignored(self) -> None:
        """Unit-test ``_FakeSession`` fakes return a bare MagicMock for un-routed
        ``system_config`` reads; `float(MagicMock()) == 1.0`, so a non-scalar
        read MUST reject to None or it would clamp real nodes to 1 byte."""
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(return_value=MagicMock()),
        ):
            assert await _read_org_stdout_retention_ceiling(_fake_session_factory) is None

    async def test_bool_and_float_values_reject(self) -> None:
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(return_value=True),
        ):
            assert await _read_org_stdout_retention_ceiling(_fake_session_factory) is None
        with patch(
            "modulo.core.cost_controller.system_config.read_system_config",
            new=AsyncMock(return_value=5_500_000.5),
        ):
            assert await _read_org_stdout_retention_ceiling(_fake_session_factory) is None


class TestStdoutTruncatedEnvelopeKey:
    """FAR-792: stdout_truncated is an OPT-IN EXTRA KEY — present only when
    True, absent when False, so existing envelope shapes are unchanged."""

    def _output(self, **overrides) -> nr._SandboxNodeOutput:
        return nr._SandboxNodeOutput(
            status="completed",
            summary="did the thing",
            exit_code=0,
            wall_clock_time_ms=1200,
            cost_estimate_usd=0.01,
            **overrides,
        )

    def test_key_present_when_truncated(self) -> None:
        envelope = _build_sandbox_node_envelope(
            node_id="n1",
            output=self._output(stdout_truncated=True, stdout_length=600_000),
        )
        for view in (envelope["artifacts"][0]["output"], envelope["output"]):
            assert view["stdout_truncated"] is True
            assert view["stdout_length"] == 600_000

    def test_key_absent_when_not_truncated(self) -> None:
        envelope = _build_sandbox_node_envelope(node_id="n1", output=self._output())
        for view in (envelope["artifacts"][0]["output"], envelope["output"]):
            assert "stdout_truncated" not in view

    def test_key_absent_when_explicitly_false(self) -> None:
        envelope = _build_sandbox_node_envelope(
            node_id="n1",
            output=self._output(stdout_truncated=False),
        )
        assert "stdout_truncated" not in envelope["output"]

    def test_stdout_artifact_pointer_is_opt_in_extra_key(self) -> None:
        pointer = {
            "rel_path": "org/run/node/key.stdout.zst",
            "size_bytes": 600_000,
            "sha256": "d1b2c3",
            "stream": "stdout",
            "compression": "zstd",
            "truncated": False,
            "redacted": True,
        }
        envelope = _build_sandbox_node_envelope(
            node_id="n1",
            output=self._output(stdout_truncated=True, stdout_length=600_000, stdout_artifact=pointer),
        )
        for view in (envelope["artifacts"][0]["output"], envelope["output"]):
            assert view["stdout_artifact"]["truncated"] is False
            assert view["stdout_artifact"]["redacted"] is True
            assert view["stdout_artifact"]["size_bytes"] == 600_000
            assert view["stdout_artifact"]["sha256"] == "d1b2c3"
            assert view["stdout_artifact"]["rel_path"].endswith(".zst")

    def test_stdout_artifact_absent_when_inline(self) -> None:
        envelope = _build_sandbox_node_envelope(node_id="n1", output=self._output())
        for view in (envelope["artifacts"][0]["output"], envelope["output"]):
            assert "stdout_artifact" not in view


# ---------------------------------------------------------------------------
# FAR-792: per-node stdout retention must be reachable through REAL config paths
# (API PipelineGraphNode + CLI ApplyGraphNode), not just a hand-built node_def
# dict in tests. These prove the declared fields survive save + round-trip into
# _build_sandbox_node_config (the reviewer's prove-the-fix gap).
# ---------------------------------------------------------------------------


def _sandbox_node_kwargs(**overrides: Any) -> dict[str, Any]:
    """Minimal valid sandbox_agent PipelineGraphNode kwargs."""
    kwargs: dict[str, Any] = {
        "id": uuid.uuid4(),
        "node_type": "sandbox_agent",
        "position": {"x": 0.0, "y": 0.0},
        "template_id": "opencode",
        "agent_commands": ["echo hi"],
        "agent_prompt": "Do the thing",
    }
    kwargs.update(overrides)
    return kwargs


def test_api_node_persists_stdout_retention_fields():
    """The API model keeps stdout_retention_mode / stdout_max_bytes on save so
    the runtime can actually read them (no silent drop)."""
    node = PipelineGraphNode(**_sandbox_node_kwargs(stdout_retention_mode="full", stdout_max_bytes=2048))
    dumped = node.model_dump(mode="json")
    assert dumped["stdout_retention_mode"] == "full"
    assert dumped["stdout_max_bytes"] == 2048


def test_api_node_rejects_stdout_retention_off_sandbox():
    """stdout retention is sandbox_agent-only — a declared value on another node
    type would be a silent no-op, so it is rejected at save time.

    A valid agent node (with agent_id) is used so the stdout_retention gate is
    actually reached — an agent node without an agent_id is rejected earlier, by
    the agent-only validation, which would mask this check.
    """
    with pytest.raises(ValueError, match="sandbox_agent"):
        PipelineGraphNode(
            **_sandbox_node_kwargs(node_type="agent", agent_id=uuid.uuid4(), stdout_retention_mode="full")
        )
    with pytest.raises(ValueError, match="sandbox_agent"):
        PipelineGraphNode(**_sandbox_node_kwargs(node_type="agent", agent_id=uuid.uuid4(), stdout_max_bytes=2048))


def test_api_node_rejects_bad_stdout_max_bytes():
    """The positive-integer gate rejects bools / negatives / non-integers."""
    for bad in (0, -1, 1.5, "abc", True, False):
        with pytest.raises(ValueError, match="positive integer"):
            PipelineGraphNode(**_sandbox_node_kwargs(stdout_max_bytes=bad))


def test_apply_node_accepts_and_round_trips_stdout_retention():
    """The CLI mirror accepts the fields (extra='forbid' must NOT reject them)
    and round-trips them into the API payload the executor normalises."""
    node = ApplyGraphNode(**_sandbox_node_kwargs(stdout_retention_mode="full", stdout_max_bytes=2048))
    assert node.stdout_retention_mode == "full"
    assert node.stdout_max_bytes == 2048
    payload = node.api_node_payload()
    assert payload["stdout_retention_mode"] == "full"
    assert payload["stdout_max_bytes"] == 2048


def test_apply_node_rejects_bad_stdout_max_bytes():
    """The CLI mirror shares the API's positive-integer gate."""
    for bad in (0, -1, 1.5, "abc", True, False):
        with pytest.raises(ValueError, match="positive integer"):
            ApplyGraphNode(**_sandbox_node_kwargs(stdout_max_bytes=bad))


def test_saved_graph_round_trips_into_sandbox_node_config():
    """A graph saved via the API model (mode='full', 2048 bytes) carries the
    values through model_dump into _build_sandbox_node_config, which the runtime
    uses to set the retention cap."""
    node = PipelineGraphNode(**_sandbox_node_kwargs(stdout_retention_mode="full", stdout_max_bytes=2048))
    config = nr._build_sandbox_node_config(
        node.model_dump(mode="json"),
        session_factory=None,
        single_sandbox_node=True,
    )
    assert config.stdout_retention_mode == "full"
    assert config.stdout_max_bytes == 2048


def test_saved_graph_defaults_to_tail():
    """A graph saved without the fields falls back to the legacy 'tail' mode."""
    node = PipelineGraphNode(**_sandbox_node_kwargs())
    config = nr._build_sandbox_node_config(
        node.model_dump(mode="json"),
        session_factory=None,
        single_sandbox_node=True,
    )
    assert config.stdout_retention_mode == "tail"
    assert config.stdout_max_bytes is None


# ---------------------------------------------------------------------------
# _persist_full_stdout_artifact (FAR-811)
# ---------------------------------------------------------------------------


class TestPersistFullStdoutArtifact:
    """Coverage for the over-cap redacted-stdout artifact writer.

    The success path is exercised end-to-end via the sandbox agent in
    test_node_runner_sandbox; these unit tests pin the branch logic (opt-out
    guards, store failure, empty pointer) that the happy-path test never hits.
    """

    def _kwargs(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "org_id": "org-1",
            "run_id": "run-1",
            "node_id": "node-1",
            "attempt_key": "attempt-1",
            "node_cap": 2048,
            "redacted_stdout": "x" * 4096,
        }
        base.update(overrides)
        return base

    def test_success_writes_artifact_and_returns_pointer(self, tmp_path: Any) -> None:
        """Non-empty redacted stdout with a real local store is persisted in
        full and surfaced as an envelope pointer (truncated: False, redacted: True)."""
        import hashlib

        store = LocalArtifactStore(tmp_path)
        with patch("modulo.core.artifacts.store.get_store", return_value=store):
            pointer = _persist_full_stdout_artifact(**self._kwargs())
        assert pointer is not None
        assert pointer["truncated"] is False
        assert pointer["redacted"] is True
        assert pointer["stream"] == "stdout"
        assert pointer["compression"] == "zstd"
        assert pointer["size_bytes"] == 4096
        assert pointer["sha256"] == hashlib.sha256(b"x" * 4096).hexdigest()
        assert pointer["rel_path"].endswith(".zst")
        assert store.read_bytes(pointer) == b"x" * 4096

    def test_opt_out_when_attempt_key_missing(self) -> None:
        """No attempt key means nothing to key the overflow artifact on — the
        function bails before touching the store (backwards-compatible no-op)."""
        store = MagicMock()
        with patch("modulo.core.artifacts.store.get_store", return_value=store):
            assert _persist_full_stdout_artifact(**self._kwargs(attempt_key=None)) is None
        store.append.assert_not_called()

    def test_opt_out_when_redacted_stdout_empty(self) -> None:
        """Empty redacted transcript has nothing to persist — bail before the
        store round-trip (the store's append is itself a no-op on empty input,
        but the guard keeps the contract explicit and cheap)."""
        store = MagicMock()
        with patch("modulo.core.artifacts.store.get_store", return_value=store):
            assert _persist_full_stdout_artifact(**self._kwargs(redacted_stdout="")) is None
        store.append.assert_not_called()

    def test_store_failure_is_non_fatal(self, caplog: Any) -> None:
        """A store write/append/finalize exception must NOT propagate — the
        caller keeps today's inline (truncated) behaviour and a warning is logged."""
        boom = RuntimeError("store unavailable")
        store = MagicMock()
        store.append.side_effect = boom
        with (
            patch("modulo.core.artifacts.store.get_store", return_value=store),
            caplog.at_level("WARNING"),
        ):
            assert _persist_full_stdout_artifact(**self._kwargs()) is None
        assert any("stdout_artifact_write_failed" in r.message for r in caplog.records)

    def test_none_pointer_is_non_fatal(self) -> None:
        """If the store finalises to no artifact (e.g. empty raw from a skipped
        append), the function returns None rather than building a pointer from a
        None ArtifactPointer."""
        store = MagicMock()
        store.append.return_value = None
        store.finalize.return_value = None
        with patch("modulo.core.artifacts.store.get_store", return_value=store):
            assert _persist_full_stdout_artifact(**self._kwargs()) is None

    def test_cancelled_error_propagates(self) -> None:
        """A CancelledError is a control signal, not a store fault — it must
        re-raise so the runtime can honour cancellation instead of swallowing it."""
        with (
            patch(
                "modulo.core.artifacts.store.get_store",
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            _persist_full_stdout_artifact(**self._kwargs())
