"""Residual unit coverage for modulo.core.pipeline_engine.node_runner (FAR-619).

Edge branches the existing suites leave uncovered: egress allowlist resolution,
sandbox log-tail diagnostics, the connector-write idempotency helper branches,
the connector node body (make_connector_fn), HITL gate-eval envelope/target
resolution, the reject-correction dispatch, the sandbox watchdog's probe /
budget / stream error paths, the dispatch marker/lease/api-key helpers, and
sandbox-dispatch env/truncation/teardown paths.

All DB access is faked in-memory; no Docker, no Postgres, no Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.pipeline_engine.node_runner as nr
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError,
    ScriptBudgetKilledError,
    ScriptInvalidOutputError,
    SupersededNodeError,
    _SandboxWatchdog,
    _wait_command_with_idle_watchdog,
    make_connector_fn,
    make_sandbox_agent_fn,
)

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_ORG_UUID = uuid.UUID(_ORG_ID)
_RUN_ID = "run-1"
_RUN_UUID_STR = str(uuid.uuid4())
_AGENT_COMMAND = "opencode run --auto --format json < /home/user/prompt.md"


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


class _FakeSession:
    """Async session double for node_runner's bounded DB reads/writes.

    ``router`` receives the statement text for EVERY execute call (the RLS
    preamble runs the real generic-backend path against the sqlite dialect).
    """

    def __init__(self, router: Callable[[str], Any] | None = None) -> None:
        self._router = router
        self.info: dict[str, Any] = {}
        self.added: list[object] = []
        self.executed: list[str] = []
        self.enter_error: BaseException | None = None

    async def __aenter__(self) -> Self:
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> Self:
        return self

    def in_transaction(self) -> bool:
        return True

    def get_bind(self) -> Any:
        bind = MagicMock()
        bind.dialect.name = "sqlite"
        return bind

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        stmt_text = str(stmt)
        self.executed.append(stmt_text)
        if self._router is not None:
            return self._router(stmt_text)
        return MagicMock()

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": _RUN_ID,
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _sandbox_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": "n1",
        "agent_prompt": "Do the thing",
        "agent_command": _AGENT_COMMAND,
    }
    node_def.update(overrides)
    return node_def


def _make_sandbox_mock(*, log_content: str = "", output_json: str = '{"summary": "done"}'):
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    def _read(path: Any, format: str = "text", **kwargs: Any) -> Any:
        if str(path).endswith("output.json"):
            return output_json
        return log_content

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=len(log_content)))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    return sandbox


class _MarkerRunRow:
    """In-memory ``runs`` row for marker-persist assertions."""

    def __init__(self, run_id: str, idempotency_key: str | None = None) -> None:
        self.id = run_id
        self.raw_output_markers: dict[str, Any] = {}
        self.idempotency_key = idempotency_key


def _run_row_router(row: _MarkerRunRow | None) -> Callable[[str], Any]:
    def _route(stmt_text: str) -> Any:
        if "FROM runs" in stmt_text:
            r = MagicMock()
            r.scalar_one_or_none.return_value = row
            return r
        return MagicMock()

    return _route


# ---------------------------------------------------------------------------
# _resolve_egress_allowlist — best-effort resolution branches
# ---------------------------------------------------------------------------


async def test_egress_allowlist_non_string_host_passthrough():
    entries = [{"host": 123, "port": 443}]
    resolved = await nr._resolve_egress_allowlist(entries)
    assert resolved == entries
    assert "_resolved_ip" not in resolved[0]


async def test_egress_allowlist_numeric_and_ipv6_hosts_untouched():
    entries = [{"host": "8.8.8.8"}, {"host": "::1"}]
    resolved = await nr._resolve_egress_allowlist(entries)
    assert resolved[0]["host"] == "8.8.8.8"
    assert resolved[1]["host"] == "::1"
    assert "_resolved_ip" not in resolved[0]
    assert "_resolved_ip" not in resolved[1]


async def test_egress_allowlist_unresolvable_host_stays_denied(monkeypatch: pytest.MonkeyPatch):
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("no dns")

    monkeypatch.setattr(nr.socket, "getaddrinfo", _boom)
    entries = [{"host": "does-not-exist.invalid"}]
    resolved = await nr._resolve_egress_allowlist(entries)
    assert resolved[0]["host"] == "does-not-exist.invalid"
    assert "_resolved_ip" not in resolved[0]


async def test_egress_allowlist_empty_result_keeps_hostname(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nr.socket, "getaddrinfo", lambda *a, **k: [])
    entries = [{"host": "empty-records.example"}]
    resolved = await nr._resolve_egress_allowlist(entries)
    assert "_resolved_ip" not in resolved[0]


async def test_egress_allowlist_none_returns_none():
    assert await nr._resolve_egress_allowlist(None) is None


# ---------------------------------------------------------------------------
# _compute_sandbox_cost — non-finite total guard
# ---------------------------------------------------------------------------


def test_compute_sandbox_cost_non_finite_total_returns_zero(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(nr, "_e2b_rate_runtime", lambda: 0.13)
    assert nr._compute_sandbox_cost(float("inf"), None) == 0.0


# ---------------------------------------------------------------------------
# _fetch_sandbox_log_tail — parses E2B log payloads or fails open
# ---------------------------------------------------------------------------


def _fake_urlopen(payload: bytes) -> Any:
    class _Resp:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> bool:
            return False

        def read(self) -> bytes:
            return payload

    return _Resp()


async def test_fetch_sandbox_log_tail_parses_preferred_levels(monkeypatch: pytest.MonkeyPatch):
    payload = (
        b'{"logEntries": ['
        b'{"message": "error line", "level": "error"},'
        b'{"message": "plain line", "level": "debug"},'
        b'{"fields": "fields-only", "level": "warn"}'
        b"]}"
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(payload))
    monkeypatch.setenv("E2B_API_KEY", "k")
    tail = await nr._fetch_sandbox_log_tail("sbx-1", limit=2)
    # Preferred levels sort ahead; the tail of the union keeps the LAST two
    # entries overall ("fields-only" + the non-preferred "plain line").
    assert tail == "fields-only\nplain line"


async def test_fetch_sandbox_log_tail_non_list_payload_returns_raw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(b'{"other": 1}'))
    monkeypatch.setenv("MODULO_E2B_API_KEY", "k")
    tail = await nr._fetch_sandbox_log_tail("sbx-1")
    assert tail == '{"other": 1}'


async def test_fetch_sandbox_log_tail_invalid_json_returns_raw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: _fake_urlopen(b"not json"))
    monkeypatch.setenv("E2B_API_KEY", "k")
    tail = await nr._fetch_sandbox_log_tail("sbx-1")
    assert tail == "not json"


async def test_fetch_sandbox_log_tail_network_failure_returns_empty(monkeypatch: pytest.MonkeyPatch):
    def _boom(req: Any, timeout: Any) -> Any:
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    monkeypatch.setenv("E2B_API_KEY", "k")
    tail = await nr._fetch_sandbox_log_tail("sbx-1")
    assert not tail


# ---------------------------------------------------------------------------
# _persist_raw_output_marker / _write_raw_output_marker
# ---------------------------------------------------------------------------


async def test_persist_raw_output_marker_reraises_cancellation():
    with (
        patch.object(nr, "_write_raw_output_marker", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await nr._persist_raw_output_marker(
            lambda: None,
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            attempt_key=None,
            marker={},
        )


async def test_write_raw_output_marker_stamps_derived_key_without_promotion():
    """FAR-438: a retry persist stamps the derived per-node key via setdefault
    (monotone — never downgrades an already-applied marker's key)."""
    row = _MarkerRunRow(_RUN_ID, idempotency_key=f"{_ORG_ID}:9")
    session = _FakeSession(_run_row_router(row))

    def _factory() -> _FakeSession:
        return session

    marker: dict[str, Any] = {"status": "failed"}
    await nr._write_raw_output_marker(
        _factory,
        org_uuid=_ORG_UUID,
        run_id=_RUN_ID,
        node_id="n1",
        attempt_key="run:run-1:node:n1:0",
        marker=marker,
    )
    from modulo.core.pipeline_engine.idempotency import node_idempotency_key

    expected = node_idempotency_key(f"{_ORG_ID}:9", "n1", index=None, payload=None)
    assert marker["idempotency_key"] == expected


async def test_write_raw_output_marker_row_missing_logs_and_skips(caplog):
    session = _FakeSession(_run_row_router(None))

    def _factory() -> _FakeSession:
        return session

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await nr._write_raw_output_marker(
            _factory,
            org_uuid=_ORG_UUID,
            run_id=_RUN_ID,
            node_id="n1",
            attempt_key=None,
            marker={},
        )
    assert any("raw_output_marker_skip_row_not_found" in m for m in caplog.messages)


def test_merge_marker_preserves_existing_idempotency_key_without_promotion():
    marker = {"status": "failed"}
    existing = {"idempotency_key": "old-key", "delivery_done": True}
    merged = nr._merge_existing_raw_output_marker(marker, existing)
    assert merged["idempotency_key"] == "old-key"
    assert merged["delivery_done"] is True


def test_merge_marker_non_dict_existing_returns_marker():
    marker = {"status": "failed"}
    assert nr._merge_existing_raw_output_marker(marker, "not-a-dict") is marker


# ---------------------------------------------------------------------------
# _read_run_raw_output_markers_for_gate — fenced read branches
# ---------------------------------------------------------------------------


async def test_read_gate_markers_requires_factory_and_claim():
    none1 = await nr._read_run_raw_output_markers_for_gate(
        None, run_id=_RUN_ID, org_id_raw=_ORG_ID, claim_lease="t", node_id="n1"
    )
    assert none1 is None
    none2 = await nr._read_run_raw_output_markers_for_gate(
        lambda: None, run_id=_RUN_ID, org_id_raw=_ORG_ID, claim_lease=None, node_id="n1"
    )
    assert none2 is None


async def test_read_gate_markers_unparseable_org_returns_none():
    got = await nr._read_run_raw_output_markers_for_gate(
        lambda: None, run_id=_RUN_ID, org_id_raw="bad-uuid", claim_lease="t", node_id="n1"
    )
    assert got is None


async def test_read_gate_markers_fenced_row_missing_returns_none():
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        r.fetchone.return_value = None
        return r

    got = await nr._read_run_raw_output_markers_for_gate(
        lambda: _FakeSession(_route), run_id=_RUN_ID, org_id_raw=_ORG_ID, claim_lease="tok", node_id="n1"
    )
    assert got is None


async def test_read_gate_markers_returns_dict_on_hit():
    markers = {"m": {"delivery_done": True}}

    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        # FAR-583 read re-point: the gate read now goes through
        # read_run_markers_fenced, which reassembles the flat markers dict from
        # the run_node_outputs rows via a single .all() (legacy column +
        # attempt_key + new-table markers). The reassembled flat dict
        # {"m": {...}} is keyed by the marker key itself.
        r.all.return_value = [(None, "m", {"delivery_done": True})]
        return r

    got = await nr._read_run_raw_output_markers_for_gate(
        lambda: _FakeSession(_route), run_id=_RUN_UUID_STR, org_id_raw=_ORG_ID, claim_lease="tok", node_id="n1"
    )
    assert got == markers


async def test_read_gate_markers_non_dict_value_returns_none():
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        r.fetchone.return_value = ("not-a-dict",)
        return r

    got = await nr._read_run_raw_output_markers_for_gate(
        lambda: _FakeSession(_route), run_id=_RUN_ID, org_id_raw=_ORG_ID, claim_lease="tok", node_id="n1"
    )
    assert got is None


async def test_read_gate_markers_db_failure_fails_open(caplog):
    session = _FakeSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        got = await nr._read_run_raw_output_markers_for_gate(
            lambda: session, run_id=_RUN_ID, org_id_raw=_ORG_ID, claim_lease="tok", node_id="n1"
        )
    assert got is None
    assert any("idempotency_gate_read_failed" in m for m in caplog.messages)


async def test_read_gate_markers_reraises_cancellation():
    session = _FakeSession()
    session.enter_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await nr._read_run_raw_output_markers_for_gate(
            lambda: session, run_id=_RUN_ID, org_id_raw=_ORG_ID, claim_lease="tok", node_id="n1"
        )


# ---------------------------------------------------------------------------
# _read_connector_idempotency_gate_state — the connector gate read
# ---------------------------------------------------------------------------


async def test_read_connector_gate_state_requires_factory_and_run_id():
    none1 = await nr._read_connector_idempotency_gate_state(None, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1")
    assert none1 == (None, None)
    none2 = await nr._read_connector_idempotency_gate_state(lambda: None, run_id="", org_id_raw=_ORG_ID, node_id="n1")
    assert none2 == (None, None)


async def test_read_connector_gate_state_unparseable_org():
    got = await nr._read_connector_idempotency_gate_state(lambda: None, run_id=_RUN_ID, org_id_raw="bad", node_id="n1")
    assert got == (None, None)


async def test_read_connector_gate_state_row_missing():
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        r.fetchone.return_value = None
        return r

    markers, key = await nr._read_connector_idempotency_gate_state(
        lambda: _FakeSession(_route), run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1"
    )
    assert markers is None
    assert key is None


async def test_read_connector_gate_state_returns_markers_and_key():
    markers = {"m": {"delivery_done": True}}

    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        # The connector gate read runs TWO statements: the run-row FOR UPDATE
        # SELECT (id, idempotency_key) consumed via .fetchone(), then the
        # fenced markers read (read_run_markers_fenced) consumed via .all().
        r.fetchone.return_value = (None, "persisted-key")
        r.all.return_value = [(None, "m", {"delivery_done": True})]
        return r

    got_markers, got_key = await nr._read_connector_idempotency_gate_state(
        lambda: _FakeSession(_route), run_id=_RUN_UUID_STR, org_id_raw=_ORG_ID, node_id="n1"
    )
    assert got_markers == markers
    assert got_key == "persisted-key"


async def test_read_connector_gate_state_non_dict_markers_none_key_kept():
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        r.fetchone.return_value = (None, "persisted-key")
        # No run_node_outputs marker rows -> the fenced reader returns None for
        # markers (the new-table representation is absent), while the persisted
        # idempotency key from the run-row read is preserved.
        r.all.return_value = []
        return r

    got_markers, got_key = await nr._read_connector_idempotency_gate_state(
        lambda: _FakeSession(_route), run_id=_RUN_UUID_STR, org_id_raw=_ORG_ID, node_id="n1"
    )
    assert got_markers is None
    assert got_key == "persisted-key"


async def test_read_connector_gate_state_db_failure_fails_open():
    session = _FakeSession()
    session.execute = AsyncMock(side_effect=RuntimeError("db down"))

    markers, key = await nr._read_connector_idempotency_gate_state(
        lambda: session, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1"
    )
    assert markers is None
    assert key is None


async def test_read_connector_gate_state_reraises_cancellation():
    session = _FakeSession()
    session.enter_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await nr._read_connector_idempotency_gate_state(
            lambda: session, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1"
        )


# ---------------------------------------------------------------------------
# _connector_write_payload_hash / canonicalisation helpers
# ---------------------------------------------------------------------------


def test_connector_payload_hash_falls_back_on_unsortable_keys():
    """A payload with mixed-type dict keys defeats json sort_keys — the fallback
    coercion (stringified keys, deterministic) must still yield a stable hash."""
    a = nr._connector_write_payload_hash(resource="command", filters={}, data={1: "a", "b": "c", "list": [1, 2]})
    b = nr._connector_write_payload_hash(resource="command", filters={}, data={"b": "c", 1: "a", "list": [1, 2]})
    assert a == b
    assert isinstance(a, str)


def test_canonicalize_sets_sorts_set_members():
    out = nr._canonicalize_sets({"tags": {"b", "a"}, "items": ({"z": 1},), "plain": 1})
    assert out["tags"] == ["a", "b"]
    assert out["items"] == [{"z": 1}]
    assert out["plain"] == 1


def test_canonical_coerce_stringifies_and_sorts():
    out = nr._canonical_coerce({2: [1, {"b": 1}], "a": {2, 1}})
    assert out["a"] == ["1", "2"]
    assert out["2"] == ["1", {"b": "1"}]


def test_canonical_scalar_default_object_renders_type_identity():
    class _Opaque:
        pass

    rendered = nr._canonical_scalar(_Opaque())
    assert "0x" not in rendered
    assert "_Opaque" in rendered


# ---------------------------------------------------------------------------
# _connector_on_unknown — defensive mode reads
# ---------------------------------------------------------------------------


def test_connector_on_unknown_missing_reader_falls_back_to_default():
    assert nr._connector_on_unknown(object(), "command") == "fail_open"


def test_connector_on_unknown_reader_raise_falls_back(caplog):
    class _Conn:
        def on_unknown_for(self, resource: str) -> str:
            raise RuntimeError("policy unreadable")

    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        mode = nr._connector_on_unknown(_Conn(), "command")
    assert mode == "fail_open"
    assert any("on_unknown_read_failed" in m for m in caplog.messages)


def test_connector_on_unknown_invalid_mode_falls_back():
    class _Conn:
        def on_unknown_for(self, resource: str) -> str:
            return "banana"

    assert nr._connector_on_unknown(_Conn(), "command") == "fail_open"


def test_connector_on_unknown_valid_mode_returned():
    class _Conn:
        def on_unknown_for(self, resource: str) -> str:
            return "fail_closed"

    assert nr._connector_on_unknown(_Conn(), "command") == "fail_closed"


# ---------------------------------------------------------------------------
# _resolve_connector_write_outcome / stamp / intent / no-delivery branches
# ---------------------------------------------------------------------------


async def test_resolve_write_outcome_success_stamps_delivery():
    stamp = AsyncMock()
    no_delivery = AsyncMock()
    with (
        patch.object(nr, "_stamp_connector_write_delivered", stamp),
        patch.object(nr, "_mark_connector_write_no_delivery", no_delivery),
        patch.object(nr, "_connector_write_reported_failure", return_value=False),
    ):
        await nr._resolve_connector_write_outcome(
            lambda: None,
            connector=MagicMock(),
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="command",
            filters={},
            data={},
            result={"ok": True},
            intent_active=True,
        )
    stamp.assert_awaited_once()
    no_delivery.assert_not_awaited()


async def test_resolve_write_outcome_reported_failure_marks_no_delivery():
    stamp = AsyncMock()
    no_delivery = AsyncMock()
    with (
        patch.object(nr, "_stamp_connector_write_delivered", stamp),
        patch.object(nr, "_mark_connector_write_no_delivery", no_delivery),
        patch.object(nr, "_connector_write_reported_failure", return_value=True),
    ):
        await nr._resolve_connector_write_outcome(
            lambda: None,
            connector=MagicMock(),
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="command",
            filters={},
            data={},
            result={"failed": True},
            intent_active=True,
        )
    no_delivery.assert_awaited_once()
    stamp.assert_not_awaited()


async def test_stamp_connector_write_delivered_no_context_returns():
    none_factory = await nr._stamp_connector_write_delivered(
        None, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1", resource="r", filters={}, data={}
    )
    no_run_id = await nr._stamp_connector_write_delivered(
        lambda: None, run_id="", org_id_raw=_ORG_ID, node_id="n1", resource="r", filters={}, data={}
    )
    assert none_factory is None
    assert no_run_id is None


async def test_stamp_connector_write_delivered_reraises_cancellation():
    with (
        patch.object(nr, "_persist_raw_output_marker", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await nr._stamp_connector_write_delivered(
            lambda: None, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1", resource="r", filters={}, data={}
        )


async def test_stamp_connector_write_delivered_lost_marker_warns(caplog):
    with (
        patch.object(nr, "_persist_raw_output_marker", new=AsyncMock(return_value=False)),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        await nr._stamp_connector_write_delivered(
            lambda: None, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1", resource="r", filters={}, data={}
        )
    assert any("idempotency_marker_lost" in m for m in caplog.messages)


async def test_persist_connector_write_intent_reraises_cancellation():
    with (
        patch.object(nr, "_persist_raw_output_marker", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await nr._persist_connector_write_intent(
            lambda: None, run_id=_RUN_ID, org_id_raw=_ORG_ID, node_id="n1", resource="r", filters={}, data={}
        )


async def test_mark_connector_write_no_delivery_reraises_cancellation():
    with (
        patch.object(nr, "_persist_raw_output_marker", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await nr._mark_connector_write_no_delivery(
            lambda: None,
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="r",
            filters={},
            data={},
            reason="reported_failure",
        )


# ---------------------------------------------------------------------------
# _connector_write_gate — suppressed / fail-open / ValueError branches
# ---------------------------------------------------------------------------


def _enable_write_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "modulo.settings.get_settings",
        lambda: SimpleNamespace(modulo_connector_write_gate_enabled=True),
    )


async def test_gate_returns_none_without_persisted_key(monkeypatch: pytest.MonkeyPatch):
    _enable_write_gate(monkeypatch)
    with patch.object(nr, "_read_connector_idempotency_gate_state", new=AsyncMock(return_value=({"m": {}}, None))):
        gate = await nr._connector_write_gate(
            lambda: None,
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="r",
            filters={},
            data={},
        )
    assert gate is None


async def test_gate_suppression_valueerror_fails_open(monkeypatch: pytest.MonkeyPatch):
    _enable_write_gate(monkeypatch)
    with (
        patch.object(nr, "_read_connector_idempotency_gate_state", new=AsyncMock(return_value=({"m": {}}, "key"))),
        patch.object(nr, "read_before_write_suppression", side_effect=ValueError("bad marker")),
    ):
        gate = await nr._connector_write_gate(
            lambda: None,
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="r",
            filters={},
            data={},
        )
    assert gate is None


async def test_gate_ambiguous_valueerror_fails_open_fail_closed(monkeypatch: pytest.MonkeyPatch):
    _enable_write_gate(monkeypatch)
    with (
        patch.object(nr, "_read_connector_idempotency_gate_state", new=AsyncMock(return_value=({"m": {}}, "key"))),
        patch.object(nr, "read_before_write_suppression", return_value=False),
        patch.object(nr, "read_before_write_ambiguous", side_effect=ValueError("bad marker")),
    ):
        gate = await nr._connector_write_gate(
            lambda: None,
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="r",
            filters={},
            data={},
            on_unknown="fail_closed",
        )
    assert gate is None


async def test_gate_fail_closed_suppresses_ambiguous(monkeypatch: pytest.MonkeyPatch):
    _enable_write_gate(monkeypatch)
    with (
        patch.object(nr, "_read_connector_idempotency_gate_state", new=AsyncMock(return_value=({"m": {}}, "key"))),
        patch.object(nr, "read_before_write_suppression", return_value=False),
        patch.object(nr, "read_before_write_ambiguous", return_value=True),
    ):
        gate = await nr._connector_write_gate(
            lambda: None,
            run_id=_RUN_ID,
            org_id_raw=_ORG_ID,
            node_id="n1",
            resource="r",
            filters={},
            data={},
            on_unknown="fail_closed",
        )
    assert gate is not None
    output_json = gate["artifacts"][0]["output"]["output_json"]
    assert output_json["delivery_done"] is False
    assert output_json["idempotency_gate"] == "connector_write_fail_closed"


# ---------------------------------------------------------------------------
# _run_conformance_gate — context fast-paths + audit cancellation
# ---------------------------------------------------------------------------


async def test_conformance_gate_no_session_factory_fast_path(monkeypatch: pytest.MonkeyPatch):
    ctx = (None, _ORG_ID, None, "pipe-1", None, False)
    monkeypatch.setattr(nr, "get_conformance_ctx", lambda: ctx)
    check = AsyncMock()
    monkeypatch.setattr("modulo.core.guardrails.conformance.check_node_start", check)
    blocked = await nr._run_conformance_gate({}, node_id="n1")
    assert blocked is False
    check.assert_not_awaited()


async def test_conformance_gate_empty_pipeline_id_fast_path(monkeypatch: pytest.MonkeyPatch):
    ctx = (lambda: None, _ORG_ID, None, "", None, False)
    monkeypatch.setattr(nr, "get_conformance_ctx", lambda: ctx)
    check = AsyncMock()
    monkeypatch.setattr("modulo.core.guardrails.conformance.check_node_start", check)
    blocked = await nr._run_conformance_gate({}, node_id="n1")
    assert blocked is False
    check.assert_not_awaited()


async def test_conformance_gate_unparseable_pipeline_fast_path(monkeypatch: pytest.MonkeyPatch):
    ctx = (lambda: None, _ORG_ID, None, "not-a-uuid", None, False)
    monkeypatch.setattr(nr, "get_conformance_ctx", lambda: ctx)
    check = AsyncMock()
    monkeypatch.setattr("modulo.core.guardrails.conformance.check_node_start", check)
    blocked = await nr._run_conformance_gate({}, node_id="n1")
    assert blocked is False
    check.assert_not_awaited()


async def test_conformance_audit_reraises_cancellation(monkeypatch: pytest.MonkeyPatch):
    from modulo.core import audit_logger

    async def _cancelled(*args: Any, **kwargs: Any) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(audit_logger, "append_audit_event", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await nr._append_conformance_audit(
            lambda: _FakeSession(),
            org_id=_ORG_UUID,
            run_id=str(uuid.uuid4()),
            node_id="n1",
            detail="d",
            state="absent",
            event_type="guardrail.conformance_blocked_midrun",
        )


# ---------------------------------------------------------------------------
# _render_agent_prompt / _invoke_node_model / _finalize_node_result
# ---------------------------------------------------------------------------


def test_render_agent_prompt_resolved_parameters_in_template():
    node_def = {"id": "n1", "_resolved_parameters": {"topic": "deploy"}}
    rendered, mode = nr._render_agent_prompt(
        state={},
        run_context={},
        raw_input={"task": "x"},
        prompt_template="topic={{ parameter.topic }} task={{ input.task }}",
        node_def=node_def,
    )
    assert "topic=deploy" in rendered
    assert "task=x" in rendered
    assert mode is None


def test_render_agent_prompt_llm_routing_appends_prompt():
    node_def = {"id": "n1", "routing_mode": "llm", "routing_prompt": "Pick a route."}
    rendered, mode = nr._render_agent_prompt(
        state={}, run_context={}, raw_input=None, prompt_template="base", node_def=node_def
    )
    assert mode == "llm"
    assert rendered.endswith("Pick a route.")


def test_render_agent_prompt_llm_routing_no_prompt_no_append():
    node_def = {"id": "n1", "routing_mode": "llm", "routing_prompt": ""}
    rendered, mode = nr._render_agent_prompt(
        state={}, run_context={}, raw_input=None, prompt_template="base", node_def=node_def
    )
    assert mode == "llm"
    assert rendered == "base"


async def test_invoke_node_model_without_hub_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("modulo.core.pipeline_engine.decorator.get_model_backend_hub", lambda: None)
    with pytest.raises(RuntimeError, match="ModelBackendHub not available"):
        await nr._invoke_node_model("prompt", "11111111-2222-3333-4444-555555555555", "n1")


def test_finalize_node_result_validates_schema_and_extracts_route():
    schema = {"required": ["verdict"]}
    result = nr._finalize_node_result("n1", {"verdict": "ok", "_next_node": "b"}, schema, "llm")
    assert result["artifacts"][0]["status"] == "completed"
    assert result["_llm_next_node"] == "b"
    assert "_next_node" not in result["output"]


def test_finalize_node_result_schema_violation_raises():
    with pytest.raises(nr.OutputSchemaValidationError):
        nr._finalize_node_result("n1", {"verdict": "ok"}, {"required": ["missing_field"]}, None)


# ---------------------------------------------------------------------------
# LLM judge — non-JSON model response falls back to a failed eval
# ---------------------------------------------------------------------------


def _judge_backend_id() -> str:
    return "11111111-2222-3333-4444-555555555555"


def test_llm_judge_non_json_response_falls_back(monkeypatch: pytest.MonkeyPatch):
    judge = nr._build_llm_judge_callable(MagicMock(), _judge_backend_id())
    monkeypatch.setattr(nr, "_run_coroutine_sync", lambda coro: SimpleNamespace(content="model said: no"))
    result = judge({"answer": "hello"}, SimpleNamespace(config={"field": "answer"}))
    assert result["passed"] is False
    assert result["score"] == 0.0
    assert "model said: no" in result["detail"]


def test_llm_judge_parses_structured_response(monkeypatch: pytest.MonkeyPatch):
    judge = nr._build_llm_judge_callable(MagicMock(), _judge_backend_id())
    monkeypatch.setattr(
        nr,
        "_run_coroutine_sync",
        lambda coro: SimpleNamespace(content='{"passed": true, "score": 0.9, "detail": "good"}'),
    )
    result = judge({"answer": "hello"}, SimpleNamespace(config={"field": "answer"}))
    assert result["passed"] is True
    assert result["score"] == pytest.approx(0.9)
    assert result["detail"] == "good"


# ---------------------------------------------------------------------------
# Gate-eval envelope / target resolution (FAR-311)
# ---------------------------------------------------------------------------


def test_gate_eval_envelope_falls_back_to_state_output():
    state = {"output": {"k": 1}, "artifacts": [{"node_id": "n1", "status": "completed"}]}
    envelope = nr._resolve_gate_eval_envelope(state, "n1")
    assert envelope["output"] == {"k": 1}
    assert envelope["artifacts"] == [{"node_id": "n1", "status": "completed"}]


def test_gate_eval_envelope_no_matching_artifact_uses_state():
    state = {"output": {"k": 1}, "artifacts": [{"node_id": "other", "output": {"z": 2}}]}
    envelope = nr._resolve_gate_eval_envelope(state, "n1")
    assert envelope["output"] == {"k": 1}


def test_gate_eval_envelope_empty_state():
    envelope = nr._resolve_gate_eval_envelope({}, "n1")
    assert not envelope


def test_gate_eval_target_non_splittable_returns_state():
    state = {"output": {"k": 1}}
    assert nr._resolve_gate_eval_target(state, "n1", {"n1": "router"}) is state


def test_gate_eval_target_no_type_map_returns_state():
    state = {"output": {"k": 1}}
    assert nr._resolve_gate_eval_target(state, "n1", None) is state


def test_gate_eval_target_no_evidence_returns_state():
    state = {"artifacts": []}
    assert nr._resolve_gate_eval_target(state, "n1", {"n1": "agent"}) is state


def test_gate_eval_target_contract_found():
    state = {"artifacts": [{"node_id": "n1", "status": "completed", "output": {"output_json": {"pr_url": "u"}}}]}
    target = nr._resolve_gate_eval_target(state, "n1", {"n1": "sandbox_agent"})
    assert target == {"pr_url": "u"}


def test_gate_eval_target_missing_contract_falls_back():
    state = {"artifacts": [{"node_id": "n1", "status": "completed", "output": {}}]}
    target = nr._resolve_gate_eval_target(state, "n1", {"n1": "sandbox_agent"})
    assert target is state


# ---------------------------------------------------------------------------
# Reject-correction dispatch (FAR-210 follow-up)
# ---------------------------------------------------------------------------


async def test_reject_correction_inner_dispatches():
    dispatch = AsyncMock()
    with patch("modulo.core.feedback_manager.dispatch_reject_correction", dispatch):
        await nr._dispatch_reject_correction_inner(
            "factory", "org", "run-1", "node-1", {"k": 1}, {"action": "rejected", "reason": "bad"}, "gate-1"
        )
    kwargs = dispatch.await_args.kwargs
    assert kwargs["node_id"] == "node-1"
    assert kwargs["rejection_reason"] == "bad"
    assert kwargs["gate_id"] == "gate-1"


async def test_reject_correction_inner_reraises_cancellation():
    dispatch = AsyncMock(side_effect=asyncio.CancelledError())
    with (
        patch("modulo.core.feedback_manager.dispatch_reject_correction", dispatch),
        pytest.raises(asyncio.CancelledError),
    ):
        await nr._dispatch_reject_correction_inner(
            "factory", "org", "run-1", "node-1", {}, {"action": "rejected"}, "gate-1"
        )


async def test_reject_correction_inner_swallows_db_error(caplog):
    dispatch = AsyncMock(side_effect=RuntimeError("db down"))
    with (
        patch("modulo.core.feedback_manager.dispatch_reject_correction", dispatch),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        await nr._dispatch_reject_correction_inner(
            "factory", "org", "run-1", "node-1", {}, {"action": "rejected"}, "gate-1"
        )
    assert any("reject_correction_dispatch_failed" in m for m in caplog.messages)


async def test_reject_correction_best_effort_dispatches_on_reject():
    inner = AsyncMock()
    with patch.object(nr, "_dispatch_reject_correction_inner", inner):
        await nr._dispatch_reject_correction_best_effort(
            {"_run_id": "run-1", "output": {"k": 1}},
            {"action": "rejected"},
            "gate-1",
            {"correction_target": "node-1"},
            "factory",
            "org",
        )
    inner.assert_awaited_once()


async def test_reject_correction_best_effort_ignores_non_reject_or_missing_context():
    inner = AsyncMock()
    with patch.object(nr, "_dispatch_reject_correction_inner", inner):
        await nr._dispatch_reject_correction_best_effort(
            {"_run_id": "run-1", "output": {"k": 1}},
            {"action": "approved"},
            "gate-1",
            {"correction_target": "node-1"},
            "factory",
            "org",
        )
        await nr._dispatch_reject_correction_best_effort(
            {"_run_id": "run-1", "output": {"k": 1}}, {"action": "rejected"}, "gate-1", {}, "factory", "org"
        )
        await nr._dispatch_reject_correction_best_effort(
            {"_run_id": "run-1", "output": "not-a-dict"},
            {"action": "rejected"},
            "gate-1",
            {"correction_target": "node-1"},
            "factory",
            "org",
        )
    inner.assert_not_awaited()


# ---------------------------------------------------------------------------
# _persist_gate_eval_results — cancellation / failure boundaries
# ---------------------------------------------------------------------------


def _gate_eval_fixtures(node_id: str | None):
    from modulo.core.eval_engine import EvalDefinition, EvalResult, EvalType

    eval_def = EvalDefinition(
        id=uuid.uuid4(),
        org_id=_ORG_UUID,
        node_id=node_id,
        name="gate-eval",
        eval_type=EvalType.REGEX,
        config={},
    )
    eval_result = EvalResult(run_id=uuid.uuid4(), node_id="n1", eval_id=eval_def.id, passed=True, score=1.0, detail="")
    return [eval_def], {eval_def.name: eval_result}


async def test_persist_gate_eval_results_reraises_cancellation():
    session = _FakeSession()
    session.add = lambda obj: (_ for _ in ()).throw(asyncio.CancelledError())
    eval_defs, results = _gate_eval_fixtures(str(uuid.uuid4()))

    with pytest.raises(asyncio.CancelledError):
        await nr._persist_gate_eval_results({"_run_id": _RUN_ID}, eval_defs, results, lambda: session, _ORG_UUID)


async def test_persist_gate_eval_results_swallows_db_error():
    session = _FakeSession()
    session.add = lambda obj: (_ for _ in ()).throw(RuntimeError("db down"))
    eval_defs, results = _gate_eval_fixtures(None)

    await nr._persist_gate_eval_results({"_run_id": _RUN_ID}, eval_defs, results, lambda: session, _ORG_UUID)
    assert not session.added


async def test_persist_gate_eval_results_skips_without_run_id():
    session = _FakeSession()
    eval_defs, results = _gate_eval_fixtures("n1")
    await nr._persist_gate_eval_results({}, eval_defs, results, lambda: session, _ORG_UUID)
    assert not session.added


# ---------------------------------------------------------------------------
# Connector node (make_connector_fn) — resolution, scope, action, outcome
# ---------------------------------------------------------------------------


class _StubConnector:
    async def query(self, q: Any) -> Any:
        return {"records": ["r1"]}

    async def write(self, payload: Any) -> Any:
        return {"delivered": True}


class _StubHub:
    def __init__(self, *, connector: Any = None, raise_on_get: Exception | None = None) -> None:
        self._connector = connector if connector is not None else _StubConnector()
        self._raise_on_get = raise_on_get
        self.resolved: list[uuid.UUID] = []

    def get(self, instance_id: uuid.UUID) -> Any:
        if self._raise_on_get is not None:
            raise self._raise_on_get
        self.resolved.append(instance_id)
        return self._connector


def _connector_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": "conn-node",
        "connector_binding": {"instance_id": str(uuid.uuid4()), "type": "github", "operation": "query"},
    }
    node_def.update(overrides)
    return node_def


def test_resolve_binding_connector_no_hub_returns_note_artifact(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("modulo.core.pipeline_engine.decorator.get_connector_hub", lambda: None)
    connector, error = nr._resolve_binding_connector({"instance_id": str(uuid.uuid4())}, "n1")
    assert connector is None
    assert error["artifacts"][0]["output"] == {"note": "no connector hub"}
    assert error["artifacts"][0]["status"] == "executed"


def test_resolve_binding_connector_missing_instance_id(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("modulo.core.pipeline_engine.decorator.get_connector_hub", lambda: _StubHub())
    connector, error = nr._resolve_binding_connector({}, "n1")
    assert connector is None
    assert error["artifacts"][0]["error"] == "no connector instance_id"


def test_resolve_binding_connector_scope_denied(monkeypatch: pytest.MonkeyPatch):
    hub = _StubHub()
    monkeypatch.setattr("modulo.core.pipeline_engine.decorator.get_connector_hub", lambda: hub)
    connector, error = nr._resolve_binding_connector(
        {"instance_id": str(uuid.uuid4()), "type": "github"}, "n1", allowed_connectors=["slack"]
    )
    assert connector is None
    assert "scope.violation" in error["artifacts"][0]["error"]
    assert not hub.resolved


def test_resolve_binding_connector_hub_get_raises(monkeypatch: pytest.MonkeyPatch):
    hub = _StubHub(raise_on_get=RuntimeError("unresolvable"))
    monkeypatch.setattr("modulo.core.pipeline_engine.decorator.get_connector_hub", lambda: hub)
    connector, error = nr._resolve_binding_connector({"instance_id": str(uuid.uuid4()), "type": "github"}, "n1")
    assert connector is None
    assert "connector error" in error["artifacts"][0]["error"]


def test_resolve_binding_connector_success(monkeypatch: pytest.MonkeyPatch):
    hub = _StubHub()
    monkeypatch.setattr("modulo.core.pipeline_engine.decorator.get_connector_hub", lambda: hub)
    connector, error = nr._resolve_binding_connector({"instance_id": str(uuid.uuid4()), "type": "github"}, "n1")
    assert connector is hub._connector
    assert error is None


def test_connector_inputs_merges_run_input_and_defaults_provider_ref():
    state = {"run_context": {"input": {"q": "abc", "data_only": "v"}}}
    resource, filters, data = nr._connector_inputs(
        {"resource": "search", "filters": {}, "data": {"data_only": "v"}}, state
    )
    assert resource == "search"
    assert filters["q"] == "abc"
    assert data["data_only"] == "v"
    assert filters["provider_ref"] == "/"


def test_connector_inputs_non_dict_input_leaves_binding_values():
    state = {"run_context": {"input": "raw-string"}}
    resource, filters, _data = nr._connector_inputs(
        {"resource": "command", "filters": {"provider_ref": "/x"}, "data": {}}, state
    )
    assert resource == "command"
    assert filters["provider_ref"] == "/x"


def test_enforce_connector_scope_missing_instance_id_allowed():
    assert nr._enforce_connector_scope({}, "n1", "github", ["github"]) is None


def test_enforce_connector_scope_violation_returns_artifact():
    block = nr._enforce_connector_scope({"instance_id": str(uuid.uuid4())}, "n1", "github", ["slack"])
    assert block is not None
    assert "scope.violation" in block["artifacts"][0]["error"]


def test_enforce_connector_scope_in_scope_returns_none():
    assert nr._enforce_connector_scope({"instance_id": str(uuid.uuid4())}, "n1", "github", ["github"]) is None


async def test_run_connector_action_write_builds_payload():
    connector = MagicMock()
    connector.write = AsyncMock(return_value={"ok": 1})
    result = await nr._run_connector_action(connector, "write", "command", {"provider_ref": "/"}, {"cmd": "ls"})
    assert result == {"ok": 1}
    payload = connector.write.await_args.args[0]
    assert payload.resource == "command"
    assert payload.data == {"cmd": "ls"}


async def test_run_connector_action_query_builds_query():
    connector = MagicMock()
    connector.query = AsyncMock(return_value={"records": []})
    result = await nr._run_connector_action(connector, "query", "search", {"q": "x"}, {})
    assert result == {"records": []}
    query = connector.query.await_args.args[0]
    assert query.resource == "search"
    assert query.filters == {"q": "x"}


def test_guard_connector_secret_output_violation(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.capability_scope import ScopeViolationError

    def _boom(result: Any, node_id: str) -> None:
        raise ScopeViolationError(node_id=node_id, target="secret-object", kind="secret")

    monkeypatch.setattr("modulo.core.capability_scope.assert_no_secret_objects", _boom)
    block = nr._guard_connector_secret_output({"leak": True}, "n1")
    assert block is not None
    assert "scope.violation" in block["artifacts"][0]["error"]


def test_guard_connector_secret_output_clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("modulo.core.capability_scope.assert_no_secret_objects", lambda result, node_id: None)
    assert nr._guard_connector_secret_output({"ok": True}, "n1") is None


async def test_connector_node_no_hub_returns_note():
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    set_connector_hub(None)
    try:
        fn = make_connector_fn(_connector_node_def())
        result = await fn({"run_context": {"input": {}}})
        assert result["artifacts"][0]["status"] == "executed"
        assert result["artifacts"][0]["output"] == {"note": "no connector hub"}
    finally:
        set_connector_hub(None)


async def test_connector_node_missing_instance_id_fails():
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def()
    node_def["connector_binding"] = {"type": "github"}
    set_connector_hub(_StubHub())
    try:
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}})
        assert result["artifacts"][0]["status"] == "failed"
        assert result["artifacts"][0]["error"] == "no connector instance_id"
    finally:
        set_connector_hub(None)


async def test_connector_node_query_success_completes():
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    set_connector_hub(_StubHub())
    try:
        fn = make_connector_fn(_connector_node_def())
        result = await fn({"run_context": {"input": {"q": "x"}}})
        assert result["artifacts"][0]["status"] == "completed"
        assert result["output"] == {"records": ["r1"]}
    finally:
        set_connector_hub(None)


async def test_connector_node_connector_raise_fails_node(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    class _FailingConnector:
        async def query(self, q: Any) -> Any:
            raise RuntimeError("boom")

    set_connector_hub(_StubHub(connector=_FailingConnector()))
    outcome = AsyncMock()
    monkeypatch.setattr(nr, "_resolve_connector_write_outcome", outcome)
    try:
        fn = make_connector_fn(_connector_node_def())
        result = await fn({"run_context": {"input": {}}})
        assert result["artifacts"][0]["status"] == "failed"
        assert "boom" in result["artifacts"][0]["error"]
        # QA Fix 1: the raised error is classified by the SINGLE authority even
        # for a query op (the resolution is fail-open with no session factory).
        outcome.assert_awaited_once()
        assert isinstance(outcome.await_args.kwargs["exception"], RuntimeError)
    finally:
        set_connector_hub(None)


async def test_connector_node_write_success_stamps_and_completes(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def()
    node_def["connector_binding"]["operation"] = "write"
    set_connector_hub(_StubHub())
    resolve = AsyncMock()
    monkeypatch.setattr(nr, "_resolve_connector_write_outcome", resolve)
    try:
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}, "_run_id": _RUN_ID, "_org_id": _ORG_ID})
        assert result["artifacts"][0]["status"] == "completed"
        resolve.assert_awaited_once()
        # The refactor's wrapper always forwards ``exception=`` explicitly
        # (None on success) — the success path must not CLASSIFY an exception.
        assert resolve.await_args.kwargs.get("exception") is None
    finally:
        set_connector_hub(None)


async def test_connector_node_write_raise_resolves_ambiguous(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    class _RaisingConnector:
        async def write(self, payload: Any) -> Any:
            raise RuntimeError("write exploded")

    node_def = _connector_node_def()
    node_def["connector_binding"]["operation"] = "write"
    set_connector_hub(_StubHub(connector=_RaisingConnector()))
    resolve = AsyncMock()
    monkeypatch.setattr(nr, "_resolve_connector_write_outcome", resolve)
    try:
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}, "_run_id": _RUN_ID, "_org_id": _ORG_ID})
        assert result["artifacts"][0]["status"] == "failed"
        resolve.assert_awaited_once()
        assert isinstance(resolve.await_args.kwargs["exception"], RuntimeError)
    finally:
        set_connector_hub(None)


async def test_connector_node_write_gate_suppresses(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def()
    node_def["connector_binding"]["operation"] = "write"
    hub = _StubHub()
    hub._connector.on_unknown_for = lambda resource: "fail_open"
    set_connector_hub(hub)

    class _S:
        modulo_connector_write_gate_enabled = True

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())
    suppressed = nr._idempotency_gate_skipped_envelope("conn-node")
    with (
        patch.object(nr, "_connector_write_gate", new=AsyncMock(return_value=suppressed)),
        patch.object(nr, "_run_connector_action", new=AsyncMock()) as action,
    ):
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}, "_run_id": _RUN_ID, "_org_id": _ORG_ID})
    assert result is suppressed
    action.assert_not_awaited()
    set_connector_hub(None)


async def test_connector_node_write_intent_persisted_before_write(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def()
    node_def["connector_binding"]["operation"] = "write"
    connector = _StubConnector()
    connector.on_unknown_for = lambda resource: "fail_open"
    connector.write = AsyncMock(return_value={"delivered": True})
    set_connector_hub(_StubHub(connector=connector))

    class _S:
        modulo_connector_write_gate_enabled = True

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())
    with (
        patch.object(nr, "_connector_write_gate", new=AsyncMock(return_value=None)),
        patch.object(nr, "_persist_connector_write_intent", new=AsyncMock()) as intent,
        patch.object(nr, "_stamp_connector_write_delivered", new=AsyncMock()) as stamp,
    ):
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}, "_run_id": _RUN_ID, "_org_id": _ORG_ID})
    assert result["artifacts"][0]["status"] == "completed"
    intent.assert_awaited_once()
    stamp.assert_awaited_once()


async def test_connector_node_write_intent_persist_failure_never_fails_node(caplog, monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def()
    node_def["connector_binding"]["operation"] = "write"
    set_connector_hub(_StubHub())

    class _S:
        modulo_connector_write_gate_enabled = True

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())
    with (
        patch.object(nr, "_connector_write_gate", new=AsyncMock(return_value=None)),
        patch.object(nr, "_persist_connector_write_intent", new=AsyncMock(side_effect=RuntimeError("intent db down"))),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}, "_run_id": _RUN_ID, "_org_id": _ORG_ID})
    assert result["artifacts"][0]["status"] == "completed"
    assert any("connector_write_intent_persist_failed" in m for m in caplog.messages)
    set_connector_hub(None)


async def test_connector_node_secret_guard_blocks_output(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.capability_scope import ScopeViolationError
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    def _boom(result: Any, node_id: str) -> None:
        raise ScopeViolationError(node_id=node_id, target="leak", kind="secret")

    monkeypatch.setattr("modulo.core.capability_scope.assert_no_secret_objects", _boom)
    set_connector_hub(_StubHub())
    try:
        fn = make_connector_fn(_connector_node_def())
        result = await fn({"run_context": {"input": {}}})
        assert result["artifacts"][0]["status"] == "failed"
        assert "scope.violation" in result["artifacts"][0]["error"]
    finally:
        set_connector_hub(None)


async def test_connector_node_scope_violation_fails_before_hub():
    """A node whose capability_scope excludes its bound connector fails FAST —
    the hub is never consulted (deny-by-default, FAR-418)."""
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def(capability_scope={"allowed_connectors": ["slack"]})
    hub = _StubHub()
    set_connector_hub(hub)
    try:
        fn = make_connector_fn(node_def)
        result = await fn({"run_context": {"input": {}}})
        assert result["artifacts"][0]["status"] == "failed"
        assert "scope.violation" in result["artifacts"][0]["error"]
        assert not hub.resolved
    finally:
        set_connector_hub(None)


async def test_connector_node_write_intent_persist_reraises_cancellation(monkeypatch: pytest.MonkeyPatch):
    from modulo.core.pipeline_engine.decorator import set_connector_hub

    node_def = _connector_node_def()
    node_def["connector_binding"]["operation"] = "write"
    set_connector_hub(_StubHub())

    class _S:
        modulo_connector_write_gate_enabled = True

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())
    with (
        patch.object(nr, "_connector_write_gate", new=AsyncMock(return_value=None)),
        patch.object(nr, "_persist_connector_write_intent", new=AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        fn = make_connector_fn(node_def)
        with pytest.raises(asyncio.CancelledError):
            await fn({"run_context": {"input": {}}, "_run_id": _RUN_ID, "_org_id": _ORG_ID})
    set_connector_hub(None)


# ---------------------------------------------------------------------------
# _wait_command_with_idle_watchdog — kill-failure branch
# ---------------------------------------------------------------------------


async def test_idle_watchdog_kill_failure_still_reports_stall(caplog):
    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.TimeoutError)
    handle.kill = AsyncMock(side_effect=RuntimeError("kill failed"))

    with caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"):
        result, reason = await _wait_command_with_idle_watchdog(
            handle,
            total_timeout=1.0,
            idle_timeout=5.0,
            last_activity=lambda: 0.0,
            tick_interval=0.01,
        )
    assert result is None
    assert "agent produced no output for 5s" in reason
    assert any("idle_watchdog_kill_failed" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# _path_matches_any_glob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "globs", "expected"),
    [
        ("/home/user/build.log", ["*.log"], True),
        ("/home/user/build.log", ["/home/user/*.log"], True),
        ("/home/user/out/result.json", ["output.json"], False),
        ("/home/user/out/result.json", ["/home/user/out/"], True),
        ("/home/user/abc", ["/home/user/a?c"], True),
        ("/home/user/abcd", ["/home/user/a?c"], False),
        ("/home/user/x.py", ["/home/user/*.json"], False),
        ("plain.json", ["plain.json"], True),
    ],
    ids=[
        "star-suffix",
        "dir-glob",
        "basename-only-miss",
        "trailing-slash",
        "question-mark",
        "question-mark-miss",
        "different-ext",
        "bare-basename",
    ],
)
def test_path_matches_any_glob(path: str, globs: list[str], expected: bool):
    assert nr._path_matches_any_glob(path, globs) is expected


def test_path_matches_any_glob_basename_match():
    assert nr._path_matches_any_glob("/home/user/out/build.log", ["build.log"]) is True


# ---------------------------------------------------------------------------
# _sandbox_resolve_secret_ref — generic resolver failure
# ---------------------------------------------------------------------------


async def test_sandbox_secret_ref_resolver_error_returns_none(caplog, monkeypatch: pytest.MonkeyPatch):
    class _S:
        fernet_key = "b" * 44

    monkeypatch.setattr("modulo.settings.get_settings", lambda: _S())
    monkeypatch.setattr(
        "modulo.core.secrets_backend.create_secrets_backend",
        MagicMock(side_effect=RuntimeError("vault unavailable")),
    )
    with caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"):
        value = await nr._sandbox_resolve_secret_ref(
            "MY_SECRET", session_factory=lambda: _FakeSession(), org_id=_ORG_ID
        )
    assert value is None
    assert any("env_var.secret_resolve_error" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# Dispatch marker / lease / api-key / clear helpers — org parsing + denial
# ---------------------------------------------------------------------------


async def test_acquire_dispatch_marker_unparseable_org_falls_back_to_token_key():
    key = await nr._sandbox_acquire_dispatch_marker(
        session_factory=lambda: _FakeSession(), claim_lease="tok", org_id="bad-org", run_id=_RUN_ID, node_id="n1"
    )
    assert key.startswith(f"run:{_RUN_ID}:node:n1:")
    assert key != f"run:{_RUN_ID}:node:n1:claim-unknown"


async def test_acquire_dispatch_marker_org_none_falls_back_to_token_key():
    key = await nr._sandbox_acquire_dispatch_marker(
        session_factory=lambda: _FakeSession(), claim_lease="tok", org_id="", run_id=_RUN_ID, node_id="n1"
    )
    assert key == f"run:{_RUN_ID}:node:n1:{nr._claim_token_attempt_suffix('tok')}"


async def test_acquire_dispatch_marker_update_denied_returns_none():
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        if "claim_count" in stmt_text:
            r.fetchone.return_value = (3,)
        else:
            r.fetchone.return_value = None
        return r

    key = await nr._sandbox_acquire_dispatch_marker(
        session_factory=lambda: _FakeSession(_route), claim_lease="tok", org_id=_ORG_ID, run_id=_RUN_ID, node_id="n1"
    )
    assert key is None


async def test_store_dispatch_marker_sandbox_unparseable_org_noop():
    session = _FakeSession()
    await nr._sandbox_store_dispatch_marker_sandbox(
        "sbx-1",
        session_factory=lambda: session,
        claim_lease="tok",
        org_id="bad",
        run_id=_RUN_ID,
        attempt_key=None,
    )
    assert not session.executed


async def test_store_script_lease_unparseable_org_noop():
    session = _FakeSession()
    await nr._sandbox_store_script_lease(
        session_factory=lambda: session, claim_lease="tok", org_id="bad", run_id=_RUN_ID, attempt_key=None
    )
    assert not session.executed


async def test_clear_dispatch_marker_unparseable_org_noop():
    session = _FakeSession()
    await nr._sandbox_clear_dispatch_marker(
        session_factory=lambda: session, claim_lease="tok", org_id="bad", run_id=_RUN_ID
    )
    assert not session.executed


async def test_mint_run_api_key_unparseable_org_returns_none():
    value = await nr._sandbox_mint_run_api_key_for_sandbox(
        session_factory=lambda: _FakeSession(), org_id="bad", run_id=_RUN_UUID_STR, node_id="n1", sandbox_timeout=60
    )
    assert value is None


async def test_mint_run_api_key_falls_back_to_first_admin(monkeypatch: pytest.MonkeyPatch):
    admin_id = uuid.uuid4()

    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        if "org_memberships" in stmt_text:
            r.fetchone.return_value = (str(admin_id),)
        else:
            r.fetchone.return_value = None
        return r

    session = _FakeSession(_route)
    minted = AsyncMock(return_value=(MagicMock(), "raw-key"))
    monkeypatch.setattr("modulo.auth.api_key.mint_run_api_key", minted)
    monkeypatch.setattr("modulo.db.crud.run.get_run_api_key_ttl_seconds", AsyncMock(return_value=900))
    value = await nr._sandbox_mint_run_api_key_for_sandbox(
        session_factory=lambda: session, org_id=_ORG_ID, run_id=_RUN_UUID_STR, node_id="n1", sandbox_timeout=60
    )
    assert value == "raw-key"
    assert minted.await_args.kwargs["account_id"] == admin_id


async def test_mint_run_api_key_no_account_returns_none(monkeypatch: pytest.MonkeyPatch):
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        r.fetchone.return_value = None
        return r

    session = _FakeSession(_route)
    monkeypatch.setattr("modulo.auth.api_key.mint_run_api_key", AsyncMock())
    monkeypatch.setattr("modulo.db.crud.run.get_run_api_key_ttl_seconds", AsyncMock(return_value=900))
    value = await nr._sandbox_mint_run_api_key_for_sandbox(
        session_factory=lambda: session, org_id=_ORG_ID, run_id=_RUN_UUID_STR, node_id="n1", sandbox_timeout=60
    )
    assert value is None


async def test_mint_run_api_key_reraises_cancellation(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "modulo.db.crud.run.get_run_api_key_ttl_seconds", AsyncMock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await nr._sandbox_mint_run_api_key_for_sandbox(
            session_factory=lambda: _FakeSession(),
            org_id=_ORG_ID,
            run_id=_RUN_UUID_STR,
            node_id="n1",
            sandbox_timeout=60,
        )


async def test_mint_run_api_key_mint_failure_returns_none(monkeypatch: pytest.MonkeyPatch):
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        r.fetchone.return_value = (str(uuid.uuid4()),)
        return r

    session = _FakeSession(_route)
    monkeypatch.setattr("modulo.auth.api_key.mint_run_api_key", AsyncMock(return_value=None))
    monkeypatch.setattr("modulo.db.crud.run.get_run_api_key_ttl_seconds", AsyncMock(return_value=900))
    value = await nr._sandbox_mint_run_api_key_for_sandbox(
        session_factory=lambda: session, org_id=_ORG_ID, run_id=_RUN_UUID_STR, node_id="n1", sandbox_timeout=60
    )
    assert value is None


def test_emit_script_span_event_swallows_telemetry_failure(monkeypatch: pytest.MonkeyPatch):
    span = MagicMock()
    monkeypatch.setattr("opentelemetry.trace.get_current_span", MagicMock(side_effect=RuntimeError("otel down")))
    nr._emit_script_span_event("script.milestone", {"k": "v"})
    assert not span.add_event.called


# ---------------------------------------------------------------------------
# _SandboxWatchdog — construction, streaming, probes, budget killers
# ---------------------------------------------------------------------------


def _watchdog(
    *,
    sandbox: Any = None,
    watch_globs: list[str] | None = None,
    watch_log_path: str | None = None,
    resource_limits: dict[str, Any] | None = None,
    stdout_ratio: float | None = None,
    sandbox_mode: str = "llm",
    wallclock_budget_seconds: int | None = None,
    require_sandbox: bool = True,
) -> _SandboxWatchdog:
    stall = nr._configure_stall_detector(
        enable_heartbeat=True,
        watch_log_path=watch_log_path,
        stdout_percentage_delta=stdout_ratio,
        watch_globs=watch_globs or [],
    )
    effective_sandbox = sandbox if (sandbox is not None or not require_sandbox) else MagicMock()
    return _SandboxWatchdog(
        sandbox=effective_sandbox,
        stall=stall,
        node_id="n1",
        run_id=_RUN_ID,
        watch_log_path=watch_log_path,
        watch_globs=watch_globs or [],
        resource_limits=resource_limits,
        sandbox_mode=sandbox_mode,
        stdout_percentage_delta=stdout_ratio,
        stream_broker=None,
        drained_chunks=[],
        wallclock_budget_seconds=wallclock_budget_seconds,
        start_time=time.monotonic(),
    )


def test_watchdog_requires_sandbox():
    with pytest.raises(RuntimeError, match="Sandbox was not created before use"):
        _watchdog(require_sandbox=False)


def test_watchdog_stream_chunk_without_broker_is_noop():
    wd = _watchdog()
    wd.stream_chunk("hello", "stdout")
    assert not wd._drained_chunks


def test_watchdog_stream_chunk_empty_chunk_returns():
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    broker = RunEventBroker(uuid.uuid4())
    wd = _watchdog()
    wd._stream_broker = broker
    wd._stream_enabled = True
    wd.stream_chunk("", "stdout")
    assert not wd._activity.get("stdout_buf")


def test_watchdog_stream_chunk_closed_broker_stops_streaming():
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    broker = RunEventBroker(uuid.uuid4())
    broker.close()
    wd = _watchdog()
    wd._stream_broker = broker
    wd._stream_enabled = True
    wd.stream_chunk("hello", "stdout")
    assert not wd._activity["stdout_buf"]


def test_watchdog_stream_chunk_publish_failure_is_isolated(caplog):
    from modulo.core.pipeline_engine.event_broker import RunEventBroker

    broker = RunEventBroker(uuid.uuid4())
    with (
        patch.object(broker, "publish", MagicMock(side_effect=OSError("socket down"))),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        wd = _watchdog()
        wd._stream_broker = broker
        wd._stream_enabled = True
        wd.stream_chunk("hello", "stdout")
    assert any("stream_publish_failed" in m for m in caplog.messages)


def test_watchdog_touch_stdout_tracks_delta():
    wd = _watchdog(stdout_ratio=0.5)
    wd.touch_stdout("first chunk")
    assert "stdout" in wd._stall.enabled
    wd._stall._activity["stdout"] = 0.0
    wd.touch_stdout("first chunk")
    assert wd._stall._activity["stdout"] == 0.0
    wd.touch_stdout("entirely different content")
    assert wd._stall._activity["stdout"] > 0.0


async def test_watchdog_drain_reraises_cancellation():
    sandbox = MagicMock()
    sandbox.files.get_info = AsyncMock(side_effect=asyncio.CancelledError())
    wd = _watchdog(sandbox=sandbox)
    with pytest.raises(asyncio.CancelledError):
        await wd.drain_sandbox_log()


async def test_watchdog_drain_read_failure_keeps_offset(caplog):
    sandbox = MagicMock()
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=10))
    sandbox.files.read = AsyncMock(side_effect=OSError("read failed"))
    wd = _watchdog(sandbox=sandbox)
    with caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"):
        await wd.drain_sandbox_log()
    assert wd._drain_offset == 0
    assert not wd._drained_chunks
    assert any("log_drain_failed" in m for m in caplog.messages)


async def test_watchdog_drain_read_reraises_cancellation():
    sandbox = MagicMock()
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=10))
    sandbox.files.read = AsyncMock(side_effect=asyncio.CancelledError())
    wd = _watchdog(sandbox=sandbox)
    with pytest.raises(asyncio.CancelledError):
        await wd.drain_sandbox_log()


async def test_watchdog_probe_log_growth_failure_is_quiet(caplog):
    sandbox = MagicMock()
    sandbox.files.get_info = AsyncMock(side_effect=OSError("probe failed"))
    wd = _watchdog(sandbox=sandbox, watch_log_path="/home/user/out.json")
    with caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.node_runner"):
        await wd.probe_log_growth()
    assert wd._watch_log_prev_size is None
    assert any("watch_log_probe_failed" in m for m in caplog.messages)


async def test_watchdog_probe_log_growth_reraises_cancellation():
    sandbox = MagicMock()
    sandbox.files.get_info = AsyncMock(side_effect=asyncio.CancelledError())
    wd = _watchdog(sandbox=sandbox, watch_log_path="/home/user/out.json")
    with pytest.raises(asyncio.CancelledError):
        await wd.probe_log_growth()


class _FsEntry:
    def __init__(self, path: str, mtime: float = 1.0, size: int = 10) -> None:
        self.path = path
        self.mtime = mtime
        self.size = size


async def test_watchdog_probe_filesystem_reraises_cancellation():
    sandbox = MagicMock()
    sandbox.files.list = AsyncMock(side_effect=asyncio.CancelledError())
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    with pytest.raises(asyncio.CancelledError):
        await wd.probe_filesystem()


async def test_watchdog_probe_filesystem_min_interval_short_circuits():
    """Within the stat interval a repeat probe does not touch the sandbox."""
    sandbox = MagicMock()
    sandbox.files.list = AsyncMock(return_value=[])
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    await wd.probe_filesystem()
    first_calls = sandbox.files.list.await_count
    await wd.probe_filesystem()
    assert sandbox.files.list.await_count == first_calls


def test_watchdog_trim_drained_chunks_clamps_head_to_window():
    wd = _watchdog()
    big = "x" * (nr._MAX_DRAIN_WINDOW + 1000)
    wd._drained_chunks = [big]
    wd._drained_len = len(big)
    wd._trim_drained_chunks()
    assert wd._drained_len == nr._MAX_DRAIN_WINDOW
    assert wd._drained_chunks[0] == "x" * nr._MAX_DRAIN_WINDOW


async def test_watchdog_probe_filesystem_list_failure_is_quiet(caplog):
    sandbox = MagicMock()
    sandbox.files.list = AsyncMock(side_effect=OSError("list failed"))
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    with caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.node_runner"):
        await wd.probe_filesystem()
    assert not wd._fs_state
    assert any("watch_fs_probe_failed" in m for m in caplog.messages)


async def test_watchdog_probe_filesystem_touches_on_changed_stat():
    class _Clock:
        t = 100.0

    sandbox = MagicMock()
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    wd._stall._now = lambda: _Clock.t
    wd._fs_min_stat_interval = 0.0
    seeded = wd._stall._activity["filesystem"]
    # Probe 1 seeds the tracked state for the matching path (not "activity" —
    # a brand-new file only becomes activity when its stat CHANGES later).
    sandbox.files.list = AsyncMock(return_value=[_FsEntry("/home/user/out/build.log")])
    await wd.probe_filesystem()
    assert wd._stall._activity["filesystem"] == seeded
    assert list(wd._fs_state) == ["/home/user/out/build.log"]
    # Probe 2 sees a changed mtime/size — real filesystem activity.
    _Clock.t = 200.0
    sandbox.files.list = AsyncMock(return_value=[_FsEntry("/home/user/out/build.log", mtime=2.0, size=99)])
    await wd.probe_filesystem()
    assert wd._stall._activity["filesystem"] == 200.0


async def test_watchdog_probe_filesystem_object_with_files_attr():
    class _Listing:
        def __init__(self) -> None:
            self.files = [_FsEntry("/home/user/out/a.log")]

    sandbox = MagicMock()
    sandbox.files.list = AsyncMock(return_value=_Listing())
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    wd._fs_min_stat_interval = 0.0
    await wd.probe_filesystem()
    assert "/home/user/out/a.log" in wd._fs_state


async def test_watchdog_probe_filesystem_invalid_listing_is_quiet(caplog):
    class _BadListing:
        @property
        def files(self) -> Any:
            raise RuntimeError("unreadable listing")

    sandbox = MagicMock()
    sandbox.files.list = AsyncMock(return_value=_BadListing())
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    wd._fs_min_stat_interval = 0.0
    with caplog.at_level(logging.INFO, logger="modulo.core.pipeline_engine.node_runner"):
        await wd.probe_filesystem()
    assert any("watch_fs_list_invalid" in m for m in caplog.messages)


async def test_watchdog_probe_filesystem_prunes_missing_and_touches():
    sandbox = MagicMock()
    sandbox.files.list = AsyncMock(return_value=[])
    wd = _watchdog(sandbox=sandbox, watch_globs=["*.log"])
    wd._fs_state = {"/home/user/old.log": (1.0, 10)}
    wd._fs_min_stat_interval = 0.0
    await wd.probe_filesystem()
    assert not wd._fs_state
    assert wd._stall._activity["filesystem"] > 0.0


def test_watchdog_track_fs_entry_skips_agent_log_and_reports_changes():
    wd = _watchdog(watch_globs=["*.log"])
    seen: set[str] = set()
    assert wd._track_fs_entry(_FsEntry(nr._SANDBOX_LOG_PATH), seen) is False
    assert wd._track_fs_entry(_FsEntry("/home/user/out/build.log"), seen) is False
    assert seen == {"/home/user/out/build.log"}
    assert wd._fs_state["/home/user/out/build.log"] == (1.0, 10)
    # Identical stat is not a change; a changed mtime/size is.
    assert wd._track_fs_entry(_FsEntry("/home/user/out/build.log"), seen) is False
    assert wd._track_fs_entry(_FsEntry("/home/user/out/build.log", mtime=2.0, size=99), seen) is True
    assert wd._track_fs_entry(_FsEntry("/home/user/out/other.txt"), seen) is False


def test_watchdog_track_fs_entry_non_string_path():
    wd = _watchdog(watch_globs=["*.log"])
    entry = MagicMock()
    entry.path = None
    entry.name = 123
    assert wd._track_fs_entry(entry, set()) is False


async def test_watchdog_enforce_resource_limits_reraises_cancellation():
    sandbox = MagicMock()
    sandbox.get_metrics = MagicMock(side_effect=asyncio.CancelledError())
    wd = _watchdog(sandbox=sandbox, resource_limits={"cpu_usage_pct": 50}, sandbox_mode="script")
    with pytest.raises(asyncio.CancelledError):
        await wd.enforce_resource_limits()


async def test_watchdog_enforce_resource_limits_none_metrics_fails_open():
    sandbox = MagicMock()
    sandbox.get_metrics = AsyncMock(return_value=None)
    wd = _watchdog(sandbox=sandbox, resource_limits={"cpu_usage_pct": 50}, sandbox_mode="script")
    killed = await wd.enforce_resource_limits()
    assert killed is False


async def test_watchdog_enforce_resource_limits_compare_failure_fails_open(caplog):
    class _BadMetrics:
        @property
        def cpu_used_pct(self) -> Any:
            raise RuntimeError("bad metric")

    sandbox = MagicMock()
    sandbox.get_metrics = AsyncMock(return_value=_BadMetrics())
    wd = _watchdog(sandbox=sandbox, resource_limits={"cpu_usage_pct": 50}, sandbox_mode="script")
    with caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"):
        killed = await wd.enforce_resource_limits()
    assert killed is False
    assert any("resource_metrics_compare_failed" in m for m in caplog.messages)


def test_watchdog_budget_exceeded_no_limits_false():
    wd = _watchdog(resource_limits=None, sandbox_mode="script")
    assert wd._budget_exceeded(MagicMock()) is False


def test_watchdog_budget_exceeded_disk_cap():
    wd = _watchdog(resource_limits={"disk_mb": 1}, sandbox_mode="script")
    metrics = MagicMock()
    metrics.disk_used = 2 * 1024 * 1024
    assert wd._budget_exceeded(metrics) is True


def test_watchdog_budget_exceeded_non_numeric_metrics_ignored():
    wd = _watchdog(resource_limits={"cpu_usage_pct": 50, "memory_mb": 1}, sandbox_mode="script")
    metrics = MagicMock()
    metrics.cpu_used_pct = True
    metrics.mem_used = None
    assert wd._budget_exceeded(metrics) is False


async def test_watchdog_kill_for_budget_reraises_cancellation():
    sandbox = MagicMock()
    sandbox.kill = AsyncMock(side_effect=asyncio.CancelledError())
    wd = _watchdog(sandbox=sandbox)
    with pytest.raises(asyncio.CancelledError):
        await wd.kill_sandbox_for_budget("n1", reason="wallclock_budget_exceeded")
    assert wd.budget_killed is True


async def test_watchdog_kill_for_budget_failure_sets_flag_and_warns(caplog):
    sandbox = MagicMock()
    sandbox.kill = AsyncMock(side_effect=RuntimeError("kill failed"))
    wd = _watchdog(sandbox=sandbox)
    with caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"):
        await wd.kill_sandbox_for_budget("n1", reason="resource_limits_exceeded")
    assert wd.budget_killed is True
    assert any("resource_kill_failed" in m for m in caplog.messages)
    assert any("budget_killed_sandbox_may_remain_alive" in m for m in caplog.messages)


async def test_watchdog_tick_probes_filesystem_when_globs_present():
    wd = _watchdog(sandbox=MagicMock(), watch_globs=["*.log"])
    drain = AsyncMock()
    probe_fs = AsyncMock()
    wd.drain_sandbox_log = drain
    wd.probe_filesystem = probe_fs
    await wd.tick()
    drain.assert_awaited_once()
    probe_fs.assert_awaited_once()


async def test_watchdog_tick_enforces_resource_limits_every_n_ticks():
    wd = _watchdog(sandbox=MagicMock(), resource_limits={"cpu_usage_pct": 1}, sandbox_mode="script")
    wd.drain_sandbox_log = AsyncMock()
    enforce = AsyncMock(return_value=False)
    wd.enforce_resource_limits = enforce
    for _ in range(nr._SANDBOX_BUDGET_POLL_INTERVAL_TICKS):
        await wd.tick()
    assert enforce.await_count == 1


# ---------------------------------------------------------------------------
# Envelope / provider-error / stall-detector config
# ---------------------------------------------------------------------------


def test_format_sandbox_provider_error_appends_dict_response_body():
    class _ProviderError(Exception):
        pass

    exc = _ProviderError("400: bad timeout")
    exc.response = {"code": 400, "detail": "too long"}
    msg = nr._format_sandbox_provider_error(exc, provider_exc_type=_ProviderError)
    assert "400: bad timeout" in msg
    assert '"code": 400' in msg


def test_format_sandbox_provider_error_unserializable_body_ignored():
    class _ProviderError(Exception):
        pass

    exc = _ProviderError("400: bad")
    exc.response = {1, 2}
    msg = nr._format_sandbox_provider_error(exc, provider_exc_type=_ProviderError)
    assert msg == "400: bad"


def test_format_sandbox_provider_error_unserializable_dict_body_ignored():
    class _ProviderError(Exception):
        pass

    exc = _ProviderError("400: bad")
    exc.response = {"detail": {1, 2}}
    msg = nr._format_sandbox_provider_error(exc, provider_exc_type=_ProviderError)
    assert msg == "400: bad"


def test_build_sandbox_node_envelope_includes_truthy_stall_reason():
    output = nr._SandboxNodeOutput(
        status="failed",
        summary="stalled",
        exit_code=1,
        wall_clock_time_ms=1,
        cost_estimate_usd=0.0,
        stall_reason="idle 300s",
    )
    envelope = nr._build_sandbox_node_envelope(node_id="n1", output=output)
    assert envelope["artifacts"][0]["output"]["stall_reason"] == "idle 300s"
    assert "output_json" not in envelope["artifacts"][0]["output"]
    assert "exit_code" not in envelope["output"]


def test_configure_stall_detector_enables_opt_in_channels():
    stall = nr._configure_stall_detector(
        enable_heartbeat=False,
        watch_log_path="/home/user/out.json",
        stdout_percentage_delta=0.2,
        watch_globs=["*.log"],
    )
    assert stall.enabled == {"output", "log_growth", "stdout", "filesystem"}


def test_configure_stall_detector_default_heartbeat_only():
    stall = nr._configure_stall_detector(
        enable_heartbeat=True, watch_log_path=None, stdout_percentage_delta=None, watch_globs=[]
    )
    assert stall.enabled == {"output", "heartbeat"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("0.4", 0.4), (1, 1.0), ("0", None), ("1.5", None), ("abc", None)],
)
def test_coerce_stdout_percentage_delta(raw: Any, expected: float | None):
    assert nr._coerce_stdout_percentage_delta(raw) == expected


def test_filter_watch_globs_drops_non_strings_and_empties():
    assert nr._filter_watch_globs(["*.log", "", 5, None]) == ["*.log"]
    assert not nr._filter_watch_globs("not-a-list")


# ---------------------------------------------------------------------------
# Builder validation — malformed loop_intercept config
# ---------------------------------------------------------------------------


def test_sandbox_node_def_malformed_loop_intercept_raises():
    node_def = _sandbox_node_def(loop_intercept="not-a-dict")
    with pytest.raises(ValueError, match="malformed loop_intercept config"):
        make_sandbox_agent_fn(node_def)


# ---------------------------------------------------------------------------
# Sandbox dispatch — env/truncation/timeout/teardown paths through the node fn
# ---------------------------------------------------------------------------


async def test_sandbox_input_payload_truncated_beyond_10k():
    """A >10KB input payload is replaced by a bounded truncation marker in the
    sandbox envs (MODULO_INPUT_PAYLOAD) — the raw payload never reaches env."""
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    sandbox = _make_sandbox_mock()
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn({**_run_state(), "run_context": {"input": {"task": "x" * 20000}}})
    assert result["output"]["status"] == "completed"
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["MODULO_INPUT_PAYLOAD"] == '{"_truncated": true, "_key_count": 1}'


async def test_sandbox_allowed_tools_exposed_as_env():
    fn = make_sandbox_agent_fn(_sandbox_node_def(capability_scope={"allowed_tools": ["git", "pytest"]}))
    sandbox = _make_sandbox_mock()
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        await fn(_run_state())
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["MODULO_ALLOWED_TOOLS"] == "git,pytest"


async def test_sandbox_invalid_stall_timeout_falls_back_with_warning(caplog):
    fn = make_sandbox_agent_fn(_sandbox_node_def(stall_timeout_seconds="not-a-number"))
    sandbox = _make_sandbox_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("stall_timeout_invalid_fallback" in m for m in caplog.messages)


async def test_sandbox_teardown_kill_failure_is_logged_not_raised(caplog):
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    sandbox = _make_sandbox_mock()
    sandbox.kill = AsyncMock(side_effect=RuntimeError("kill down"))
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("sandbox_agent.kill_failed" in m for m in caplog.messages)


async def test_sandbox_success_delivery_marker_persist_failure_is_isolated(caplog):
    """A failed sentinel-marker persist must never convert a successful run into
    a failure (FAR-228 success path is best-effort)."""
    fn = make_sandbox_agent_fn(_sandbox_node_def(delivery_sentinel="ALL DONE"))
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "work done\nALL DONE\n"
    cmd_result.stderr = ""
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    sandbox = _make_sandbox_mock(output_json='{"summary": "done"}')
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.object(nr, "_persist_raw_output_marker", new=AsyncMock(side_effect=RuntimeError("marker db down"))),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("success_delivery_marker_persist_failed" in m for m in caplog.messages)


async def test_sandbox_success_delivery_marker_prefers_drained_stdout():
    """The success marker's retained source is the DRAINED agent stdout when the
    drain probe collected chunks (the redirected log file is the real stdout)."""
    row = _MarkerRunRow(_RUN_ID)
    fn = make_sandbox_agent_fn(
        _sandbox_node_def(delivery_sentinel="ALL DONE"), session_factory=lambda: _FakeSession(_run_row_router(row))
    )
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "sdk stdout fallback\nALL DONE\n"
    cmd_result.stderr = ""
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()

    async def _read(path: Any, format: str = "text", **kwargs: Any) -> Any:
        if str(path).endswith("output.json"):
            return '{"summary": "done"}'
        return "drained agent output\nALL DONE\n"

    sandbox.files.read = AsyncMock(side_effect=_read)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=26))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    marker = next(iter(row.raw_output_markers.values()))
    assert marker["delivery_done"] is True
    assert marker["raw_output"].startswith("drained agent output")


async def test_sandbox_otel_span_event_recorded(monkeypatch: pytest.MonkeyPatch):
    span = MagicMock()
    span.is_recording.return_value = True
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: span)
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    sandbox = _make_sandbox_mock()
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        await fn(_run_state())
    assert span.add_event.called


async def test_sandbox_command_timeout_kill_failure_still_raises_retryable(caplog):
    """A total command timeout kills the sandbox before reading output.json; a
    failing kill must still surface the retryable SandboxNodeFailedError."""
    sandbox = _make_sandbox_mock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))
    sandbox.kill = AsyncMock(side_effect=RuntimeError("kill down"))
    fn = make_sandbox_agent_fn(_sandbox_node_def(timeout_seconds=30))
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
        pytest.raises(SandboxNodeFailedError),
    ):
        await fn(_run_state())
    assert any("kill_before_output_read_failed" in m for m in caplog.messages)


async def test_sandbox_gate_killswitch_read_failure_fails_open(monkeypatch: pytest.MonkeyPatch, caplog):
    """A settings read failure inside the single-node idempotency gate degrades
    to the safe default (gate enabled) with a warning — dispatch proceeds."""
    fn = make_sandbox_agent_fn(_sandbox_node_def(delivery_sentinel="ALL DONE"), single_sandbox_node=True)

    def _boom() -> Any:
        raise RuntimeError("settings down")

    monkeypatch.setattr("modulo.settings.get_settings", _boom)
    sandbox = _make_sandbox_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.WARNING, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("idempotency_gate_killswitch_check_failed" in m for m in caplog.messages)


async def test_sandbox_dispatch_marker_clear_failure_is_logged(caplog):
    """A fenced dispatch-marker clear that raises at teardown is failure-isolated
    — the node still completes."""
    _MarkerRunRow(_RUN_ID)

    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        if "claim_count" in stmt_text:
            r.fetchone.return_value = (0,)
        elif "RETURNING" in stmt_text:
            r.fetchone.return_value = ("granted",)
        elif "sandbox_dispatch_state=NULL" in stmt_text:
            raise RuntimeError("clear db down")
        return r

    session = _FakeSession(_route)
    fn = make_sandbox_agent_fn(_sandbox_node_def(), session_factory=lambda: session)
    state = _run_state()
    state["_claim_lease"] = "tok"
    sandbox = _make_sandbox_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(state)
    assert result["output"]["status"] == "completed"
    assert any("dispatch_marker_clear_failed" in m for m in caplog.messages)


async def test_sandbox_dispatch_marker_clear_reraises_cancellation():
    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        if "claim_count" in stmt_text:
            r.fetchone.return_value = (0,)
        elif "RETURNING" in stmt_text:
            r.fetchone.return_value = ("granted",)
        elif "sandbox_dispatch_state=NULL" in stmt_text:
            raise asyncio.CancelledError
        return r

    session = _FakeSession(_route)
    fn = make_sandbox_agent_fn(_sandbox_node_def(), session_factory=lambda: session)
    state = _run_state()
    state["_claim_lease"] = "tok"
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=_make_sandbox_mock())),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(state)


# ---------------------------------------------------------------------------
# SupersededNodeError — dispatch marker denied (zero-row UPDATE)
# ---------------------------------------------------------------------------


async def test_sandbox_superseded_dispatch_marker_denies_provisioning():
    """A zero-row fenced UPDATE means the claim was rotated — the node must
    raise SupersededNodeError and never create a sandbox."""

    def _route(stmt_text: str) -> Any:
        r = MagicMock()
        if "claim_count" in stmt_text:
            r.fetchone.return_value = (0,)
        elif "RETURNING" in stmt_text:
            r.fetchone.return_value = None
        return r

    session = _FakeSession(_route)
    fn = make_sandbox_agent_fn(_sandbox_node_def(), session_factory=lambda: session)
    state = _run_state()
    state["_claim_lease"] = "tok"
    with patch("e2b.AsyncSandbox.create", new=AsyncMock()) as create, pytest.raises(SupersededNodeError):
        await fn(state)
    create.assert_not_called()


async def test_sandbox_context_files_written_verbatim():
    fn = make_sandbox_agent_fn(_sandbox_node_def(context_files={"ctx/notes.txt": "note content"}))
    sandbox = _make_sandbox_mock()
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    writes = {call.args[0]: call.args[1] for call in sandbox.files.write.await_args_list}
    assert writes["ctx/notes.txt"] == "note content"


async def test_sandbox_context_file_write_failure_returns_synthetic_failure_envelope(monkeypatch: pytest.MonkeyPatch):
    """A mid-dispatch generic exception returns the synthetic failed envelope
    (never a wrong-success) and records the OTel span event when recording."""
    span = MagicMock()
    span.is_recording.return_value = True
    monkeypatch.setattr("opentelemetry.trace.get_current_span", lambda: span)

    async def _write(path: Any, content: Any, **kwargs: Any) -> None:
        if str(path) == "ctx/notes.txt":
            raise RuntimeError("context upload failed")

    fn = make_sandbox_agent_fn(_sandbox_node_def(context_files={"ctx/notes.txt": "data"}))
    sandbox = _make_sandbox_mock()
    sandbox.files.write = AsyncMock(side_effect=_write)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(_run_state())
    assert result["output"]["status"] == "failed"
    assert result["output"]["error_type"] == "RuntimeError"
    assert result["output"]["modulo_synthetic_failure"] is True
    span.add_event.assert_called()


async def test_sandbox_loop_intercept_setup_failure_falls_back_to_plain_command(caplog):
    """A loop-intercept guardrail load failure must never fail the dispatch —
    the agent command runs without the bridge (FAR-211 fail-open)."""
    node_def = _sandbox_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    with (
        patch(
            "modulo.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
            new=AsyncMock(side_effect=RuntimeError("guardrail db down")),
        ),
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("loop_intercept_setup_failed" in m for m in caplog.messages)
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "modulo_bridge.py" not in wrapped


async def test_sandbox_output_read_reraises_cancellation():
    """A cancellation during the output.json read re-raises after the
    delivery-evidence retention attempt — never a silent wrong-success."""
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    sandbox = _make_sandbox_mock()

    async def _read(path: Any, format: str = "text", **kwargs: Any) -> Any:
        if str(path).endswith("output.json"):
            raise asyncio.CancelledError
        return ""

    sandbox.files.read = AsyncMock(side_effect=_read)
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)), pytest.raises(asyncio.CancelledError):
        await fn(_run_state())


async def test_sandbox_cancellation_retains_drained_delivery_evidence():
    """FAR-228: a cancellation AFTER the sentinel was drained retains the
    delivery_done marker (nested cancellation in the persist is absorbed)."""
    fn = make_sandbox_agent_fn(_sandbox_node_def(delivery_sentinel="ALL DONE"))
    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.CancelledError)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=9))
    sandbox.files.read = AsyncMock(return_value="ALL DONE\n")
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.object(nr, "_sandbox_cancel_retention_persist", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(_run_state())


async def test_sandbox_cancellation_final_drain_reraises_cancellation():
    """A cancellation INSIDE the final drain is absorbed (uncancel) so the
    delivery-evidence attempt still runs; the original cancellation re-raises."""
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.CancelledError)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(side_effect=asyncio.CancelledError)
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(_run_state())


async def test_sandbox_cancellation_final_drain_failure_is_isolated(caplog):
    """A final-drain wait_for failure (here: a zeroed persist timeout) is
    isolated — the delivery evidence attempt proceeds, then the original
    cancellation re-raises."""
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    handle = MagicMock()
    handle.wait = AsyncMock(side_effect=asyncio.CancelledError)
    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.pipeline_engine.node_runner._IDEMPOTENCY_GATE_CANCEL_PERSIST_TIMEOUT", 0.0),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(_run_state())
    assert any("cancel_retention_drain_failed" in m for m in caplog.messages)


async def test_sandbox_success_delivery_marker_persist_reraises_cancellation():
    fn = make_sandbox_agent_fn(_sandbox_node_def(delivery_sentinel="ALL DONE"))
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "work done\nALL DONE\n"
    cmd_result.stderr = ""
    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)
    sandbox = _make_sandbox_mock(output_json='{"summary": "done"}')
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.object(nr, "_persist_raw_output_marker", new=AsyncMock(side_effect=asyncio.CancelledError())),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(_run_state())


# ---------------------------------------------------------------------------
# Skipped-by-design branches (defensive arms with no observable difference)
# ---------------------------------------------------------------------------
# 1. `_compile_delivery_sentinel_pattern`'s except arm (959-961): the pattern
#    is built from `re.escape(sentinel)` of an already-validated str, so
#    `re.compile` cannot fail — the try/except is a defensive no-op.
# 2. `_sandbox_agent_impl` lines 6229-6232 (sdk-stdout preference in the stall
#    branch): the branch is guarded by `cmd_result is None`, so the inner
#    `if cmd_result is not None` can never fire — defensive dead code.
# 3. `_sandbox_cancel_retention_persist`'s no-running-loop early return
#    (6778-6779): an awaited coroutine always has a running loop.
# 4. `_E2B_SANDBOX_USD_PER_HOUR` module-import fallback (670-671): the except
#    arm runs only at import time, before any test can observe it.


# ---------------------------------------------------------------------------
# Loop-intercept bridge (FAR-211) — setup success path + teardown failure
# ---------------------------------------------------------------------------


def _bridge_server_mock(*, close_side_effect: Exception | None = None) -> MagicMock:
    server = MagicMock()
    server.start = AsyncMock(return_value=47591)
    server.close = AsyncMock(side_effect=close_side_effect) if close_side_effect else AsyncMock()
    return server


def _bridge_load_mock(defs: list[Any] | BaseException) -> AsyncMock:
    if isinstance(defs, BaseException):
        return AsyncMock(side_effect=defs)
    return AsyncMock(return_value=defs)


async def test_sandbox_loop_intercept_bridge_wires_files_env_and_wrapped_command():
    """With enabled loop_intercept AND bound guardrails the bridge is fully
    wired: the bridge client + config are written into the sandbox, the
    endpoint/config env vars are set, and the agent command is wrapped by the
    bridge client (FAR-211)."""
    node_def = _sandbox_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    server = _bridge_server_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
            new=_bridge_load_mock([MagicMock()]),
        ),
        patch("modulo.core.guardrails.loop_intercept.LoopInterceptCallbackServer", return_value=server),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    written = {call.args[0]: call.args[1] for call in sandbox.files.write.call_args_list}
    assert "/home/user/modulo_bridge.py" in written
    assert written["/home/user/modulo_bridge.py"]
    config = json.loads(written["/home/user/modulo_bridge_config.json"])
    assert config["enabled"] is True
    assert config["latency_budget_ms"] == 100
    envs = sandbox.commands.run.call_args.kwargs["envs"]
    assert envs["MODULO_BRIDGE_ENDPOINT"] == "http://127.0.0.1:47591"
    assert envs["MODULO_BRIDGE_CONFIG"] == "/home/user/modulo_bridge_config.json"
    wrapped = sandbox.commands.run.call_args.args[0]
    assert "python3 /home/user/modulo_bridge.py --wrap --" in wrapped
    server.close.assert_awaited_once()


async def test_sandbox_loop_intercept_bridge_setup_cancellation_reraises():
    """A cancellation during the bridge guardrail load must propagate — the
    bridge setup never converts a cancellation into a completed node."""
    node_def = _sandbox_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
            new=_bridge_load_mock(asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn(_run_state())


async def test_sandbox_loop_intercept_teardown_failure_is_isolated(caplog):
    """A bridge-server teardown failure must not mask the node result — the
    node still completes and the failure is logged (FAR-211 fail-open)."""
    node_def = _sandbox_node_def(loop_intercept={"enabled": True, "latency_budget_ms": 100})
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _make_sandbox_mock()
    server = _bridge_server_mock(close_side_effect=RuntimeError("bridge close down"))
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.core.guardrails.loop_intercept.load_loop_intercept_guardrails",
            new=_bridge_load_mock([MagicMock()]),
        ),
        patch("modulo.core.guardrails.loop_intercept.LoopInterceptCallbackServer", return_value=server),
        caplog.at_level(logging.ERROR, logger="modulo.core.pipeline_engine.node_runner"),
    ):
        result = await fn(_run_state())
    assert result["output"]["status"] == "completed"
    assert any("loop_intercept_teardown_failed" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# Provisioning invariant + dispatch capacity gate + budget/schema raises
# ---------------------------------------------------------------------------


async def test_sandbox_create_returning_none_raises_invariant():
    """If ``AsyncSandbox.create`` resolves to ``None`` the provisioning loop
    breaks without a sandbox — the post-loop invariant must raise instead of
    sending commands into a None sandbox. The impl's outer handler converts
    the invariant failure into a failed-node envelope that carries the
    provider error (FAR-511)."""
    fn = make_sandbox_agent_fn(_sandbox_node_def())
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=None)):
        result = await fn(_run_state())
    art = result["artifacts"][0]["output"]
    assert art["status"] == "failed"
    assert art["error_type"] == "RuntimeError"
    assert art["error_message"] == "Sandbox was not created before use"


def _script_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": "s1",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_prompt": "ignored in script mode",
    }
    node_def.update(overrides)
    return node_def


async def test_sandbox_script_capacity_gate_skips_unparseable_org(monkeypatch: pytest.MonkeyPatch):
    """An unparseable org id short-circuits the script-mode dispatch capacity
    gate BEFORE any capacity read — the trigger must not be blocked by a
    malformed org (fail-open) and no capacity query runs."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "k")
    fn = make_sandbox_agent_fn(_script_node_def(), session_factory=lambda: _FakeSession())
    sandbox = _make_sandbox_mock()
    counts = AsyncMock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.db.crud.run.get_sandbox_concurrency_limit", new=counts),
    ):
        result = await fn({**_run_state(), "_org_id": "not-a-uuid"})
    assert result["output"]["status"] == "completed"
    assert not counts.await_count


async def test_sandbox_script_capacity_check_failure_fails_open(caplog, monkeypatch: pytest.MonkeyPatch):
    """A capacity-read failure must not block dispatch: the D8 gate degrades to
    a fail-open dispatch (``runner.capacity.gate_error``) and the sandbox is
    provisioned anyway — the dispatch marker is still written best-effort."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "k")

    class _FakeGateSettings:
        runner_capacity_gate_enabled = False
        runner_capacity_lock_timeout_ms = 2000

    fn = make_sandbox_agent_fn(_script_node_def(), session_factory=lambda: _FakeSession())
    sandbox = _make_sandbox_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.db.crud.run.get_sandbox_concurrency_limit",
            new=AsyncMock(side_effect=RuntimeError("capacity db down")),
        ),
        patch("modulo.core.runner_capacity.get_settings", new=lambda: _FakeGateSettings()),
        caplog.at_level(logging.WARNING, logger="modulo.core.runner_capacity"),
    ):
        result = await fn({**_run_state(), "_claim_lease": "tok"})
    assert result["output"]["status"] == "completed"
    assert any("runner.capacity.gate_error" in m for m in caplog.messages)


async def test_sandbox_script_capacity_check_cancellation_reraises(monkeypatch: pytest.MonkeyPatch):
    """A cancellation inside the D8 dispatch gate must propagate — the gate is
    fail-open for ERRORS only, never for a cancelled dispatch."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "k")

    class _FakeGateSettings:
        runner_capacity_gate_enabled = False
        runner_capacity_lock_timeout_ms = 2000

    fn = make_sandbox_agent_fn(_script_node_def(), session_factory=lambda: _FakeSession())
    sandbox = _make_sandbox_mock()
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch(
            "modulo.db.crud.run.get_sandbox_concurrency_limit",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        patch("modulo.core.runner_capacity.get_settings", new=lambda: _FakeGateSettings()),
        pytest.raises(asyncio.CancelledError),
    ):
        await fn({**_run_state(), "_claim_lease": "tok"})


async def test_sandbox_budget_killed_on_timeout_raises_script_budget_killed():
    """When the resource-cap killer fired during a command that also hit its
    total timeout, the failure must surface as the TERMINAL (never-retryable)
    ``ScriptBudgetKilledError`` — not as the retryable sandbox-node failure
    (FAR-296 Phase 3b-3)."""
    fn = make_sandbox_agent_fn(_sandbox_node_def(timeout_seconds=30))
    sandbox = _make_sandbox_mock()
    sandbox.commands.run = AsyncMock(side_effect=TimeoutError("command timed out"))

    class _PreKilledWatchdog(nr._SandboxWatchdog):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._budget_killed = True

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch.object(nr, "_SandboxWatchdog", _PreKilledWatchdog),
        pytest.raises(ScriptBudgetKilledError, match="exceeded its budget"),
    ):
        await fn(_run_state())


async def test_sandbox_script_schema_violation_raises_script_invalid_output(monkeypatch: pytest.MonkeyPatch):
    """A script-mode output that parses but fails schema validation is a
    POST-CLAIM fault: the terminal ``ScriptInvalidOutputError`` (never
    retryable), distinct from the shared retryable schema code."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "k")
    fn = make_sandbox_agent_fn(_script_node_def(output_schema_json={"required": ["result"]}))
    sandbox = _make_sandbox_mock(output_json='{"other": 1}')
    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        pytest.raises(ScriptInvalidOutputError, match="schema"),
    ):
        await fn(_run_state())
