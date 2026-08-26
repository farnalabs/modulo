"""Unit tests for the FAR-458 connector-write UNKNOWN-recovery idempotency dedup.

Covers the connector-specific wiring (the connector node's write-boundary
decision point) and the threading of index/payload through the marker and
suppression call sites. Unlike ``test_idempotency.py`` (which proves the pure
:func:`read_before_write_suppression` / :func:`node_idempotency_key` contract),
these tests exercise the connector helpers that CONSUME those primitives:

  - ``_connector_write_payload_hash`` — stable full-write-identity hash for key
    derivation (resource + provider_ref + data)
  - ``_connector_marker_attempt_key`` — stable per-node marker key
  - ``_connector_write_gate`` — the read-before-write gate that suppresses a
    duplicate upstream write ONLY when a marker carries ``delivery_done`` for
    the SAME derived key
  - ``_stamp_connector_write_delivered`` — the ``delivery_done`` marker stamp
    that PROMOTES the newest delivered key on a content-edit re-run

The gate is unit-tested by monkeypatching the DB read
(``_read_connector_idempotency_gate_state``) and the killswitch setting, so no
DB is required. The newest-key promotion (MAJOR 1) is tested against a fake DB
session harness that captures the persisted marker, since the promotion happens
inside ``_write_raw_output_marker``. Fail-open behaviour (missing run id /
session factory / key, killswitch off) is asserted directly since that is the
safety contract that must never block a connector write.

NOTE (fan-out, MAJOR-2 related): the fan-out-distinct-keys test below
(``test_fanout_items_derive_distinct_keys``) tests the LIBRARY PRIMITIVE
(``node_idempotency_key`` / ``read_before_write_suppression`` with an explicit
``index``), NOT the connector gate — the connector node performs ONE logical
write per invocation and therefore threads ``index=None``. Per-item fan-out
idempotency remains a capability of the primitive (see the SCOPE note in
``idempotency.py``); the test documents that the primitive is correct even
though the node boundary does not yet thread per-item keys.
"""

from __future__ import annotations

import types
from typing import Self
from unittest.mock import AsyncMock, patch

from modulo.core.pipeline_engine.idempotency import node_idempotency_key, read_before_write_suppression
from modulo.core.pipeline_engine.node_runner import (
    _connector_marker_attempt_key,
    _connector_write_gate,
    _connector_write_payload_hash,
    _stamp_connector_write_delivered,
)

# A stable, well-formed persisted run identity (FAR-438 run-record key).
_PERSISTED_KEY = "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f:9"
_NODE_ID = "connector-node-a"
# A real UUID (the connector persist parses org_id_raw via uuid.UUID).
_ORG_UUID = "550e8400-1b24-4f1a-91d3-1f2b3c4d5e6f"
_DEFAULT_RESOURCE = "command"


def _payload_hash(
    data: dict,
    *,
    resource: str = _DEFAULT_RESOURCE,
    filters: dict | None = None,
) -> str:
    return _connector_write_payload_hash(resource=resource, filters=filters or {}, data=data)


def _applied_key(data: dict, *, resource: str = _DEFAULT_RESOURCE, filters: dict | None = None) -> str:
    """The per-node key the gate derives for a write with this payload hash."""
    return node_idempotency_key(
        _PERSISTED_KEY, _NODE_ID, index=None, payload=_payload_hash(data, resource=resource, filters=filters)
    )


async def _run_gate(
    *,
    markers: object,
    persisted_key: str | None,
    data: dict,
    session_factory: object | None,
    run_id: str = "run-123",
    gate_enabled: bool = True,
    resource: str = _DEFAULT_RESOURCE,
    filters: dict | None = None,
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
            resource=resource,
            filters=filters or {},
            data=data,
        )


# ── payload hash: stable full-write identity for key derivation ──────────────


class TestConnectorPayloadHash:
    def test_same_data_same_hash(self) -> None:
        assert _payload_hash({"name": "n1", "id": 1}) == _payload_hash({"name": "n1", "id": 1})

    def test_changed_data_different_hash(self) -> None:
        assert _payload_hash({"name": "n1", "id": 1}) != _payload_hash({"name": "n2", "id": 1})

    def test_key_order_normalised(self) -> None:
        assert _payload_hash({"a": 1, "b": 2}) == _payload_hash({"b": 2, "a": 1})

    def test_non_json_value_coerced_without_raising(self) -> None:
        assert _payload_hash({"created": type("D", (), {})()})

    def test_changed_resource_different_hash(self) -> None:
        # MAJOR 2: same data, different write target (resource) -> different key.
        assert _payload_hash({"name": "n1"}, resource="command") != _payload_hash({"name": "n1"}, resource="file")

    def test_changed_provider_ref_different_hash(self) -> None:
        # MAJOR 2: same data, different write target (provider_ref, shell) -> different key.
        assert _payload_hash({"name": "n1"}, filters={"provider_ref": "/a"}) != _payload_hash(
            {"name": "n1"}, filters={"provider_ref": "/b"}
        )


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
        applied = _applied_key({"name": "n1"})
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
        # The driver-readable reason the run's suppression relies on
        # (``_node_output_has_idempotency_gate`` checks truthiness, but the tag
        # must say the connector gate, not the sandbox email_sent default).
        assert result["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "connector_write_suppressed"

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
        applied = _applied_key({"name": "v1"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "v2"},
            session_factory=lambda: None,
        )
        assert result is None

    async def test_changed_write_target_not_suppressed(self) -> None:
        """MAJOR 2: changing the write TARGET (resource) with byte-identical
        data derives a DIFFERENT key, so the new-target write is not wrongly
        suppressed against a marker for the old target."""
        applied = _applied_key({"name": "n1"}, resource="command")
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            resource="file",
            session_factory=lambda: None,
        )
        assert result is None

    async def test_changed_provider_ref_not_suppressed(self) -> None:
        """MAJOR 2: changing the write target's ``provider_ref`` (shell
        connector) with byte-identical data derives a DIFFERENT key, so the new
        target write is not suppressed against a marker for the old target."""
        applied = _applied_key({"name": "n1"}, filters={"provider_ref": "/a"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            filters={"provider_ref": "/b"},
            session_factory=lambda: None,
        )
        assert result is None

    def test_fanout_items_derive_distinct_keys(self) -> None:
        """Two fan-out items (index 0 vs 1) for the SAME connector node derive
        DIFFERENT keys: item B's delivered marker never suppresses item A.

        NOTE (LIBRARY-ONLY): this exercises the PRIMITIVE's ``index`` threading
        (``node_idempotency_key`` / ``read_before_write_suppression``). The
        connector NODE gate passes ``index=None`` (one logical write per
        invocation), so it does NOT test connector-gate fan-out — per-item
        idempotency remains a primitive capability, not yet wired through the
        node boundary.
        """
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
        """No run id => the gate never suppresses (write proceeds). The DB read
        is already moot (the gate returns before any read), so no patch is
        installed."""
        result = await _connector_write_gate(
            lambda: None,
            run_id="",
            org_id_raw="org-1",
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"name": "n1"},
        )
        assert result is None

    async def test_fails_open_when_killswitch_disabled(self) -> None:
        """The killswitch ``modulo_idempotency_gate_enabled=False`` disables the
        gate so a connector write is never suppressed."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            gate_enabled=False,
        )
        assert result is None

    async def test_fails_open_when_killswitch_missing(self) -> None:
        """MAJOR 5: the killswitch FAIL-OPEN default is ``False`` (explicit
        opt-in). A settings object WITHOUT the attribute must NOT enable the
        gate — the write proceeds (no suppression)."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        with (
            patch(
                "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
                new=AsyncMock(return_value=(markers, _PERSISTED_KEY)),
            ),
            patch("modulo.settings.get_settings", return_value=types.SimpleNamespace()),
        ):
            result = await _connector_write_gate(
                lambda: None,
                run_id="run-123",
                org_id_raw="org-1",
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "n1"},
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


# ── newest-key promotion (MAJOR 1) ───────────────────────────────────────────


class _FakeConnectorRun:
    """Stand-in for ``modulo.db.models.run.Run`` used by ``_write_raw_output_marker``."""

    def __init__(self, markers: dict, idempotency_key: str | None) -> None:
        self.raw_output_markers = markers
        self.idempotency_key = idempotency_key


class _FakeConnectorResult:
    def __init__(self, run: _FakeConnectorRun) -> None:
        self._run = run

    def scalar_one_or_none(self) -> _FakeConnectorRun:
        return self._run


class _FakeConnectorSession:
    """A session that surfaces the run row for the write marker persist, no DB."""

    def __init__(self, run: _FakeConnectorRun) -> None:
        self._run = run

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    async def execute(self, statement: object, *args: object, **kwargs: object) -> _FakeConnectorResult:
        return _FakeConnectorResult(self._run)

    async def flush(self) -> None:
        return None


class TestConnectorNewestKeyPromotion:
    async def test_promotes_newest_key_on_content_edit(self) -> None:
        """MAJOR 1: a content-edit re-run executes the stamp side and PROMOTES
        the newest delivered key. After delivering P1 then editing to P2 (same
        slot), a subsequent gate for P2 SUPPRESSES while a gate for the
        superseded P1 does NOT."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        # Run 1 delivers P1 (content v1) -> marker key K1.
        # Run 2 (content edit) delivers P2 (content v2) -> marker key K2 promoted.
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _stamp_connector_write_delivered(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v1"},
            )
            await _stamp_connector_write_delivered(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v2"},
            )

        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        assert slot in fake_run.raw_output_markers, "the delivery marker must have been persisted"
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["delivery_done"] is True
        # The NEWEST key (P2/v2) is promoted, NOT pinned to the superseded P1/v1.
        assert persisted["idempotency_key"] == _applied_key({"name": "v2"})
        assert persisted["idempotency_key"] != _applied_key({"name": "v1"})

        # Gate for P2 -> SUPPRESSED (the latest delivery is dedupable).
        with (
            patch(
                "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
                new=AsyncMock(return_value=(fake_run.raw_output_markers, _PERSISTED_KEY)),
            ),
            patch(
                "modulo.settings.get_settings",
                return_value=types.SimpleNamespace(modulo_idempotency_gate_enabled=True),
            ),
        ):
            gate_v2 = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v2"},
            )
            assert gate_v2 is not None
            assert gate_v2["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "connector_write_suppressed"
            # Gate for the superseded P1/v1 -> NOT suppressed (no stale double-submit).
            gate_v1 = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v1"},
            )
            assert gate_v1 is None
