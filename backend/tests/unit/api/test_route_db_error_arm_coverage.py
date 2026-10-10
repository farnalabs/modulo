"""Changed-lines coverage for the route-local DB-error arms the S1192
literal-extraction sweep touched (FAR-366).

``chore(sonar)/s1192-api-cli`` replaced duplicated log-key string literals in
several routes' ``except ProgrammingError`` and ``except SQLAlchemyError`` arms
with module-level ``_CODE_*`` constants.  ``test_far1464_route_arm_coverage``
drives those same arms with an ``InvalidRequestError`` (the session-contract
guard raises), which covers each arm's guard CALL but never the handler body
lines above it nor the ``logger.exception`` line the guard RETURNS past — so
the changed-lines coverage gate measured those extracted lines as uncovered.

This module drives the affected endpoints with the two exception classes that
reach those arms but are NOT exercised elsewhere:

* ``ProgrammingError`` -> the ``except ProgrammingError`` arm -> HTTP 501.
* a plain ``SQLAlchemyError`` (transient, non-contract) -> the guard RETURNS,
  the arm's ``logger.exception`` runs -> HTTP 503.

Both tests pin the arm's status so the extracted line is measured as covered.
The behaviour itself is owned by the per-route suites; these are coverage-of-arm
tests, matching the harness in ``test_far1464_route_arm_coverage``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.api.routes import (
    admin_email,
    admin_tiers,
    api_keys,
    environment_profiles,
    error_forwarder_config,
    mcp_oauth,
    webhooks,
)
from tests.unit.api.test_far1464_route_arm_coverage import (
    _ORG_ID,
    _FakeRedisFactory,
    _principal,
    _RaisingSession,
    _settings,
    _tenant,
)


def _programming_error() -> ProgrammingError:
    return ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))


def _transient_error() -> SQLAlchemyError:
    return SQLAlchemyError("connection reset by peer")


async def _assert_status(coro: object, status_code: int) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await coro  # type: ignore[misc]
    assert excinfo.value.status_code == status_code, excinfo.value.detail


def _patch_route_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_tiers, "Redis", _FakeRedisFactory)
    monkeypatch.setattr(mcp_oauth, "public_url_is_configured", lambda settings: True)
    monkeypatch.setattr(mcp_oauth, "normalize_scopes", lambda joined: joined.split())
    monkeypatch.setattr(mcp_oauth, "normalize_redirect_uris", lambda uris: uris)


async def test_programming_error_arms_answer_501(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_route_seams(monkeypatch)
    session = _RaisingSession(_programming_error())

    await _assert_status(admin_email.admin_get_email_settings(org_id=_ORG_ID, _=_principal(), session=session), 501)
    await _assert_status(
        admin_tiers.list_tiers_endpoint(settings=_settings(), current_user=_tenant(), session=session), 501
    )
    await _assert_status(api_keys.list_api_keys_endpoint(session=session, principal=_tenant()), 501)
    await _assert_status(
        environment_profiles.restore_profile(profile_id=uuid.uuid4(), session=session, principal=_tenant()), 501
    )
    await _assert_status(
        error_forwarder_config.delete_forwarder(forwarder_type="sentry", session=session, principal=_tenant()), 501
    )
    await _assert_status(
        error_forwarder_config.restore_forwarder(forwarder_type="sentry", session=session, principal=_tenant()), 501
    )
    await _assert_status(
        mcp_oauth.register_oauth_client(
            req=SimpleNamespace(scopes=["read"], redirect_uris=["https://x/cb"], name="c"),
            session=session,
            principal=_tenant(),
            settings=_settings(),
        ),
        501,
    )
    await _assert_status(mcp_oauth.list_oauth_clients_endpoint(session=session, principal=_tenant()), 501)
    await _assert_status(mcp_oauth.remove_oauth_client(client_id="c", session=session, principal=_tenant()), 501)
    await _assert_status(
        mcp_oauth.approve_consent(req=SimpleNamespace(state="s"), session=session, principal=_tenant()), 501
    )
    await _assert_status(webhooks.cleanup_expired(session=session, principal=_tenant()), 501)


async def test_transient_sqlalchemy_error_arms_answer_503(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_route_seams(monkeypatch)
    session = _RaisingSession(_transient_error())

    await _assert_status(admin_email.admin_get_email_settings(org_id=_ORG_ID, _=_principal(), session=session), 503)
    await _assert_status(
        admin_tiers.list_tiers_endpoint(settings=_settings(), current_user=_tenant(), session=session), 503
    )
    await _assert_status(api_keys.list_api_keys_endpoint(session=session, principal=_tenant()), 503)
    await _assert_status(
        environment_profiles.restore_profile(profile_id=uuid.uuid4(), session=session, principal=_tenant()), 503
    )
    await _assert_status(
        error_forwarder_config.delete_forwarder(forwarder_type="sentry", session=session, principal=_tenant()), 503
    )
    await _assert_status(
        error_forwarder_config.restore_forwarder(forwarder_type="sentry", session=session, principal=_tenant()), 503
    )
    await _assert_status(
        mcp_oauth.register_oauth_client(
            req=SimpleNamespace(scopes=["read"], redirect_uris=["https://x/cb"], name="c"),
            session=session,
            principal=_tenant(),
            settings=_settings(),
        ),
        503,
    )
    await _assert_status(mcp_oauth.list_oauth_clients_endpoint(session=session, principal=_tenant()), 503)
    await _assert_status(mcp_oauth.remove_oauth_client(client_id="c", session=session, principal=_tenant()), 503)
    await _assert_status(
        mcp_oauth.approve_consent(req=SimpleNamespace(state="s"), session=session, principal=_tenant()), 503
    )
    await _assert_status(webhooks.cleanup_expired(session=session, principal=_tenant()), 503)
