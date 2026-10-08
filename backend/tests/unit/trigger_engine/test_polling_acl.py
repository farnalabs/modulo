"""FAR-1583: the polling/cron read path enforces the connector ``read`` ACL.

``_build_polling_connector`` deliberately does NOT wrap the connector in
``_TracedConnector`` (polling runs outside a normal run context), so the
executor's per-operation ACL gate never applied here: both poll read sites
called ``connector.query()`` with no ACL check, letting an instance whose
non-empty ``allowed_operations`` excluded ``read`` serve scheduled reads on
every tick.

The gate lives in ``_build_polling_connector_from_instance`` — the single
builder every poll read flows through (the SAQ cron fire job and
``TriggerEngine.evaluate_condition``) — and runs BEFORE any credential is
decrypted. These tests prove:

* a ``["write"]`` instance is BLOCKED with ``ConnectorPermissionError``;
* a ``["read"]`` / unrestricted (``None`` / ``[]``) instance still READS;
* a malformed ``allowed_operations`` fails closed;
* BOTH read sites are blocked — no second unwrapped path.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.connectors.base import ConnectorPermissionError, ConnectorQuery, ConnectorResult
from modulo.core.trigger_engine.polling import (
    _build_polling_connector_from_instance,
    enforce_polling_read_acl,
)

ORG = uuid.UUID("7f000000-0000-0000-0000-000000000002")
_SENTINEL = object()


def _instance(allowed_operations: Any, *, visibility: str = "org") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        connector_type_id="rest",
        config_json={},
        allowed_operations=allowed_operations,
        visibility=visibility,
    )


class _FakeSecretsBackend:
    async def get_secret(self, _cid: str) -> str:
        return '{"token": "x"}'


class _StubConnector:
    """Read-only stand-in that records the resources it was queried for."""

    def __init__(self) -> None:
        self.queried_resources: list[str] = []

    async def query(self, q: ConnectorQuery) -> ConnectorResult:
        self.queried_resources.append(q.resource)
        return ConnectorResult(records=[{"id": 1}], total=1)


def _build_stubbed(instance: Any) -> tuple[Any, Any]:
    """Run the real builder with everything BELOW the ACL gate stubbed out."""
    with (
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=_FakeSecretsBackend()),
        patch("modulo.core.connector_hub.resolve_shared_rate_limit_redis", return_value=None),
        patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=_SENTINEL),
    ):
        return asyncio.run(_build_polling_connector_from_instance(MagicMock(), instance, ORG))


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_write_only_allowlist_denies_polling_read() -> None:
    """A non-empty allowlist that excludes ``read`` must raise (fail closed)."""
    with pytest.raises(ConnectorPermissionError, match="not in allowed_operations"):
        enforce_polling_read_acl(_instance(["write"]))


@pytest.mark.parametrize(
    "allowed_operations",
    [{"read": True}, "read"],
    ids=["dict-not-list", "str-not-list"],
)
def test_malformed_allowed_operations_fails_closed(allowed_operations: Any) -> None:
    """A malformed value certifies no grant — it must never permit a read."""
    with pytest.raises(ConnectorPermissionError, match="malformed"):
        enforce_polling_read_acl(_instance(allowed_operations))


def test_denied_instance_never_reaches_credential_decryption() -> None:
    """The gate runs FIRST: a denied instance exposes no credentials."""
    with (
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend") as create_backend,
        patch("modulo.core.trigger_engine.polling._build_polling_connector") as build,
        pytest.raises(ConnectorPermissionError, match="not in allowed_operations"),
    ):
        asyncio.run(_build_polling_connector_from_instance(MagicMock(), _instance(["write"]), ORG))

    create_backend.assert_not_called()
    build.assert_not_called()


# ---------------------------------------------------------------------------
# Read site 1 — the SAQ cron fire path (cron_helpers._build_polling_connector)
# ---------------------------------------------------------------------------


def test_cron_fire_path_denies_write_only_instance_and_records_poll_error() -> None:
    """The cron fire build returns ``(None, None)`` and logs an observable denial."""
    from modulo.core import cron_helpers

    session = MagicMock()
    trigger = SimpleNamespace(id=uuid.uuid4())

    with (
        patch.object(cron_helpers, "_log_poll_event", new_callable=AsyncMock) as log_event,
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend") as create_backend,
    ):
        result = asyncio.run(
            cron_helpers._build_polling_connector(
                session,
                _instance(["write"]),
                trigger,
                ORG,
                uuid.uuid4(),
            )
        )

    assert result == (None, None)
    create_backend.assert_not_called()
    log_event.assert_awaited_once()
    logged = log_event.await_args.kwargs
    assert logged["result"] == "poll_error"
    assert "ACL denied read" in logged["error_detail"]


# ---------------------------------------------------------------------------
# Read site 2 — TriggerEngine.evaluate_condition
# ---------------------------------------------------------------------------


def test_evaluate_condition_denies_write_only_instance_without_querying() -> None:
    """The one-off evaluation path returns an explicit ACL-denied error dict."""
    from modulo.core.trigger_engine import TriggerEngine

    session = AsyncMock()
    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = _instance(["write"])
    session.execute = AsyncMock(return_value=row_result)

    with patch("modulo.core.trigger_engine.polling._build_polling_connector") as build:
        result = asyncio.run(
            TriggerEngine.evaluate_condition(
                session,
                _trigger=MagicMock(),
                org_id=ORG,
                connector_instance_id=uuid.uuid4(),
                poll_query="SELECT 1",
            )
        )

    assert result["status"] == "error"
    assert result.get("acl_denied") is True
    assert "ACL denied read" in result["error"]
    build.assert_not_called()


@pytest.mark.parametrize(
    "allowed_operations",
    [None, [], ["read"]],
    ids=["unrestricted-none", "unrestricted-empty-list", "read-granted"],
)
def test_polling_read_proceeds_for_unrestricted_and_read_granted(allowed_operations: Any) -> None:
    """Control: an unrestricted or read-granted instance still PERFORMS the read."""
    from modulo.core.trigger_engine import TriggerEngine

    session = AsyncMock()
    row_result = MagicMock()
    row_result.scalar_one_or_none.return_value = _instance(allowed_operations)
    session.execute = AsyncMock(return_value=row_result)

    stub = _StubConnector()
    with (
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=_FakeSecretsBackend()),
        patch("modulo.core.connector_hub.resolve_shared_rate_limit_redis", return_value=None),
        patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=stub),
    ):
        result = asyncio.run(
            TriggerEngine.evaluate_condition(
                session,
                _trigger=MagicMock(),
                org_id=ORG,
                connector_instance_id=uuid.uuid4(),
                poll_query="SELECT 1",
            )
        )

    assert result["status"] == "condition_met"
    assert stub.queried_resources == ["SELECT 1"]
