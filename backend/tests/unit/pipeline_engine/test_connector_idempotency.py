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

FAR-458 adds the per-connector-per-write ``on_unknown`` mode to the gate: it
governs ONLY the AMBIGUOUS (couldn't-confirm-delivery) case — a marker carrying
the SAME derived key but WITHOUT ``delivery_done``. ``fail_open`` (default) re-fires
that write, ``fail_closed`` SUPPRESSES it, and ``off`` bypasses the gate entirely.
A CONFIRMED-delivered write (``delivery_done`` + matching key) is suppressed
in every mode except ``off``; a first-time / changed-payload write is NEVER
suppressed. See ``TestConnectorWriteGateOnUnknown``.

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

from modulo.connectors.base import DEFAULT_ON_UNKNOWN, ON_UNKNOWN_MODES
from modulo.connectors.rest import _normalise_on_unknown
from modulo.core.pipeline_engine.idempotency import node_idempotency_key, read_before_write_suppression
from modulo.core.pipeline_engine.node_runner import (
    _connector_marker_attempt_key,
    _connector_on_unknown,
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
    on_unknown: str = "fail_open",
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
            return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=gate_enabled),
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
            on_unknown=on_unknown,
        )


# ── payload hash: stable full-write identity for key derivation ──────────────


class TestConnectorPayloadHash:
    def test_same_data_same_hash(self) -> None:
        assert _payload_hash({"name": "n1", "id": 1}) == (
            '{"data": {"id": 1, "name": "n1"}, "provider_ref": null, "resource": "command"}'
        )

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
        assert _connector_marker_attempt_key("run-1", "node-a") == "run:run-1:node:node-a:connector"

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
        """MAJOR 2: the connector gate uses its OWN opt-in flag
        (``modulo_connector_write_gate_enabled``) which defaults to ``False`` in
        ``modulo.settings`` — so at runtime the gate is GENUINELY opt-in and a
        settings object WITHOUT the attribute (or with it ``False``) must NOT
        enable the gate: the write proceeds (no suppression)."""
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

    async def test_gate_active_when_optin_flag_enabled(self) -> None:
        """MAJOR 2: once the opt-in flag is explicitly enabled, a confirmed
        delivery IS suppressed (proves the gate actually engages via the new
        flag, not the FAR-228 sandbox flag)."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            gate_enabled=True,
        )
        assert isinstance(result, dict)
        assert result["artifacts"][0]["status"] == "skipped"

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


# ── per-connector ``on_unknown`` mode (FAR-458 refinement) ──────────────────


class TestConnectorWriteGateOnUnknown:
    """FAR-458 refinement: the per-connector-per-write ``on_unknown`` mode
    governs ONLY the ambiguous (couldn't-confirm-delivery) case. A
    CONFIRMED-delivered write (``delivery_done`` + matching key) is ALWAYS
    suppressed regardless of mode; a first-time / changed-payload write is NEVER
    suppressed; ``off`` bypasses the gate entirely."""

    async def test_fail_closed_suppresses_ambiguous_write(self) -> None:
        """An ambiguous prior attempt (matching key, NO ``delivery_done``) is
        SUPPRESSED under ``fail_closed`` — the write does not fire (possible
        silent miss; the operator reconciles)."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            on_unknown="fail_closed",
        )
        assert isinstance(result, dict)
        assert result["artifacts"][0]["status"] == "skipped"
        assert result["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "connector_write_fail_closed"

    async def test_fail_open_default_fires_ambiguous_write(self) -> None:
        """Under the default ``fail_open``, an ambiguous prior attempt (matching
        key, no ``delivery_done``) is NOT suppressed — the write fires (possible
        duplicate, usually recoverable)."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
        )
        assert result is None

    async def test_fail_open_explicit_fires_ambiguous_write(self) -> None:
        """Explicit ``fail_open`` also lets the ambiguous write fire."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            on_unknown="fail_open",
        )
        assert result is None

    async def test_off_never_dedupes_confirmed_delivery(self) -> None:
        """``off`` bypasses the gate entirely — even a CONFIRMED-delivered marker
        does not suppress (the write always fires, never deduped)."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            on_unknown="off",
        )
        assert result is None

    async def test_confirmed_delivery_suppressed_regardless_of_mode(self) -> None:
        """A CONFIRMED-delivered write (``delivery_done`` + matching key) is
        ALWAYS suppressed — under both ``fail_open`` and ``fail_closed`` (the
        whole point of dedup, independent of the ambiguous-case policy)."""
        applied = _applied_key({"name": "n1"})
        markers = {"attempt-0": {"_modulo_marker": True, "delivery_done": True, "idempotency_key": applied}}
        for mode in ("fail_open", "fail_closed"):
            result = await _run_gate(
                markers=markers,
                persisted_key=_PERSISTED_KEY,
                data={"name": "n1"},
                session_factory=lambda: None,
                on_unknown=mode,
            )
            assert isinstance(result, dict)
            assert result["artifacts"][0]["status"] == "skipped"
            assert result["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "connector_write_suppressed"

    async def test_first_time_write_never_suppressed_even_fail_closed(self) -> None:
        """A first-time write (no marker at all) is NEVER suppressed, even under
        ``fail_closed`` — there is no prior attempt, so nothing is ambiguous."""
        result = await _run_gate(
            markers={},
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            on_unknown="fail_closed",
        )
        assert result is None

    async def test_changed_payload_never_suppressed_even_fail_closed(self) -> None:
        """A changed-payload re-run derives a DIFFERENT key, so it is never
        ambiguous and never suppressed — even under ``fail_closed``."""
        applied = _applied_key({"name": "v1"})
        markers = {"attempt-0": {"_modulo_marker": True, "idempotency_key": applied}}
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "v2"},
            session_factory=lambda: None,
            on_unknown="fail_closed",
        )
        assert result is None


class TestOnUnknownModeSetSingleSource:
    """The ``on_unknown`` mode set and default are defined ONCE — in
    ``modulo.connectors.base`` (a stdlib-only leaf) — and both the REST
    connector's config validation and the pipeline engine's gate read import
    them. No local redefinition may drift from the shared set."""

    def test_shared_constants_hold_the_contract(self) -> None:
        assert ON_UNKNOWN_MODES == ("fail_open", "fail_closed", "off")
        assert DEFAULT_ON_UNKNOWN == "fail_open"

    def test_rest_connector_validates_against_the_shared_set(self) -> None:
        assert _normalise_on_unknown(None) == DEFAULT_ON_UNKNOWN
        for mode in ON_UNKNOWN_MODES:
            assert _normalise_on_unknown(mode.upper()) == mode
        assert _normalise_on_unknown(" off ") == "off"

    def test_engine_gate_reader_uses_the_shared_set(self) -> None:
        for mode in ON_UNKNOWN_MODES:
            connector = types.SimpleNamespace(on_unknown_for=lambda _resource, _mode=mode: _mode)
            assert _connector_on_unknown(connector, "res") == mode
        invalid = types.SimpleNamespace(on_unknown_for=lambda _resource: "bogus")
        assert _connector_on_unknown(invalid, "res") == DEFAULT_ON_UNKNOWN


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
                return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
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


# ── MAJOR 1: shell failed write must not stamp delivery_done ──────────────────
# ── Round-trip: stamped key must match the gate-derived key ───────────────────


class TestConnectorFailedWriteNoStamp:
    async def test_failed_shell_write_not_stamped(self) -> None:
        """MAJOR 1: a shell ``command`` write that returns ``exit_code != 0``
        WITHOUT raising must NOT stamp ``delivery_done`` — otherwise a re-run of
        the same run would be silently suppressed (the "silent miss")."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

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
                data={"command": "exit 1"},
                result={"stdout": "", "stderr": "boom", "exit_code": 1, "duration_ms": 0, "masked": True},
            )

        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        # No marker at all => the failed write was treated as undelivered.
        assert slot not in fake_run.raw_output_markers

    async def test_failed_shell_write_rerun_not_suppressed(self) -> None:
        """Round-trip: a failed attempt (no stamp) followed by a re-run is NOT
        suppressed by the gate — proving the operator's recover-by-re-run fires
        rather than being silently dropped."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
            patch(
                "modulo.settings.get_settings",
                return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
                new=AsyncMock(return_value=(fake_run.raw_output_markers, _PERSISTED_KEY)),
            ),
        ):
            # First attempt: failed shell write -> not stamped.
            await _stamp_connector_write_delivered(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"command": "exit 1"},
                result={"exit_code": 1},
            )
            # Re-run of the same write -> gate must NOT suppress (no delivery_done).
            gate = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"command": "exit 1"},
            )
        assert gate is None

    async def test_successful_write_stamps_matching_key(self) -> None:
        """Round-trip: a SUCCESSFUL write's stamped ``idempotency_key`` equals
        the key the gate derives for the SAME payload, so the gate suppresses the
        re-run (proves gate-side and stamp-side keys match)."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)
        data = {"command": "echo hi"}

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

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
                data=data,
                result={"stdout": "hi", "exit_code": 0},
            )

        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        assert slot in fake_run.raw_output_markers
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["delivery_done"] is True
        # The stamped key must be exactly the gate-derived key for this payload.
        assert persisted["idempotency_key"] == _applied_key(data)

        with (
            patch(
                "modulo.settings.get_settings",
                return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
                new=AsyncMock(return_value=(fake_run.raw_output_markers, _PERSISTED_KEY)),
            ),
        ):
            gate = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
        assert isinstance(gate, dict)
        assert gate["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "connector_write_suppressed"

    def test_payload_hash_deterministic_fallback(self) -> None:
        """Non-blocking: the payload hash fallback is deterministic across
        processes (it coerces via str + sorted keys, NOT ``repr``)."""
        from modulo.core.pipeline_engine.node_runner import _connector_write_payload_hash

        class _Weird:
            def __str__(self) -> str:
                return "weird"

        odd = _Weird()
        hash_data = {"cmd": "x", "obj": odd}
        h1 = _connector_write_payload_hash(resource="command", filters={"provider_ref": None}, data=hash_data)
        h2 = _connector_write_payload_hash(resource="command", filters={"provider_ref": None}, data=hash_data)
        assert h1 == h2
        assert "weird" in h1

    def test_payload_hash_set_order_deterministic(self) -> None:
        """Set/frozenset members must be sorted in the deterministic fallback so
        the hash is byte-identical across processes (PYTHONHASHSEED) — otherwise
        the gate and stamp sides could derive DIFFERENT keys and silently defeat
        the dedup."""
        from modulo.core.pipeline_engine.node_runner import _canonical_coerce

        s = {"gamma", "alpha", "beta", frozenset({"z", "a"})}
        out = _canonical_coerce(s)
        # Stable, sorted, str-coerced — independent of Python's set iteration order.
        # (The nested frozenset coerces to a list whose str sorts before the
        # plain strings, so it leads the sorted output.)
        assert out == [["a", "z"], "alpha", "beta", "gamma"], out
        assert len({str(_canonical_coerce(s)) for _ in range(50)}) == 1

    def test_payload_hash_set_level_cross_pythonhashseed(self) -> None:
        """Hash-level proof of the MAJOR finding: ``_connector_write_payload_hash``
        must derive an identical key for set-valued ``data`` across two different
        ``PYTHONHASHSEED`` values (i.e. across separate worker processes). The
        primary ``json.dumps(..., default=str)`` path stringifies sets via
        ``str(set)``, whose order is PYTHONHASHSEED-dependent — so we re-derive the
        hash under two seeds in subprocesses and assert they match. This exercises
        the observable invariant end-to-end, not just the ``_canonical_coerce``
        fallback."""
        import os
        import subprocess
        import sys
        from pathlib import Path

        backend_src = str(Path(__file__).parents[3] / "src")
        payload_src = (
            "from modulo.core.pipeline_engine.node_runner import "
            "_connector_write_payload_hash as h; "
            "data={'items': {'z', 'a', 'm'}, 'scopes': frozenset({'read', 'write'}), "
            "'nested': {'k': {'x', 'y'}}}; "
            "print(h(resource='command', filters={'provider_ref': None}, data=data))"
        )

        def _hash_under_seed(seed: str) -> str:
            proc = subprocess.run(  # noqa: S603 - payload_src is a trusted literal constant
                [sys.executable, "-c", payload_src],
                env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": backend_src},
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert proc.returncode == 0, proc.stderr
            return proc.stdout.strip()

        assert _hash_under_seed("0") == _hash_under_seed("1")
        # And the value must be genuinely canonical: the same set rebuilt under a
        # different insertion order yields the identical key within this process.
        h_a = _connector_write_payload_hash(
            resource="command",
            filters={"provider_ref": None},
            data={"items": {"z", "a", "m"}, "scopes": frozenset({"read", "write"})},
        )
        h_b = _connector_write_payload_hash(
            resource="command",
            filters={"provider_ref": None},
            data={"items": {"m", "z", "a"}, "scopes": frozenset({"write", "read"})},
        )
        assert h_a == h_b
