"""FAR-1737: MCP ``get_run_status`` detail parity — ``input_payload`` + ``deadline_triage``.

REST ``GET /api/v1/runs/{run_id}`` carries the masked run ``input_payload``
(the original run input, so a prompt is recoverable) and ``deadline_triage``
(the per-attempt sandbox envelopes — attempt_key, no_output_reason,
sandbox_log_tail, raw_output_tail, stdout_length — that explain why a node
produced no output). The MCP ``get_run_status`` detail read returned
``nodes: []`` for a failed sandbox run and neither field, so a run's original
prompt was unrecoverable over MCP.

These tests pin the parity: both fields appear on the DETAIL read only,
``input_payload`` is masked with the SAME masker REST uses, and
``deadline_triage`` is projected by the SAME helper from the markers already
reassembled for the detail read.

No DB and no network: rows are in-memory ORM instances; the session/blob
reads are patched at the MCP module boundary.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

from modulo.api.mcp_server import (
    _MCP_INPUT_PAYLOAD_MAX_DEPTH,
    _MCP_INPUT_PAYLOAD_MAX_STRING_CHARS,
    _MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS,
    _get_run_status_impl,
    _load_run_for_status,
    _run_status_detail,
)
from modulo.core.pipeline_engine.hitl_context import TRUNCATION_MARKER
from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK
from modulo.db.crud.run_node_outputs import RunBlobs
from modulo.db.models.run import Run
from tests.unit.mcp.helpers import ORG_ID, AuthContext, make_session_context

_RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000009")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

# A value-pattern-matching secret under a NON-sensitive key: it is only redacted
# because REST's ``_mask_output_value`` also scans every string for secret
# VALUE patterns (FAR-392), which is exactly the behaviour MCP must reuse.
_RAW_SECRET_IN_PROMPT = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"

_TRIAGE_MARKERS: dict[str, Any] = {
    # Deliberately unsorted insertion order: the projection sorts by attempt_key.
    f"run:{_RUN_ID}:node:zeta:deadline": {
        "deadline_triage": True,
        "node_id": "zeta",
        "no_output_reason": "stalled",
        "sandbox_log_tail": "tail-zeta",
        "raw_output": "",
        "stdout_length": 0,
        "stderr_length": 12,
        "delivery_done": False,
    },
    f"run:{_RUN_ID}:node:alpha:deadline": {
        "deadline_triage": True,
        "node_id": "alpha",
        "no_output_reason": "no_output",
        "sandbox_log_tail": "tail-alpha",
        "raw_output": "raw-alpha",
        "stdout_length": 4,
        "stderr_length": 0,
        "delivery_done": True,
    },
    # NOT deadline-triage signed (delivery-done marker) — must not surface.
    f"run:{_RUN_ID}:node:alpha:done": {
        "delivery_done": True,
    },
}


def _run_row(**overrides: Any) -> Run:
    """A real ``Run`` row (no DB) carrying an input payload with both a
    sensitive-KEY secret and a secret VALUE under a plain key."""
    row = Run(
        id=_RUN_ID,
        pipeline_id=_PIPELINE_ID,
        status="failed",
        trigger_type="manual",
        run_number=7,
        created_at=_NOW,
        input_payload={
            "prompt": f"rebuild the docs — token {_RAW_SECRET_IN_PROMPT}",
            "api_key": "sk-live-abcdef123456",
        },
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _blobs(markers: dict[str, Any] | None = None) -> RunBlobs:
    return RunBlobs(outputs={}, telemetry={}, markers=markers)


class _McpCtx(AuthContext):
    """Auth context vars so ``_get_run_status_impl`` reaches the load path."""


# ---------------------------------------------------------------------------
# _run_status_detail — the detail body builder
# ---------------------------------------------------------------------------


class TestRunStatusDetailParityFields:
    def test_input_payload_is_masked_exactly_as_rest_masks_it(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        run = _run_row()
        result = _run_status_detail(run, _blobs(markers=None))

        assert "input_payload" in result
        assert result["input_payload"] == _mask_output_value(run.input_payload)

    def test_sensitive_key_and_secret_value_are_both_redacted(self) -> None:
        run = _run_row()
        masked = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        assert masked["api_key"] == SENSITIVE_VALUE_MASK
        assert _RAW_SECRET_IN_PROMPT not in masked["prompt"]

    def test_empty_or_absent_input_payload_reads_none(self) -> None:
        """REST parity: an empty / missing input column serialises as None."""
        empty = _run_status_detail(_run_row(input_payload={}), _blobs(markers=None))
        absent = _run_status_detail(_run_row(input_payload=None), _blobs(markers=None))

        assert empty["input_payload"] is None
        assert absent["input_payload"] is None

    def test_deadline_triage_projects_signed_markers_only(self) -> None:
        result = _run_status_detail(_run_row(), _blobs(markers=_TRIAGE_MARKERS))

        triage = result["deadline_triage"]
        assert len(triage) == 2
        # Sorted by attempt_key — alpha before zeta regardless of dict order.
        assert [entry["attempt_key"] for entry in triage] == [
            f"run:{_RUN_ID}:node:alpha:deadline",
            f"run:{_RUN_ID}:node:zeta:deadline",
        ]

    def test_deadline_triage_entry_shape_matches_rest(self) -> None:
        result = _run_status_detail(_run_row(), _blobs(markers=_TRIAGE_MARKERS))

        alpha = result["deadline_triage"][0]
        assert alpha["node_id"] == "alpha"
        assert alpha["no_output_reason"] == "no_output"
        assert alpha["sandbox_log_tail"] == "tail-alpha"
        assert alpha["raw_output_tail"] == "raw-alpha"
        assert alpha["stdout_length"] == 4
        assert alpha["stderr_length"] == 0
        assert alpha["delivery_done"] is True

    def test_deadline_triage_empty_list_when_no_markers(self) -> None:
        result = _run_status_detail(_run_row(), _blobs(markers=None))

        # REST parity: a marker-less run projects [] (never None) on the detail
        # body — None is reserved for the REST path's DB-failure degrade.
        assert isinstance(result["deadline_triage"], list)
        assert not result["deadline_triage"]

    def test_detail_body_still_carries_the_node_breakdown(self) -> None:
        """The parity fields are additive — the existing detail body is intact."""
        run = _run_row(node_token_usage={"n1": {"input_tokens": 1}})
        blobs = RunBlobs(outputs={"n1": {"result": "ok"}}, telemetry={}, markers=None)

        result = _run_status_detail(run, blobs)

        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["node_id"] == "n1"


# ---------------------------------------------------------------------------
# FAR-1575 — the detail input_payload is size-bounded (after masking)
# ---------------------------------------------------------------------------


class TestRunStatusDetailPayloadBounds:
    def test_normal_small_payload_is_unchanged(self) -> None:
        from modulo.api.routes.runs import _mask_output_value

        run = _run_row()

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        assert bounded == _mask_output_value(run.input_payload)
        assert TRUNCATION_MARKER not in bounded["prompt"]

    def test_long_string_is_truncated_with_marker(self) -> None:
        long_prompt = "p" * (_MCP_INPUT_PAYLOAD_MAX_STRING_CHARS + 5_000)
        run = _run_row(input_payload={"prompt": long_prompt})

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        assert len(bounded["prompt"]) <= _MCP_INPUT_PAYLOAD_MAX_STRING_CHARS
        assert bounded["prompt"].endswith(TRUNCATION_MARKER)

    def test_huge_list_is_capped(self) -> None:
        run = _run_row(input_payload={"items": list(range(10_000))})

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        assert len(bounded["items"]) <= _MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS
        assert bounded["items"][-1] == TRUNCATION_MARKER

    def test_small_list_is_unchanged(self) -> None:
        run = _run_row(input_payload={"items": [1, "two", [3.0]]})

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        assert bounded["items"] == [1, "two", [3.0]]
        assert TRUNCATION_MARKER not in bounded["items"]

    def test_huge_dict_is_capped_with_omitted_count(self) -> None:
        run = _run_row(input_payload={f"key-{i}": i for i in range(10_000)})

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        # Retained entries plus the one marker key that reports the remainder.
        assert len(bounded) == _MCP_INPUT_PAYLOAD_MAX_TOTAL_ITEMS + 1
        assert "item(s) omitted" in bounded[TRUNCATION_MARKER]

    def test_deeply_nested_value_collapses_to_marker(self) -> None:
        payload: dict[str, Any] = {"leaf": "bottom"}
        for _ in range(_MCP_INPUT_PAYLOAD_MAX_DEPTH + 5):
            payload = {"nested": payload}
        run = _run_row(input_payload=payload)

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        node: Any = bounded
        while isinstance(node, dict) and "nested" in node:
            node = node["nested"]
        assert node == TRUNCATION_MARKER

    def test_secret_straddling_the_string_cap_is_still_masked(self) -> None:
        # The secret STARTS 10 chars before the cap and would be cut in half if
        # truncation ran before masking — masking must win.
        # A non-alphanumeric tail: an alphanumeric one would extend the greedy
        # token regex and change the masked length.
        long_prompt = "x" * (_MCP_INPUT_PAYLOAD_MAX_STRING_CHARS - 10) + _RAW_SECRET_IN_PROMPT + "!" * 1_000
        run = _run_row(input_payload={"prompt": long_prompt})

        bounded = _run_status_detail(run, _blobs(markers=None))["input_payload"]

        assert "ghp_" not in bounded["prompt"]
        assert _RAW_SECRET_IN_PROMPT not in bounded["prompt"]
        assert bounded["prompt"].endswith(TRUNCATION_MARKER)


# ---------------------------------------------------------------------------
# _get_run_status_impl — the MCP tool's actual response builder
# ---------------------------------------------------------------------------


class TestGetRunStatusImplParity(_McpCtx):
    @staticmethod
    async def _call(run: Run, blobs: RunBlobs | None, *, detail: bool) -> dict[str, Any]:
        session = AsyncMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=make_session_context(session)),
            patch("modulo.api.mcp_server._load_run_for_status", AsyncMock(return_value=run)),
            patch("modulo.api.mcp_server.read_run_blobs", AsyncMock(return_value=blobs)),
        ):
            return await _get_run_status_impl(str(run.id), detail)

    async def test_detail_true_carries_masked_input_payload(self) -> None:
        result = await self._call(_run_row(), _blobs(markers=None), detail=True)

        assert "error" not in result
        masked = result["input_payload"]
        assert masked["api_key"] == SENSITIVE_VALUE_MASK
        assert _RAW_SECRET_IN_PROMPT not in masked["prompt"]

    async def test_detail_true_carries_deadline_triage(self) -> None:
        result = await self._call(_run_row(), _blobs(markers=_TRIAGE_MARKERS), detail=True)

        triage = result["deadline_triage"]
        assert len(triage) == 2
        assert triage[0]["node_id"] == "alpha"
        assert triage[1]["node_id"] == "zeta"

    async def test_detail_true_carries_empty_triage_when_no_markers(self) -> None:
        result = await self._call(_run_row(), _blobs(markers=None), detail=True)

        assert isinstance(result["deadline_triage"], list)
        assert not result["deadline_triage"]

    async def test_detail_false_omits_both_fields(self) -> None:
        """The base read never touches the input payload or the markers."""
        result = await self._call(_run_row(), _blobs(markers=_TRIAGE_MARKERS), detail=False)

        assert "input_payload" not in result
        assert "deadline_triage" not in result
        assert "nodes" not in result

    async def test_detail_true_without_input_payload_reads_none(self) -> None:
        result = await self._call(_run_row(input_payload=None), _blobs(markers=None), detail=True)

        assert result["input_payload"] is None

    async def test_load_run_for_status_forwards_org_predicate(self) -> None:
        """The loader must carry the defence-in-depth org predicate (FAR-328).

        RLS is the production backstop; this pins that the MCP caller passes
        the org it already resolved so a cross-tenant read is impossible even
        if RLS were misconfigured (the BYPASSRLS variant has an integration
        test in tests/integration/test_run_org_scoping_bypassrls.py).
        """
        mock_get_run = AsyncMock(return_value=None)
        with patch("modulo.api.mcp_server.get_run", mock_get_run):
            loaded = await _load_run_for_status(AsyncMock(), _RUN_ID, ORG_ID)

        assert loaded is None
        mock_get_run.assert_awaited_once()
        assert mock_get_run.await_args.kwargs["organisation_id"] == ORG_ID
