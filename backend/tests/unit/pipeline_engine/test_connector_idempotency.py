"""Unit tests for the FAR-458 connector-write UNKNOWN-recovery idempotency dedup.

Covers the connector-specific wiring (the connector node's write-boundary
decision point) and the threading of index/payload through the marker and
suppression call sites. Unlike ``test_idempotency.py`` (which proves the pure
:func:`read_before_write_suppression` / :func:`node_idempotency_key` contract),
these tests exercise the connector helpers that CONSUME those primitives:

  - ``_connector_write_payload_hash`` — stable content hash for key derivation
  - ``_connector_marker_attempt_key`` — stable per-node marker key
  - ``_connector_write_gate`` — the read-before-write gate that suppresses a
    duplicate upstream write ONLY when a marker carries ``delivery_done`` for
    the SAME derived key

The gate is unit-tested by monkeypatching the DB read
(``_read_connector_idempotency_gate_state``) and the killswitch setting, so no
DB is required. Fail-open behaviour (missing run id / session factory / key,
killswitch off) is asserted directly since that is the safety contract that
must never block a connector write.
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

from modulo.core.pipeline_engine.idempotency import node_idempotency_key, read_before_write_suppression
from modulo.core.pipeline_engine.node_runner import (
    _connector_marker_attempt_key,
    _connector_write_gate,
    _connector_write_payload_hash,
)

# A stable, well-formed persisted run identity (FAR-438 run-record key).
_PERSISTED_KEY = "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f:9"
_NODE_ID = "connector-node-a"


def _payload_hash(value: dict) -> str:
    return _connector_write_payload_hash(value)


async def _run_gate(
    *,
    markers: object,
    persisted_key: str | None,
    data: dict,
    session_factory: object | None,
    run_id: str = "run-123",
    gate_enabled: bool = True,
) -> object:
    """Invoke ``_connector_write_gate`` with controlled gate state.

    The DB read is monkeypatched to return ``(markers, persisted_key)`` and the
    killswitch setting is fixed to ``gate_enabled``, so the gate can be tested
    without a real DB or settings object.
    """
    with (
        patch(
            "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
            new=AsyncMock(return_value=(markers, persisted_key)),
        ),
        patch(
            "modulo.settings.get_settings",
            return_value=types.SimpleNamespace(modulo_idempotency_gate_enabled=gate_enabled),
        ),
    ):
        return await _connector_write_gate(
            session_factory,
            run_id=run_id,
            org_id_raw="org-1",
            node_id=_NODE_ID,
            data=data,
        )


# ── payload hash: stable content for key derivation ──────────────────────────


class TestConnectorPayloadHash:
    def test_same_data_same_hash(self) -> None:
        assert _payload_hash({"name": "n1", "id": 1}) == _payload_hash({"name": "n1", "id": 1})

    def test_changed_data_different_hash(self) -> None:
        assert _payload_hash({"name": "n1", "id": 1}) != _payload_hash({"name": "n2", "id": 1})

    def test_key_order_normalised(self) -> None:
        assert _payload_hash({"a": 1, "b": 2}) == _payload_hash({"b": 2, "a": 1})

    def test_non_json_value_coerced_without_raising(self) -> None:
        assert _payload_hash({"created": type("D", (), {})()}) != ""


# ── marker attempt key: stable per node ──────────────────────────────────────


class TestConnectorMarkerAttemptKey:
    def test_stable_for_same_run_and_node(self) -> None:
        assert _connector_marker_attempt_key("run-1", "node-a") == _connector_marker_attempt_key("run-1", "node-a")

    def test_differs_across_nodes(self) -> None:
        assert _connector_marker_attempt_key("run-1", "node-a") != _connector_marker_attempt_key("run-1", "node-b")

    def test_differs_across_runs(self) -> None:
        assert _connector_marker_attempt_key("run-1", "node-a") != _connector_marker_attempt_key("run-2", "node-a")


# ── read-before-write gate ───────────────────────────────────────────────────


class TestConnectorWriteGate:
    async def test_suppresses_rewrite_with_same_persisted_key(self) -> None:
        """A connector write UNKNOWN re-run reusing the SAME persisted key, where
        the prior write genuinely delivered (``delivery_done`` marker on the
        matching key), must suppress the duplicate upstream write."""
        applied = node_idempotency_key(_PERSISTED_KEY, _NODE_ID, index=None, payload=_payload_hash({"name": "n1"}))
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
        )
        # A skipped envelope (write suppressed) is returned.
        assert isinstance(result, dict)
        assert result["artifacts"][0]["status"] == "skipped"
        assert result["artifacts"][0]["output"]["output_json"]["delivery_done"] is True

    async def test_first_time_connector_write_not_suppressed(self) -> None:
        """A first-time connector write (no prior marker) is NEVER suppressed."""
        result = await _run_gate(
            markers={},
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
        )
        assert result is None

    async def test_changed_payload_connector_rerun_not_suppressed(self) -> None:
        """A genuinely-edited content-edit re-run derives a DIFFERENT key, so it
        is NOT suppressed (the edit is never silently dropped)."""
        applied = node_idempotency_key(_PERSISTED_KEY, _NODE_ID, index=None, payload=_payload_hash({"name": "v1"}))
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "v2"},
            session_factory=lambda: None,
        )
        assert result is None

    async def test_fanout_items_derive_distinct_keys(self) -> None:
        """Two fan-out items (index 0 vs 1) for the SAME connector node derive
        DIFFERENT keys: item B's delivered marker never suppresses item A."""
        item_a_key = node_idempotency_key(_PERSISTED_KEY, _NODE_ID, index=0, payload="item-a")
        item_b_key = node_idempotency_key(_PERSISTED_KEY, _NODE_ID, index=1, payload="item-b")
        assert item_a_key != item_b_key
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": item_b_key}}
        assert (
            read_before_write_suppression(markers, run_ref=_PERSISTED_KEY, node_ref=_NODE_ID, index=0, payload="item-a")
            is False
        )
        assert (
            read_before_write_suppression(markers, run_ref=_PERSISTED_KEY, node_ref=_NODE_ID, index=1, payload="item-b")
            is True
        )

    async def test_fails_open_when_no_session_factory(self) -> None:
        """No session factory => the gate never suppresses (write proceeds)."""
        result = await _run_gate(
            markers={"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": "x"}},
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=None,
        )
        assert result is None

    async def test_fails_open_when_no_run_id(self) -> None:
        """No run id => the gate never suppresses (write proceeds)."""
        with patch(
            "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
            new=AsyncMock(return_value=({}, _PERSISTED_KEY)),
        ):
            result = await _connector_write_gate(
                lambda: None, run_id="", org_id_raw="org-1", node_id=_NODE_ID, data={"name": "n1"}
            )
        assert result is None

    async def test_fails_open_when_killswitch_disabled(self) -> None:
        """The killswitch ``modulo_idempotency_gate_enabled=False`` disables the
        gate so a connector write is never suppressed."""
        applied = node_idempotency_key(_PERSISTED_KEY, _NODE_ID, index=None, payload=_payload_hash({"name": "n1"}))
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            gate_enabled=False,
        )
        assert result is None

    async def test_unmatched_marker_key_not_suppressed(self) -> None:
        """A marker keyed for a DIFFERENT node/cardinality never suppresses."""
        other = node_idempotency_key(
            _PERSISTED_KEY, "connector-node-b", index=None, payload=_payload_hash({"name": "n1"})
        )
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": other}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
        )
        assert result is None
