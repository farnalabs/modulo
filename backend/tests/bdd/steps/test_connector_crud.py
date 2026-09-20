"""Step definitions for the connector CRUD lifecycle BDD scenarios (PRD 7.11).

Wires the scenarios in ``connector_crud.feature`` to the real
``/api/v1/connectors`` create / get / list / update / delete routes
(``modulo/api/routes/connectors.py``) through the shared TestClient +
mock-org-session pattern used by the nearby connector / feedback step modules.
Only the DB lookup / CRUD and RLS seams are patched at their route use-site;
the routing, permission gating (``require_permission`` resolving through the
real tenant principal), break-glass mint deny, request validation, REST
credential/config contract validation, Fernet encryption, and response
serialisation all run for real — so the scenarios assert the actual API
contract for the connector instance lifecycle: 201 create with credentials
encrypted at rest and never echoed, 422 on malformed REST credentials / config,
200 individual + paginated retrieval (redacted), 200 PATCH re-encrypting fresh
credentials, 204 DELETE, and the org-isolation 404 on foreign-org access.
"""

from __future__ import annotations

import json
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings, get_settings

scenarios("../features/connectors/connector_crud.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FOREIGN_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000009")

_FERNET_KEY = Fernet.generate_key().decode()


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        modulo_admin_password="testpass",
        modulo_csrf_enabled=False,
    )


def _connector_uuid(connector_id: str) -> uuid.UUID:
    """Deterministically map a feature-file connector id onto a UUID."""
    return uuid.uuid5(_ORG_ID, connector_id)


def _make_connector(**overrides: object) -> MagicMock:
    """ConnectorInstance-shaped mock matching ``_to_response``'s reads."""
    ci = MagicMock()
    ci.id = overrides.get("id", _connector_uuid("conn-1"))
    ci.organisation_id = overrides.get("organisation_id", _ORG_ID)
    ci.name = overrides.get("name", "Staging Bot")
    ci.connector_type_id = overrides.get("connector_type_id", "rest")
    ci.credentials_ciphertext = overrides.get("credentials_ciphertext", b"gAAAAAB")
    ci.config_json = overrides.get("config_json", {})
    ci.allowed_operations = overrides.get("allowed_operations", [])
    ci.status = overrides.get("status", "active")
    ci.visibility = overrides.get("visibility", "org")
    ci.owner_team_id = overrides.get("owner_team_id")
    ci.tier = overrides.get("tier", "native")
    ci.created_at = overrides.get("created_at", datetime(2025, 1, 1, tzinfo=UTC))
    ci.updated_at = overrides.get("updated_at", datetime(2025, 1, 1, tzinfo=UTC))
    ci.degraded_at = overrides.get("degraded_at")
    ci.last_skip_error = overrides.get("last_skip_error")
    ci.validation_level = overrides.get("validation_level")
    return ci


def _make_session() -> AsyncMock:
    """AsyncSession double dispatching the connector routes' reads.

    ``require_permission``'s per-request org kill-switch read
    (``organisations.authz_enforce``) resolves to None (defaults to enforce=True
    in ``resolve_authz_enforce``, so the admin principal passes the org-role
    gate for real). Every other read returns an empty result so un-seeded
    queries are deterministic. ``deny_break_glass_mint``'s account read
    (``session.get``) auto-resolves to a MagicMock whose ``is_break_glass`` is
    not the literal True, so the mint-deny never fires.
    """
    session = AsyncMock()
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = AsyncMock(return_value=bind)
    session.in_transaction = MagicMock(return_value=True)
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.add = MagicMock()

    def _execute(stmt: object, *_args: object, **_kwargs: object) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        result.all.return_value = []
        return result

    session.execute = AsyncMock(side_effect=_execute)
    return session


def _make_test_client(session: AsyncMock) -> TestClient:
    """Build an admin-principal TestClient against the real connectors app."""

    async def override_session() -> Any:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    app.dependency_overrides[get_current_tenant_user] = lambda: TenantPrincipal(
        username="operator",
        organisation_id=_ORG_ID,
        account_id=_ACCOUNT_ID,
        org_role="admin",
    )
    return TestClient(app)


def _clear_overrides() -> None:
    for dep in (get_settings, get_db_session, _get_engine, get_current_user, get_current_tenant_user):
        app.dependency_overrides.pop(dep, None)


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {"session": _make_session()}
    return cast("dict[str, Any]", request.node._ctx)


def _dispatch(request: Any, method: str, url: str, *, json: dict[str, Any] | None = None) -> None:
    """Issue the request against an admin client and stash the response."""
    with ExitStack() as stack:
        for patcher in _ctx(request).get("patchers", []):
            stack.enter_context(patcher)
        client = _make_test_client(_ctx(request)["session"])
        try:
            resp = client.request(method, url, json=json)
        finally:
            _clear_overrides()
            client.close()
    request.node._resp = resp


def _body(request: Any) -> dict[str, Any]:
    resp = request.node._resp
    body = resp.json()
    assert isinstance(body, dict), f"Expected a JSON object body, got {type(body)}"
    return body


def _standard_patches() -> list[Any]:
    """RLS seams every covered endpoint touches."""
    return [
        patch("modulo.api.routes.connectors.set_rls_org", new=AsyncMock()),
        patch("modulo.api.routes.connectors.set_rls_user_context", new=AsyncMock()),
    ]


def _capturing_create(ciphertext_slot: list[bytes]) -> AsyncMock:
    """Fake ``create_connector_instance`` that echoes a created connector and
    captures the ciphertext the route asked the store to persist."""

    async def _create(*args: object, **kwargs: object) -> MagicMock:
        ciphertext = kwargs["credentials_ciphertext"]
        assert isinstance(ciphertext, bytes), "credentials_ciphertext must be bytes"
        ciphertext_slot.append(ciphertext)
        return _make_connector(
            name=str(kwargs["name"]),
            connector_type_id=str(kwargs["connector_type_id"]),
            credentials_ciphertext=ciphertext,
            config_json=kwargs.get("config_json") or {},
            allowed_operations=kwargs.get("allowed_operations") or [],
            visibility=str(kwargs.get("visibility", "org")),
            tier=str(kwargs.get("tier", "native")),
        )

    return AsyncMock(side_effect=_create)


def _capturing_update(ciphertext_slot: list[bytes], existing: MagicMock) -> AsyncMock:
    """Fake ``update_connector_instance`` that echoes the merged row and
    captures the ciphertext the route asked the store to persist."""

    async def _update(session: object, connector_id: uuid.UUID, updates: dict[str, Any]) -> MagicMock:
        ciphertext = updates.get("credentials_ciphertext", existing.credentials_ciphertext)
        if ciphertext is not None:
            assert isinstance(ciphertext, bytes), "credentials_ciphertext must be bytes"
            ciphertext_slot.append(ciphertext)
        return _make_connector(
            id=existing.id,
            name=updates.get("name", existing.name),
            connector_type_id=existing.connector_type_id,
            credentials_ciphertext=ciphertext,
            degraded_at=updates.get("degraded_at", existing.degraded_at),
            last_skip_error=updates.get("last_skip_error", existing.last_skip_error),
        )

    return AsyncMock(side_effect=_update)


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given("I am an admin operator in the organisation")
def _given_admin_operator() -> None:
    """No-op — the ``when`` steps build an admin-principal TestClient."""


@given("the connector store accepts a new connector")
def _given_store_accepts_new_connector(request: Any) -> None:
    ctx = _ctx(request)
    ctx["store_ciphertext"] = []
    ctx["store_mock"] = _capturing_create(ctx["store_ciphertext"])


@given(parsers.parse('a connector with id "{connector_id}" exists in my organisation'))
def _given_connector_in_org(request: Any, connector_id: str) -> None:
    _ctx(request)["connector"] = _make_connector(
        id=_connector_uuid(connector_id),
        name="Staging Bot",
        credentials_ciphertext=bytes(Fernet(_FERNET_KEY.encode()).encrypt(
            json.dumps({"auth_mode": "bearer", "token": "tok-123"}).encode()
        )),
    )


@given(parsers.parse('a connector with id "{connector_id}" exists in another organisation'))
def _given_connector_foreign_org(request: Any, connector_id: str) -> None:
    _ctx(request)["connector"] = _make_connector(
        id=_connector_uuid(connector_id),
        name="Foreign Connector",
        organisation_id=_FOREIGN_ORG_ID,
    )


@given("my organisation has connectors in the store")
def _given_connectors_in_store(request: Any) -> None:
    _ctx(request)["page_result"] = MagicMock(
        items=[_make_connector(name="Staging Bot")],
        total=1,
        page=1,
        page_size=20,
        next_cursor=None,
        has_more=False,
    )


@given(parsers.parse('REST credentials with bearer token "{token}"'))
def _given_rest_bearer_credentials(request: Any, token: str) -> None:
    _ctx(request)["credentials"] = json.dumps({"auth_mode": "bearer", "token": token})
    _ctx(request)["secret"] = token


@given(parsers.parse('REST credentials payload "{payload}"'))
def _given_rest_raw_credentials(request: Any, payload: str) -> None:
    _ctx(request)["credentials"] = payload
    _ctx(request)["secret"] = None


@given(parsers.parse('a REST connector config with on_unknown "{mode}"'))
def _given_rest_config_on_unknown(request: Any, mode: str) -> None:
    _ctx(request)["config_json"] = {
        "base_url": "https://api.example.com",
        "method": "GET",
        "on_unknown": mode,
    }


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('I create a connector named "{name}" of type "{conn_type}"'))
def _when_create_connector(request: Any, name: str, conn_type: str) -> None:
    ctx = _ctx(request)
    ctx["session"] = _make_session()
    config_json = ctx.get("config_json") or {}
    body = {
        "name": name,
        "connector_type_id": conn_type,
        "credentials": ctx.get("credentials", "tok"),
        "config_json": config_json,
    }
    ctx["patchers"] = [
        *_standard_patches(),
        patch(
            "modulo.api.routes.connectors.validate_owner_team_for_create",
            new=AsyncMock(),
        ),
    ]
    store_mock = ctx.get("store_mock")
    if store_mock is not None:
        ctx["patchers"].append(
            patch("modulo.api.routes.connectors.create_connector_instance", new=store_mock)
        )
    _dispatch(request, "POST", "/api/v1/connectors", json=body)


@when(parsers.parse('I fetch the connector "{connector_id}"'))
def _when_get_connector(request: Any, connector_id: str) -> None:
    ctx = _ctx(request)
    ctx["session"] = _make_session()
    ctx["patchers"] = [
        *_standard_patches(),
        patch(
            "modulo.api.routes.connectors.get_connector_instance",
            new=AsyncMock(return_value=ctx.get("connector")),
        ),
    ]
    _dispatch(request, "GET", f"/api/v1/connectors/{_connector_uuid(connector_id)}")


@when("I list the connectors")
def _when_list_connectors(request: Any) -> None:
    ctx = _ctx(request)
    ctx["session"] = _make_session()
    ctx["patchers"] = [
        *_standard_patches(),
        patch(
            "modulo.api.routes.connectors.list_connector_instances",
            new=AsyncMock(return_value=ctx.get("page_result")),
        ),
    ]
    _dispatch(request, "GET", "/api/v1/connectors")


@when(parsers.parse('I update connector "{connector_id}" with fresh REST credentials "{token}"'))
def _when_update_connector(request: Any, connector_id: str, token: str) -> None:
    ctx = _ctx(request)
    ctx["session"] = _make_session()
    existing = ctx.get("connector") or _make_connector(id=_connector_uuid(connector_id), name="Staging Bot")
    ctx["store_ciphertext"] = []
    ctx["secret"] = token
    ctx["patchers"] = [
        *_standard_patches(),
        patch(
            "modulo.api.routes.connectors.get_connector_instance",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "modulo.api.routes.connectors.update_connector_instance",
            new=_capturing_update(ctx["store_ciphertext"], existing),
        ),
    ]
    _dispatch(
        request,
        "PATCH",
        f"/api/v1/connectors/{_connector_uuid(connector_id)}",
        json={"credentials": json.dumps({"auth_mode": "bearer", "token": token})},
    )


@when(parsers.parse('I delete the connector "{connector_id}"'))
def _when_delete_connector(request: Any, connector_id: str) -> None:
    ctx = _ctx(request)
    ctx["session"] = _make_session()
    connector = ctx.get("connector")
    delete_return = connector is not None and connector.organisation_id == _ORG_ID
    ctx["patchers"] = [
        *_standard_patches(),
        patch(
            "modulo.api.routes.connectors.get_connector_instance",
            new=AsyncMock(return_value=connector),
        ),
        patch(
            "modulo.api.routes.connectors.delete_connector_instance",
            new=AsyncMock(return_value=delete_return),
        ),
    ]
    _dispatch(request, "DELETE", f"/api/v1/connectors/{_connector_uuid(connector_id)}")


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the connector response exposes has_credentials true")
def _then_has_credentials(request: Any) -> None:
    assert _body(request)["has_credentials"] is True


@then("the connector response does not echo the credential plaintext")
def _then_no_credential_plaintext(request: Any) -> None:
    secret = _ctx(request).get("secret")
    resp = request.node._resp
    assert "credentials_ciphertext" not in resp.text, "response echoes the encrypted credential column"
    for marker in ("tok-123", "tok-456", "tok-abc"):
        assert marker not in resp.text, f"response echoes plaintext credential {marker!r}"
    if secret:
        assert secret not in resp.text, f"response echoes plaintext credential {secret!r}"


@then("the stored credentials are Fernet-encrypted")
def _then_stored_fernet(request: Any) -> None:
    ciphertext = _ctx(request)["store_ciphertext"][-1]
    plaintext = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    expected = json.dumps({"auth_mode": "bearer", "token": _ctx(request)["secret"]})
    assert json.loads(plaintext) == json.loads(expected), (
        f"stored credential round-trip mismatch: {plaintext!r} != {expected!r}"
    )


@then("the updated credentials are stored Fernet-encrypted")
def _then_updated_fernet(request: Any) -> None:
    ciphertext = _ctx(request)["store_ciphertext"][-1]
    plaintext = Fernet(_FERNET_KEY.encode()).decrypt(ciphertext).decode()
    parsed = json.loads(plaintext)
    assert parsed["auth_mode"] == "bearer"
    assert parsed["token"] == _ctx(request)["secret"], (
        f"stored credential token mismatch: {plaintext!r}"
    )


@then("the list response reports one connector total")
def _then_list_total(request: Any) -> None:
    body = _body(request)
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "Staging Bot"


@then("the list response does not echo the credential plaintext")
def _then_list_no_plaintext(request: Any) -> None:
    assert "credentials_ciphertext" not in request.node._resp.text
