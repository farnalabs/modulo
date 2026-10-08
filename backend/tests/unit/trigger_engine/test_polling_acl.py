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

FAR-1595 adds the TEAM-scope half in the same builder: a team-private
connector polled by a trigger whose pipeline is owned by a different team (or
by no team at all) is blocked before credential decryption too — the fire job
reads the connector row team-blind, so the allowlist gate alone never caught
a cross-team reference.
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
    """A malformed value certifies no grant — it must never permit a read.

    ``ConnectorACL`` restricts a malformed ``allowed_operations`` to the EMPTY
    allowlist (FAR-1564), so the poll is denied with an empty grant list.
    """
    with pytest.raises(ConnectorPermissionError, match=r"not in allowed_operations: \[\]"):
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


# ---------------------------------------------------------------------------
# Team scope (FAR-1595) — the fire job reads the connector row team-blind
# ---------------------------------------------------------------------------

TEAM_A = uuid.UUID("7f000000-0000-0000-0000-00000000000a")
TEAM_B = uuid.UUID("7f000000-0000-0000-0000-00000000000b")
PIPELINE_ID = uuid.UUID("7f000000-0000-0000-0000-00000000000c")


def _team_instance(owner_team_id: Any, *, allowed_operations: Any = None, visibility: str = "team") -> Any:
    instance = _instance(allowed_operations, visibility=visibility)
    instance.owner_team_id = owner_team_id
    return instance


class _PipelineResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


def _session_with_pipeline(owner_team_id: Any) -> Any:
    """Session double whose pipeline read resolves to *owner_team_id*.

    ``pipeline_row=None`` (a pipeline the org cannot resolve) is passed as a
    sentinel to model the fail-closed branch.
    """
    session = MagicMock()
    session.execute = AsyncMock(return_value=_PipelineResult(SimpleNamespace(owner_team_id=owner_team_id)))
    return session


def _build_team_gated(instance: Any, session: Any, pipeline_id: Any = PIPELINE_ID) -> tuple[Any, Any]:
    """Run the real builder with everything BELOW both gates stubbed out."""
    with (
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=_FakeSecretsBackend()),
        patch("modulo.core.connector_hub.resolve_shared_rate_limit_redis", return_value=None),
        patch("modulo.core.trigger_engine.polling._build_polling_connector", return_value=_SENTINEL),
    ):
        return asyncio.run(
            _build_polling_connector_from_instance(session, instance, ORG, pipeline_id=pipeline_id),
        )


def test_cross_team_connector_denied_before_credential_decryption() -> None:
    """A team-B connector polled by a team-A pipeline's trigger is refused.

    The fire job reads the connector row TEAM-BLIND, so without this gate the
    allowlist check was the only thing between a trigger and another team's
    credentials (FAR-1595). No credential may be decrypted for a denial.
    """
    session = _session_with_pipeline(TEAM_A)
    with (
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend") as create_backend,
        patch("modulo.core.trigger_engine.polling._build_polling_connector") as build,
        pytest.raises(ConnectorPermissionError, match="Team-private connector"),
    ):
        asyncio.run(
            _build_polling_connector_from_instance(
                session,
                _team_instance(TEAM_B),
                ORG,
                pipeline_id=PIPELINE_ID,
            ),
        )

    create_backend.assert_not_called()
    build.assert_not_called()


def test_same_team_connector_still_polls() -> None:
    """Control: a team-private connector owned by the pipeline's OWN team reads."""
    connector, redis_client = _build_team_gated(
        _team_instance(TEAM_A),
        _session_with_pipeline(TEAM_A),
    )

    assert connector is _SENTINEL
    assert redis_client is None


def test_team_private_connector_on_an_org_pipeline_is_denied() -> None:
    """An org pipeline (no owner team) cannot poll a team-private connector."""
    with pytest.raises(ConnectorPermissionError, match="Team-private connector"):
        _build_team_gated(_team_instance(TEAM_B), _session_with_pipeline(None))


def test_org_visible_connector_needs_no_team_context() -> None:
    """An org-wide connector is usable by any team's pipeline — no pipeline read at all."""
    session = _session_with_pipeline(TEAM_A)
    connector, _redis_client = _build_team_gated(
        _team_instance(None, visibility="org"),
        session,
    )

    assert connector is _SENTINEL
    session.execute.assert_not_awaited()


def test_unresolvable_pipeline_fails_closed() -> None:
    """A pipeline id that does not resolve in the org is a DENIAL, not a pass."""
    session = MagicMock()
    session.execute = AsyncMock(return_value=_PipelineResult(None))

    with pytest.raises(ConnectorPermissionError, match="does not resolve"):
        _build_team_gated(_team_instance(TEAM_B), session)


def test_no_supplied_pipeline_context_skips_the_team_gate() -> None:
    """A caller without team context cannot be judged — and must not be blocked.

    The gate is defence-in-depth on top of the save-time validation
    (``api/routes/triggers.py``) and the request session's team RLS; the SAQ
    fire path ALWAYS supplies ``pipeline_id`` (proven below).
    """
    connector, _redis_client = _build_team_gated(
        _team_instance(TEAM_B),
        MagicMock(),
        pipeline_id=None,
    )

    assert connector is _SENTINEL


def test_cron_fire_path_supplies_the_pipeline_team_context() -> None:
    """The production fire path hands the gate the trigger's pipeline (the wiring).

    Without this argument the team gate would silently never run on the one
    path that reads the connector row team-blind — the defect FAR-1595 names.
    """
    from modulo.core import cron_helpers

    session = _session_with_pipeline(TEAM_A)
    trigger = SimpleNamespace(id=uuid.uuid4(), pipeline_id=PIPELINE_ID)

    with (
        patch.object(cron_helpers, "_log_poll_event", new_callable=AsyncMock) as log_event,
        patch("modulo.settings.get_settings", return_value=MagicMock(fernet_key="b" * 44)),
        patch("modulo.core.secrets_backend.create_secrets_backend") as create_backend,
    ):
        result = asyncio.run(
            cron_helpers._build_polling_connector(
                session,
                _team_instance(TEAM_B),
                trigger,
                ORG,
                uuid.uuid4(),
            ),
        )

    assert result == (None, None)
    create_backend.assert_not_called()
    log_event.assert_awaited_once()
    logged = log_event.await_args.kwargs
    assert logged["result"] == "poll_error"
    assert "Team-private connector" in logged["error_detail"]
