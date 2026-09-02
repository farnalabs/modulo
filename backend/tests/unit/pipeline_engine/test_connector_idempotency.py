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

FAR-531 adds the INTENT markers that make the ambiguous state REACHABLE in
production: an in-flight ``connector_write_intent`` marker is persisted after
the gate proceeds and before the upstream write fires (same derived key, same
marker slot); success promotes it in place to ``delivery_done: True`` and a
REPORTED failure — the connector's OWN result shape reporting failure via the
``write_reported_failure`` hook — resolves it to ``no_delivery_confirmed:
True`` (never ambiguous — re-fires under BOTH modes). A RAISED connector error
is AMBIGUOUS (QA Fix 1: a raise cannot tell whether the write landed —
read-timeout after dispatch vs pre-dispatch validation failure), so the
in-flight intent is left AS-IS: fail_closed suppresses, fail_open re-fires.
A crash/timeout also leaves it in-flight —
ambiguous — so ``fail_closed`` finally suppresses the re-fire (previously the
ambiguous state could never exist). See ``TestConnectorIntentMarkerLifecycle``,
``TestConnectorIntentMarkerGateStates``, ``TestConnectorNoDeliveryResolution``
and ``TestRaisedConnectorErrorAmbiguous``.
The envelope honesty (AC4) and the ``write_reported_failure`` hook (AC6) and
the payload-hash determinism (AC5) are covered in their own classes. QA-fix
classes: ``TestSameKeyDeliveryEvidencePreserved`` (Fix 2 — same-key delivered
evidence survives an intent/no-delivery persist), ``TestGateEligibilityPairing``
(Fix 3 — gate and intent guard agree through ONE shared eligibility helper),
``TestPersistRaiseBoundary`` (Fix 5 — a hostile payload never fails the node
paths).

The gate is unit-tested by monkeypatching the DB read
(``_read_connector_idempotency_gate_state``) and the killswitch setting, so no
DB is required. The newest-key promotion (MAJOR 1), the intent/no-delivery
marker writes and the stamp's in-place promotion are tested against a fake DB
session harness that captures the persisted marker, since they happen inside
``_write_raw_output_marker`` (the REAL marker-storage path — the same harness
the FAR-458 suite uses). Fail-open behaviour (missing run id / session
factory / key, killswitch off) is asserted directly since that is the safety
contract that must never block a connector write.

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
import warnings
from typing import Any, Self
from unittest.mock import AsyncMock, patch

import pytest

from modulo.connectors.base import DEFAULT_ON_UNKNOWN, ON_UNKNOWN_MODES, ConnectorBase
from modulo.connectors.rest import _normalise_on_unknown
from modulo.connectors.shell import ShellConnector
from modulo.core.pipeline_engine.idempotency import (
    node_idempotency_key,
    read_before_write_ambiguous,
    read_before_write_suppression,
)
from modulo.core.pipeline_engine.node_runner import (
    _canonical_scalar,
    _connector_gate_enabled,
    _connector_intent_marker_enabled,
    _connector_marker_attempt_key,
    _connector_on_unknown,
    _connector_write_gate,
    _connector_write_payload_hash,
    _connector_write_reported_failure,
    _idempotency_gate_skipped_envelope,
    _mark_connector_write_no_delivery,
    _persist_connector_write_intent,
    _resolve_connector_write_outcome,
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


# ── AC6: the write_reported_failure hook (reported-failure shape is opt-in) ───
# ── FAR-531: intent markers (write-before / stamp-after) ──────────────────────


def _shell_connector() -> ShellConnector:
    """A real ShellConnector (its ``write_reported_failure`` override is the
    AC6 contract); construction warns deprecation, which the suite fails on."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return ShellConnector(allowed_commands=["echo"])


class TestWriteReportedFailureHook:
    def test_connector_base_default_is_false(self) -> None:
        """The default contract: a non-raising write IS a delivery. Connectors
        that raise on failure never override the hook, so the stamp proceeds
        exactly as before FAR-531."""

        class _RaisingConnector(ConnectorBase):
            @property
            def connector_type(self) -> Any:  # pragma: no cover - ABC plumbing
                return None

            async def health_check(self) -> Any:  # pragma: no cover - ABC plumbing
                raise NotImplementedError

            async def query(self, q: Any) -> Any:  # pragma: no cover - ABC plumbing
                raise NotImplementedError

            async def write(self, payload: Any) -> Any:  # pragma: no cover - ABC plumbing
                raise NotImplementedError

        assert _RaisingConnector().write_reported_failure({"anything": 1}) is False

    def test_shell_reports_failure_on_nonzero_exit_code(self) -> None:
        """ShellConnector's ``command``/``file`` write results carry the executed
        command's ``exit_code`` — non-zero is a REPORTED failure without a
        raise, so the stamp must not treat the return as a delivery."""
        connector = _shell_connector()
        assert connector.write_reported_failure({"stdout": "", "stderr": "boom", "exit_code": 1}) is True
        assert connector.write_reported_failure({"exit_code": 42}) is True

    def test_shell_reports_failure_on_none_exit_code(self) -> None:
        """QA Fix 4: ``exit_code: None`` IS a reported failure. E2B can produce
        it (``CommandsExecResult.exit_code`` is Optional for killed /
        failed-to-start commands; the runtime provider only defaults on a
        MISSING attribute) — a killed command is NOT a confirmed delivery, so
        the result must not stamp ``delivery_done`` and suppress the re-run in
        every mode (main's not-delivered semantics for None)."""
        connector = _shell_connector()
        assert connector.write_reported_failure({"stdout": "", "stderr": "", "exit_code": None}) is True

    def test_shell_does_not_report_failure_on_success_shape(self) -> None:
        connector = _shell_connector()
        assert connector.write_reported_failure({"stdout": "hi", "exit_code": 0}) is False
        assert connector.write_reported_failure({"path": "/x", "bytes_written": 2, "exit_code": 0}) is False
        # A result without an exit_code shape is not a REPORTED failure.
        assert connector.write_reported_failure({"ok": True}) is False
        assert connector.write_reported_failure(None) is False

    def test_defensive_reader_uses_the_hook(self) -> None:
        connector = _shell_connector()
        assert _connector_write_reported_failure(connector, {"exit_code": 1}) is True
        assert _connector_write_reported_failure(connector, {"exit_code": 0}) is False
        assert _connector_write_reported_failure(connector, {"exit_code": None}) is True

    def test_defensive_reader_fails_open_without_hook(self) -> None:
        """A connector WITHOUT the hook (or a non-connector object) is treated
        as not-reporting-failure — the pre-FAR-531 default for connectors that
        raise on failure."""
        assert _connector_write_reported_failure(object(), {"exit_code": 1}) is False
        assert _connector_write_reported_failure(None, {"exit_code": 1}) is False

    def test_defensive_reader_fails_open_when_hook_raises(self) -> None:
        broken = types.SimpleNamespace(write_reported_failure=lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _connector_write_reported_failure(broken, {"exit_code": 1}) is False


class TestConnectorFailedWriteRoutesToNoDelivery:
    """The AC6 routing the node performs: a reported failure is a DEFINITE
    no-delivery — the intent marker resolves to ``no_delivery_confirmed`` (so a
    re-run re-fires under BOTH modes), and ``delivery_done`` is never stamped."""

    async def test_reported_failure_marks_no_delivery_and_rerun_refires(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        connector = _shell_connector()
        data = {"command": "exit 1"}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
            patch(
                "modulo.settings.get_settings",
                return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
            ),
        ):
            # The node path: intent BEFORE the write (the node consults
            # _connector_intent_marker_enabled), then the reported failure
            # resolves it.
            assert _connector_intent_marker_enabled("fail_closed") is True
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
            assert _connector_write_reported_failure(connector, {"exit_code": 1}) is True
            await _mark_connector_write_no_delivery(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                reason="connector_reported_failure",
            )

        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["no_delivery_confirmed"] is True
        assert persisted["marker_kind"] == "connector_write_no_delivery"
        assert persisted.get("delivery_done") is not True
        assert persisted["idempotency_key"] == _applied_key(data)

        # Round-trip: a re-run of the SAME failed write re-fires under BOTH
        # modes (never suppress a definite failure) — the pre-FAR-531 contract
        # for failed writes, now WITH evidence instead of a bare absent marker.
        for mode in ("fail_open", "fail_closed"):
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
                    on_unknown=mode,
                )
            assert gate is None, f"a definite no-delivery must re-fire under {mode}"

    async def test_reported_failure_without_intent_markers_never_stamps(self) -> None:
        """Killswitch off (no intent markers): the pre-FAR-531 behaviour is
        preserved — a reported failure produces NO delivery_done stamp (the
        node's real post-write resolver, driven directly)."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        connector = _shell_connector()
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _resolve_connector_write_outcome(
                session_factory,
                connector=connector,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"command": "exit 1"},
                result={"stdout": "", "stderr": "boom", "exit_code": 1, "duration_ms": 0, "masked": True},
                intent_active=False,
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        assert slot not in fake_run.raw_output_markers

    async def test_none_exit_code_routes_to_no_delivery_never_stamps(self) -> None:
        """QA Fix 4: ``exit_code: None`` (E2B killed / failed-to-start command)
        is a REPORTED failure — it must never stamp ``delivery_done``. With
        intent markers active it resolves to ``no_delivery_confirmed`` (the
        re-run stays possible under BOTH modes); without them nothing is
        stamped at all."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        connector = _shell_connector()
        data = {"command": "killed-command"}
        none_result = {"stdout": "", "stderr": "", "exit_code": None, "duration_ms": 0}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            # Without intent markers: no stamp at all (main's not-delivered
            # semantics for None).
            await _resolve_connector_write_outcome(
                session_factory,
                connector=connector,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                result=none_result,
                intent_active=False,
            )
            slot = _connector_marker_attempt_key("run-123", _NODE_ID)
            assert slot not in fake_run.raw_output_markers, "None exit_code must not stamp delivered"

            # With intent markers: resolves to a definite no-delivery.
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
            await _resolve_connector_write_outcome(
                session_factory,
                connector=connector,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                result=none_result,
                intent_active=True,
            )
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["no_delivery_confirmed"] is True
        assert persisted.get("delivery_done") is not True


class TestConnectorIntentMarkerLifecycle:
    """FAR-531: the intent marker is persisted BEFORE the write fires, in the
    SAME slot the delivery stamp updates — success promotes it in place (no
    duplicate rows), and it never inherits a superseded key's delivery."""

    async def test_intent_persisted_pre_write_with_matching_key(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"name": "n1"}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        assert slot in fake_run.raw_output_markers
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["marker_kind"] == "connector_write_intent"
        assert persisted["_modulo_marker"] is True
        assert persisted.get("delivery_done") is not True
        # AC1: the intent marker carries the EXACT derived key the gate reads.
        assert persisted["idempotency_key"] == _applied_key(data)

        # ...and the gate SEES it (cross-attempt visibility: the gate read spans
        # the run's whole markers dict and matches by derived key).
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
            fail_closed = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                on_unknown="fail_closed",
            )
            fail_open = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                on_unknown="fail_open",
            )
        assert isinstance(fail_closed, dict), "an in-flight intent must suppress under fail_closed"
        envelope = fail_closed["artifacts"][0]["output"]["output_json"]
        assert envelope["idempotency_gate"] == "connector_write_fail_closed"
        # AC4: the suppressed write is NOT confirmed delivered.
        assert envelope["delivery_done"] is False
        assert fail_open is None, "fail_open re-fires an in-flight intent (unchanged)"

    async def test_success_promotes_intent_in_place_no_duplicate_rows(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"name": "n1"}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
            await _stamp_connector_write_delivered(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                result={"ok": True},
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        # ONE row: the stamp UPDATED the intent marker in place.
        assert len(fake_run.raw_output_markers) == 1
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["delivery_done"] is True
        assert persisted["idempotency_key"] == _applied_key(data)
        # The intent kind does not survive the promotion (it is delivered now).
        assert persisted.get("marker_kind") != "connector_write_intent"
        assert persisted.get("no_delivery_confirmed") is not True

    async def test_intent_never_inherits_superseded_key_delivery(self) -> None:
        """A delivered marker for a SUPERSEDED content-version (same slot,
        promoted key) must not bleed ``delivery_done: True`` into a NEW
        intent's marker — that would claim the new key was delivered BEFORE
        the write fired (a fail_closed-relevant silent miss)."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            # Attempt 1 delivers v1 (slot carries K1 + delivery_done).
            await _stamp_connector_write_delivered(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v1"},
                result={"ok": True},
            )
            # Attempt 2 writes an intent for the EDITED v2 — same slot,
            # different derived key. It must NOT inherit delivery_done.
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v2"},
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["marker_kind"] == "connector_write_intent"
        assert persisted.get("delivery_done") is not True, (
            "an in-flight intent must never claim a delivery it has not made"
        )
        assert persisted["idempotency_key"] == _applied_key({"name": "v2"})

    async def test_intent_write_fails_open_on_missing_context(self) -> None:
        """No session factory / no run id => the intent write is skipped (the
        write still fires) — best-effort, never fails the node."""
        marker_store: dict[str, Any] = {}
        fake_run = _FakeConnectorRun(markers=marker_store, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        await _persist_connector_write_intent(
            None,
            run_id="run-123",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"name": "n1"},
        )
        await _persist_connector_write_intent(
            session_factory,
            run_id="",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"name": "n1"},
        )
        assert marker_store == {}


class TestConnectorIntentMarkerGuard:
    """The intent marker is written ONLY when the gate could suppress on
    ambiguity: killswitch enabled AND ``on_unknown != off``."""

    def test_disabled_when_mode_off(self) -> None:
        with patch(
            "modulo.settings.get_settings",
            return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
        ):
            assert _connector_intent_marker_enabled("off") is False

    def test_disabled_when_killswitch_off(self) -> None:
        with patch(
            "modulo.settings.get_settings",
            return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=False),
        ):
            assert _connector_intent_marker_enabled("fail_closed") is False
            assert _connector_intent_marker_enabled("fail_open") is False

    def test_disabled_when_killswitch_missing(self) -> None:
        with patch("modulo.settings.get_settings", return_value=types.SimpleNamespace()):
            assert _connector_intent_marker_enabled("fail_closed") is False

    def test_disabled_when_settings_read_fails(self) -> None:
        with patch("modulo.settings.get_settings", side_effect=RuntimeError("boom")):
            assert _connector_intent_marker_enabled("fail_closed") is False

    def test_enabled_for_open_and_closed_when_killswitch_on(self) -> None:
        """fail_open ALSO writes the marker (one uniform state machine; the
        evidence survives an operator later flipping to fail_closed) — but the
        fail_open GATE semantics are unchanged (it never suppresses ambiguity)."""
        with patch(
            "modulo.settings.get_settings",
            return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
        ):
            assert _connector_intent_marker_enabled("fail_open") is True
            assert _connector_intent_marker_enabled("fail_closed") is True


class TestConnectorNoDeliveryResolution:
    """FAR-531: a definite failure — ONLY the REPORTED-failure path (the
    connector's own result shape, via the ``write_reported_failure`` hook) —
    resolves the in-flight intent to ``no_delivery_confirmed`` — NOT ambiguous,
    re-fires under BOTH modes (never suppress a definite failure). A RAISED
    connector error is NOT routed here (QA Fix 1 — see
    ``TestRaisedConnectorErrorAmbiguous``)."""

    async def test_no_delivery_marker_resolves_ambiguity(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"name": "n1"}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
            await _mark_connector_write_no_delivery(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                reason="connector_reported_failure",
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["no_delivery_confirmed"] is True
        assert persisted["marker_kind"] == "connector_write_no_delivery"
        assert persisted["summary"] == "connector write did not reach upstream (connector_reported_failure)"
        assert len(fake_run.raw_output_markers) == 1, "the intent is resolved in place, not duplicated"

    async def test_no_delivery_fails_open_on_missing_context(self) -> None:
        marker_store: dict[str, Any] = {}
        fake_run = _FakeConnectorRun(markers=marker_store, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        await _mark_connector_write_no_delivery(
            None,
            run_id="run-123",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"name": "n1"},
            reason="connector_reported_failure",
        )
        await _mark_connector_write_no_delivery(
            session_factory,
            run_id="",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"name": "n1"},
            reason="connector_reported_failure",
        )
        assert marker_store == {}


class TestConnectorIntentMarkerGateStates:
    """Gate-level states for the FAR-531 marker vocabulary (synthetic markers,
    mirroring the FAR-458 gate-test style)."""

    async def test_no_delivery_marker_never_suppresses_fail_closed(self) -> None:
        applied = _applied_key({"name": "n1"})
        markers = {
            "slot": {
                "_modulo_marker": True,
                "marker_kind": "connector_write_no_delivery",
                "no_delivery_confirmed": True,
                "idempotency_key": applied,
            }
        }
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            on_unknown="fail_closed",
        )
        assert result is None, "a definite no-delivery re-fires under fail_closed"

    async def test_stale_intent_marker_suppresses_fail_closed(self) -> None:
        """The headline FAR-531 fix: an IN-FLIGHT intent marker (crash/timeout
        between the intent persist and the stamp) is ambiguous — fail_closed
        suppresses the re-fire; the envelope does NOT claim delivery."""
        applied = _applied_key({"name": "n1"})
        markers = {
            "slot": {
                "_modulo_marker": True,
                "marker_kind": "connector_write_intent",
                "idempotency_key": applied,
            }
        }
        result = await _run_gate(
            markers=markers,
            persisted_key=_PERSISTED_KEY,
            data={"name": "n1"},
            session_factory=lambda: None,
            on_unknown="fail_closed",
        )
        assert isinstance(result, dict)
        assert result["artifacts"][0]["status"] == "skipped"
        envelope = result["artifacts"][0]["output"]["output_json"]
        assert envelope["idempotency_gate"] == "connector_write_fail_closed"
        assert envelope["delivery_done"] is False

    def test_no_delivery_marker_is_not_ambiguous_for_gate(self) -> None:
        """Only the newest state counts: when the slot holds the resolved
        no-delivery marker, the same key is NOT ambiguous even though a prior
        intent existed (the intent was REPLACED in place)."""
        applied = _applied_key({"name": "n1"})
        markers = {
            "slot": {
                "_modulo_marker": True,
                "marker_kind": "connector_write_no_delivery",
                "no_delivery_confirmed": True,
                "idempotency_key": applied,
            }
        }
        # read_before_write_ambiguous directly (the slot holds ONE marker).
        assert (
            read_before_write_ambiguous(
                markers, run_ref=_PERSISTED_KEY, node_ref=_NODE_ID, payload=_payload_hash({"name": "n1"})
            )
            is False
        )


class TestRaisedConnectorErrorAmbiguous:
    """QA Fix 1: a RAISED connector write error is AMBIGUOUS, not a definite
    no-delivery — a read-timeout / connection-reset AFTER dispatch may still
    have landed the write. The classification flows through
    ``_resolve_connector_write_outcome`` (the single authority), which leaves
    the in-flight intent AS-IS: fail_closed suppresses the re-fire ("possible
    silent miss"), fail_open re-fires (unchanged). Only the connector's OWN
    reported-failure shape (``write_reported_failure`` hook) is trusted as a
    definite no-delivery."""

    async def test_raised_exception_leaves_intent_in_flight_and_gate_classifies(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"command": "slow-write"}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
            # The node's except path: the raised error resolves through the
            # single authority, which classifies it AMBIGUOUS.
            await _resolve_connector_write_outcome(
                session_factory,
                connector=_shell_connector(),
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                result=None,
                intent_active=True,
                exception=RuntimeError("read timeout after dispatch"),
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["marker_kind"] == "connector_write_intent", "left in-flight — NOT resolved to no-delivery"
        assert persisted.get("no_delivery_confirmed") is not True, "a raise must not persist definite no-delivery"
        assert persisted.get("delivery_done") is not True

        # Gate classification of the surviving in-flight intent: fail_closed
        # SUPPRESSES, fail_open re-fires.
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
            fail_closed = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                on_unknown="fail_closed",
            )
            fail_open = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                on_unknown="fail_open",
            )
        assert isinstance(fail_closed, dict), "a raised-error intent is ambiguous: fail_closed suppresses"
        envelope = fail_closed["artifacts"][0]["output"]["output_json"]
        assert envelope["idempotency_gate"] == "connector_write_fail_closed"
        assert envelope["delivery_done"] is False
        assert fail_open is None, "a raised-error intent re-fires under fail_open (unchanged)"

    async def test_raised_exception_without_intent_writes_nothing(self) -> None:
        """No intent marker in flight (killswitch off / query op): the raised
        error persists NOTHING — the pre-FAR-531 behaviour (failed node, no
        marker evidence) is preserved."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            await _resolve_connector_write_outcome(
                session_factory,
                connector=_shell_connector(),
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "n1"},
                result=None,
                intent_active=False,
                exception=RuntimeError("boom"),
            )
        assert not fake_run.raw_output_markers


class TestSameKeyDeliveryEvidencePreserved:
    """QA Fix 2: delivered evidence for the SAME derived key is monotone — an
    intent / no-delivery persist arriving after a confirmed delivery stamp must
    not wipe it (concurrent-attempt window, brownout re-run), while a
    DIFFERENT-key (superseded content-version) persist still drops it."""

    async def test_same_key_no_delivery_after_delivered_stamp_keeps_delivery_done(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"command": "echo hi"}
        with (
            patch("modulo.core.pipeline_engine.node_runner.set_rls_org", new=AsyncMock()),
            patch("modulo.core.pipeline_engine.node_runner.set_rls_execution_context", new=AsyncMock()),
        ):
            # Attempt A genuinely delivered.
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
            # Attempt B (concurrent window): the reported-failure resolution
            # persists AFTER the delivery stamp, SAME derived key.
            await _mark_connector_write_no_delivery(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                reason="connector_reported_failure",
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["delivery_done"] is True, "same-key no-delivery persist must not wipe delivered evidence"
        assert persisted["no_delivery_confirmed"] is True
        assert persisted["idempotency_key"] == _applied_key(data)

    async def test_same_key_intent_after_delivered_stamp_keeps_delivery_done(self) -> None:
        """Brownout sequence: attempt 2's gate read times out (fail-open) but
        its intent persist succeeds — the SAME-key intent must not wipe
        attempt A's delivered evidence."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"command": "echo hi"}
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
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted["delivery_done"] is True, "same-key intent persist must not wipe delivered evidence"

    async def test_concurrent_window_delivered_stamp_survives_and_gate_suppresses(self) -> None:
        """End-to-end brownout round-trip: delivered stamp → same-key intent →
        the gate re-read SUPPRESSES (the delivered write is not re-fired)."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"command": "echo hi"}
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
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
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
            for mode in ("fail_open", "fail_closed"):
                gate = await _connector_write_gate(
                    session_factory,
                    run_id="run-123",
                    org_id_raw=_ORG_UUID,
                    node_id=_NODE_ID,
                    resource="command",
                    filters={},
                    data=data,
                    on_unknown=mode,
                )
                assert isinstance(gate, dict), f"delivered evidence must suppress under {mode}"
                assert gate["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == "connector_write_suppressed"

    async def test_different_key_intent_still_drops_delivery_done(self) -> None:
        """The original rationale survives: a SUPERSEDED content-version's
        delivered marker must not bleed into a NEW key's intent."""
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
                data={"name": "v1"},
                result={"ok": True},
            )
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data={"name": "v2"},
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        persisted = fake_run.raw_output_markers[slot]
        assert persisted.get("delivery_done") is not True, "a different key still drops delivered evidence"
        assert persisted["marker_kind"] == "connector_write_intent"


class TestGateEligibilityPairing:
    """QA Fix 3: gate eligibility and the intent-marker guard are ONE shared
    helper — the invariant "an intent marker is written ⟺ the gate can
    suppress on ambiguity" must hold across killswitch / mode combinations."""

    @pytest.mark.parametrize("mode", ["fail_open", "fail_closed", "off"])
    def test_intent_guard_equals_gate_eligibility(self, mode: str) -> None:
        with patch(
            "modulo.settings.get_settings",
            return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=True),
        ):
            assert _connector_intent_marker_enabled(mode) == _connector_gate_enabled(mode)
        with patch(
            "modulo.settings.get_settings",
            return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=False),
        ):
            assert _connector_intent_marker_enabled(mode) == _connector_gate_enabled(mode)

    def test_intent_guard_equals_gate_eligibility_when_settings_read_fails(self) -> None:
        with patch("modulo.settings.get_settings", side_effect=RuntimeError("boom")):
            assert _connector_gate_enabled("fail_closed") is False
            assert _connector_intent_marker_enabled("fail_closed") is False

    async def test_gate_proceeds_implies_intent_marker_written(self) -> None:
        """Behavioural pairing through the real paths: killswitch ON + a
        first-time fail_closed write → the gate PROCEEDS and the intent marker
        is actually persisted. Killswitch OFF → the gate proceeds (disabled)
        but the guard writes NOTHING (a marker nobody would read)."""
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        data = {"name": "n1"}
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
            gate = await _connector_write_gate(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                on_unknown="fail_closed",
            )
            assert gate is None, "a first-time write proceeds"
            assert _connector_intent_marker_enabled("fail_closed") is True
            await _persist_connector_write_intent(
                session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
            )
        slot = _connector_marker_attempt_key("run-123", _NODE_ID)
        assert slot in fake_run.raw_output_markers, "gate proceeded ⇒ intent marker written"

        # Killswitch OFF: the gate proceeds (eligibility disabled) but the
        # guard says NO marker — the node would never persist one.
        fresh_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def fresh_session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fresh_run)

        with (
            patch(
                "modulo.settings.get_settings",
                return_value=types.SimpleNamespace(modulo_connector_write_gate_enabled=False),
            ),
            patch(
                "modulo.core.pipeline_engine.node_runner._read_connector_idempotency_gate_state",
                new=AsyncMock(return_value=(fresh_run.raw_output_markers, _PERSISTED_KEY)),
            ),
        ):
            gate = await _connector_write_gate(
                fresh_session_factory,
                run_id="run-123",
                org_id_raw=_ORG_UUID,
                node_id=_NODE_ID,
                resource="command",
                filters={},
                data=data,
                on_unknown="fail_closed",
            )
            assert gate is None, "killswitch off: the gate proceeds (disabled, cannot suppress)"
            assert _connector_intent_marker_enabled("fail_closed") is False
        assert not fresh_run.raw_output_markers, "gate disabled ⇒ no intent marker"


class TestPersistRaiseBoundary:
    """QA Fix 5: the connector persist helpers' payload-hash computation can
    raise on an adversarial payload (a raising ``__str__`` escapes the hash's
    ``except (TypeError, ValueError)``). The never-fails contract must hold:
    any such failure degrades to "no marker" with a log — the node proceeds
    (pre-write intent) / the original outcome is preserved (post-write
    resolution, where a raise would otherwise mask the connector's error)."""

    class _RaisingStr:
        def __str__(self) -> str:
            raise RuntimeError("str boom")

    async def test_intent_persist_never_raises_on_hostile_payload(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        await _persist_connector_write_intent(
            session_factory,
            run_id="run-123",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"obj": self._RaisingStr()},
        )
        assert not fake_run.raw_output_markers, "hash/persist failure degrades to no marker; node proceeds"

    async def test_no_delivery_persist_never_raises_on_hostile_payload(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        await _mark_connector_write_no_delivery(
            session_factory,
            run_id="run-123",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"obj": self._RaisingStr()},
            reason="connector_reported_failure",
        )
        assert not fake_run.raw_output_markers, "original error/result preserved — persist never masks it"

    async def test_delivered_stamp_never_raises_on_hostile_payload(self) -> None:
        fake_run = _FakeConnectorRun(markers={}, idempotency_key=_PERSISTED_KEY)

        def session_factory() -> _FakeConnectorSession:
            return _FakeConnectorSession(fake_run)

        await _stamp_connector_write_delivered(
            session_factory,
            run_id="run-123",
            org_id_raw=_ORG_UUID,
            node_id=_NODE_ID,
            resource="command",
            filters={},
            data={"obj": self._RaisingStr()},
            result={"ok": True},
        )
        assert not fake_run.raw_output_markers, "a hostile payload must not fail the node after a delivered write"


class TestEnvelopeHonesty:
    """AC4: the skip envelope's ``delivery_done`` must reflect WHY the write
    was skipped — a confirmed-delivery dedup IS delivered; an ambiguous
    fail-closed suppression is NOT (suppressed ≠ delivered)."""

    def test_confirmed_suppression_claims_delivery(self) -> None:
        envelope = _idempotency_gate_skipped_envelope(_NODE_ID, gate_tag="connector_write_suppressed")
        output_json = envelope["artifacts"][0]["output"]["output_json"]
        assert output_json["delivery_done"] is True
        assert output_json["idempotency_gate"] == "connector_write_suppressed"

    def test_fail_closed_suppression_does_not_claim_delivery(self) -> None:
        envelope = _idempotency_gate_skipped_envelope(_NODE_ID, gate_tag="connector_write_fail_closed", delivered=False)
        output_json = envelope["artifacts"][0]["output"]["output_json"]
        assert output_json["delivery_done"] is False
        assert output_json["idempotency_gate"] == "connector_write_fail_closed"
        # The envelope still satisfies the shape consumers rely on.
        assert envelope["artifacts"][0]["status"] == "skipped"

    def test_default_envelope_unchanged_for_sandbox_sentinel(self) -> None:
        """The FAR-228 sandbox path (genuine delivery sentinel) is untouched."""
        envelope = _idempotency_gate_skipped_envelope(_NODE_ID)
        assert envelope["artifacts"][0]["output"]["output_json"]["delivery_done"] is True

    @pytest.mark.parametrize(
        ("kwargs", "expected_gate"),
        [
            ({}, None),
            ({"gate_tag": "connector_write_suppressed"}, "connector_write_suppressed"),
        ],
    )
    def test_envelope_gate_tag_roundtrip(self, kwargs: dict[str, Any], expected_gate: str | None) -> None:
        from modulo.core.pipeline_engine.executor import _node_output_has_idempotency_gate

        envelope = _idempotency_gate_skipped_envelope(_NODE_ID, **kwargs)
        assert _node_output_has_idempotency_gate(envelope) is True
        if expected_gate is not None:
            assert envelope["artifacts"][0]["output"]["output_json"]["idempotency_gate"] == expected_gate


class TestSuccessfulWriteStamp:
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


# ── AC5: payload-hash determinism (PYTHONHASHSEED- AND address-safe) ─────────


class TestPayloadHashDeterminism:
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
        primary ``json.dumps`` path would stringify sets via ``str(set)``, whose
        order is PYTHONHASHSEED-dependent — so we re-derive the hash under two
        seeds in subprocesses and assert they match. This exercises the
        observable invariant end-to-end, not just the ``_canonical_coerce``
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
                check=False,
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

    def test_canonical_scalar_never_embeds_memory_address(self) -> None:
        """AC5 (the hole the set fix missed): ``str()`` of an object with the
        DEFAULT ``__str__``/``__repr__`` embeds its memory address, which differs
        in every worker process. ``_canonical_scalar`` renders a stable type
        identity instead; types with a CUSTOM ``__str__`` keep their meaningful
        rendering (dates, Paths and friends are unaffected)."""
        from datetime import datetime
        from pathlib import PurePosixPath

        class _Opaque:
            pass

        rendered = _canonical_scalar(_Opaque())
        assert "0x" not in rendered, f"memory address leaked into the canonical scalar: {rendered!r}"
        assert rendered == f"<{_Opaque.__module__}.{_Opaque.__qualname__}>"
        # A custom __str__ is preserved (deterministic by contract).
        assert _canonical_scalar(datetime(2026, 1, 1)) == "2026-01-01 00:00:00"
        assert _canonical_scalar(PurePosixPath("/tmp/x")) == "/tmp/x"
        assert _canonical_scalar("plain") == "plain"

    def test_payload_hash_with_opaque_object_is_address_free(self) -> None:
        """The primary json.dumps path must not leak memory addresses for
        opaque objects either (pre-FAR-531 it rendered ``<X object at 0x...>``),
        and the hash stays equal for equal-shape payloads within a process."""

        class _Opaque:
            pass

        h1 = _connector_write_payload_hash(resource="command", filters={}, data={"obj": _Opaque(), "n": 1})
        h2 = _connector_write_payload_hash(resource="command", filters={}, data={"obj": _Opaque(), "n": 1})
        assert h1 == h2
        assert "0x" not in h1, f"memory address leaked into the payload hash: {h1!r}"
        # A genuinely different payload still derives a different hash.
        assert h1 != _connector_write_payload_hash(resource="command", filters={}, data={"obj": _Opaque(), "n": 2})

    def test_payload_hash_opaque_object_cross_pythonhashseed(self) -> None:
        """End-to-end AC5 proof: a payload containing an opaque object must
        derive the IDENTICAL hash under two different PYTHONHASHSEED values
        (separate worker processes) — the byte-identical docstring claim is now
        actually true."""
        import os
        import subprocess
        import sys
        from pathlib import Path

        backend_src = str(Path(__file__).parents[3] / "src")
        payload_src = (
            "from modulo.core.pipeline_engine.node_runner import "
            "_connector_write_payload_hash as h; "
            "Opaque = type('Opaque', (), {}); "
            "data={'obj': Opaque(), 's': {'b', 'a'}}; "
            "print(h(resource='command', filters={}, data=data))"
        )

        def _hash_under_seed(seed: str) -> str:
            proc = subprocess.run(  # noqa: S603 - payload_src is a trusted literal constant
                [sys.executable, "-c", payload_src],
                env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": backend_src},
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            assert proc.returncode == 0, proc.stderr
            return proc.stdout.strip()

        assert _hash_under_seed("0") == _hash_under_seed("1")
