"""Consolidated route coverage tests for batch 11 targets."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import get_db_session, get_plan_context
from modulo.api.main import app
from modulo.api.routes import (
    admin_email,
    admin_system_config,
    contributions,
    error_notification_rules,
    notifications,
)
from modulo.auth.dependencies import get_current_tenant_user, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.email_service import EmailSendingError
from modulo.db.models.system_config import SystemConfig
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_rls_preamble
from tests.unit.api.plan_stubs import all_features

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
FERNET_KEY = Fernet.generate_key().decode()


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key=FERNET_KEY,
        modulo_admin_password="testpass",
    )


def _tenant(*, role: str = "admin") -> TenantPrincipal:
    return TenantPrincipal(username=role, organisation_id=ORG_ID, account_id=USER_ID, org_role=role)


def _auth(*, role: str = "admin", is_sys: bool = True) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username=role, organisation_id=ORG_ID, account_id=USER_ID, org_role=role, is_system_admin=is_sys
    )


def _result(
    *,
    scalar: object = 0,
    scalar_one: object = 0,
    scalar_one_or_none: object = None,
    scalars: list[object] | None = None,
) -> MagicMock:
    res = MagicMock()
    res.scalar.return_value = scalar
    res.scalar_one.return_value = scalar_one
    res.scalar_one_or_none.return_value = scalar_one_or_none
    res.scalars.return_value.all.return_value = scalars or []
    res.scalars.return_value.__iter__.return_value = iter(scalars or [])
    return res


def _session(*, begin_error: Exception | None = None, results: list[MagicMock] | None = None) -> AsyncMock:
    session = configure_rls_preamble(AsyncMock())
    context = AsyncMock()
    context.__aenter__ = AsyncMock(side_effect=begin_error, return_value=None)
    context.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=context)
    session.add = MagicMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    if results is None:
        session.execute = AsyncMock(return_value=_result())
    else:
        queue = list(results)

        async def _exec(*_a: object, **_kw: object) -> MagicMock:
            return queue.pop(0) if len(queue) > 1 else queue[0]

        session.execute = AsyncMock(side_effect=_exec)
    return session


def _client(session: AsyncMock, *, role: str = "admin", is_sys: bool = True) -> TestClient:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_current_user] = lambda: _auth(role=role, is_sys=is_sys)
    app.dependency_overrides[get_current_tenant_user] = lambda: _tenant(role=role)
    app.dependency_overrides[get_plan_context] = all_features
    return TestClient(app, raise_server_exceptions=False)


def _rule() -> MagicMock:
    rule = MagicMock()
    rule.id = uuid.uuid4()
    rule.name = "rule"
    rule.enabled = True
    rule.condition_level = "error"
    rule.condition_min_count = 1
    rule.condition_window_seconds = 60
    rule.action_type = "in_app"
    rule.webhook_url = None
    rule.cooldown_seconds = 60
    rule.created_at = datetime(2025, 1, 1, tzinfo=UTC)
    rule.updated_at = datetime(2025, 1, 1, tzinfo=UTC)
    return rule


def _endpoint() -> MagicMock:
    ep = MagicMock()
    ep.id = uuid.uuid4()
    ep.url = "https://hooks.example.com"
    ep.events = ["run_failed"]
    ep.description = "endpoint"
    ep.auto_disabled = False
    ep.consecutive_dead_letter_count = 0
    ep.team_id = None
    return ep


# ---------------------------------------------------------------------------
# org_settings
# ---------------------------------------------------------------------------


def test_org_settings_http_flows() -> None:
    session = _session()
    client = _client(session)
    org = SimpleNamespace(
        settings_json={"cost_controls": {"currency": "EUR"}},
        guardrails_kill_switch=True,
        guardrails_kill_switch_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    with patch("modulo.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=org):
        resp = client.get("/api/v1/org/settings")
        assert resp.status_code == 200
        assert resp.json()["currency"] == "EUR"
        resp = client.get("/api/v1/org/settings/guardrails/kill-switch")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    for bad in (
        {},
        {"cost_controls": {}},
        {"cost_controls": {"currency": "BAD"}},
        {"cost_controls": "str"},
        {"cost_controls": {"currency": 123}},
    ):
        with patch(
            "modulo.api.routes.org_settings.get_organisation",
            new_callable=AsyncMock,
            return_value=SimpleNamespace(settings_json=bad),
        ):
            resp = client.get("/api/v1/org/settings")
            assert resp.status_code == 200
            assert resp.json()["currency"] == "USD"

    with patch("modulo.api.routes.org_settings.get_organisation", new_callable=AsyncMock, return_value=None):
        assert client.get("/api/v1/org/settings").status_code == 200
        assert client.get("/api/v1/org/settings/guardrails/kill-switch").status_code == 404

    for exc, expected in (
        (ProgrammingError("", {}, Exception()), 501),
        (SQLAlchemyError("db"), 503),
        (RuntimeError("boom"), 500),
    ):
        broken = _client(_session(begin_error=exc))
        assert broken.get("/api/v1/org/settings").status_code == expected
        assert broken.get("/api/v1/org/settings/guardrails/kill-switch").status_code == expected


# ---------------------------------------------------------------------------
# contributions
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_contributions_all_endpoints_and_error_handlers() -> None:
    principal = _tenant()
    session = _session()
    req = contributions.ContributeFixtureRequest(name="f", slug="f", fixture_map={"a": "b"})
    prim = SimpleNamespace(
        id=uuid.uuid4(),
        contribution_status="draft",
        visibility="org",
        name="f",
        slug="f",
        version="1.0",
        account_id=USER_ID,
    )
    with (
        patch.object(contributions, "set_rls_org", AsyncMock()),
        patch.object(contributions, "set_rls_user_context", AsyncMock()),
        patch.object(contributions, "contribute_fixture", AsyncMock(return_value=prim)),
        patch.object(contributions, "submit_contribution_for_review", AsyncMock(return_value=prim)),
        patch.object(contributions, "publish_contribution", AsyncMock(return_value=prim)),
        patch.object(contributions, "submit_contribution_version", AsyncMock(return_value=prim)),
        patch.object(contributions, "list_contribution_versions", AsyncMock(return_value=[prim])),
    ):
        assert (await contributions.create_contribution(req, session, principal)).id == prim.id
        assert (await contributions.submit_for_review(prim.id, session, principal)).id == prim.id
        assert (await contributions.publish_contribution_endpoint(prim.id, session, principal)).id == prim.id
        assert (
            await contributions.submit_contribution_version_endpoint(prim.id, req, session, principal)
        ).id == prim.id
        assert (await contributions.list_contribution_versions_endpoint(prim.id, session, principal)).total == 1

    calls = (
        (contributions.create_contribution, (req,)),
        (contributions.submit_for_review, (prim.id,)),
        (contributions.publish_contribution_endpoint, (prim.id,)),
        (contributions.submit_contribution_version_endpoint, (prim.id, req)),
        (contributions.list_contribution_versions_endpoint, (prim.id,)),
        (contributions.list_contributions_endpoint, (1, 20, None)),
    )
    for func, prefix in calls:
        for exc, expected in (
            (ProgrammingError("", {}, Exception()), 501),
            (SQLAlchemyError("db"), 503),
            (RuntimeError("x"), 500),
        ):
            broken = _session(begin_error=exc)
            if func is contributions.create_contribution:
                call = func(prefix[0], broken, principal)
            elif func is contributions.submit_contribution_version_endpoint:
                call = func(prefix[0], prefix[1], broken, principal)
            elif func is contributions.list_contributions_endpoint:
                call = func(*prefix, broken, principal)
            else:
                call = func(prefix[0], broken, principal)
            with (
                patch.object(contributions, "set_rls_org", AsyncMock()),
                patch.object(contributions, "set_rls_user_context", AsyncMock()),
                pytest.raises(HTTPException) as caught,
            ):
                await call
            assert caught.value.status_code == expected


# ---------------------------------------------------------------------------
# error_notification_rules
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_error_notification_rules_handlers_and_branches() -> None:
    principal = _tenant()
    plan = MagicMock()
    plan.feature_enabled.return_value = True
    rule = _rule()
    session = _session(results=[_result(scalars=[rule]), _result(scalar_one=1)])

    with patch.object(error_notification_rules, "set_rls_org", AsyncMock()):
        listed = await error_notification_rules.list_notification_rules(
            limit=20, offset=0, session=session, principal=principal
        )
        assert listed["total"] == 1

    session.execute = AsyncMock(return_value=_result(scalar_one=0))
    with (
        patch.object(error_notification_rules, "set_rls_org", AsyncMock()),
        patch.object(error_notification_rules, "_validate_webhook_url", AsyncMock()),
    ):
        created = await error_notification_rules.create_notification_rule(
            error_notification_rules.ErrorNotificationRuleCreate(name="n", action_type="in_app"),
            session,
            principal,
            plan,
        )
        assert created["name"] == "n"

    session.execute = AsyncMock(return_value=_result(scalar_one=10))
    with patch.object(error_notification_rules, "set_rls_org", AsyncMock()), pytest.raises(HTTPException) as caught:
        await error_notification_rules.create_notification_rule(
            error_notification_rules.ErrorNotificationRuleCreate(name="n", action_type="in_app"),
            session,
            principal,
            plan,
        )
    assert caught.value.status_code == 422

    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=rule))
    with patch.object(error_notification_rules, "set_rls_org", AsyncMock()):
        up = await error_notification_rules.update_notification_rule(
            rule.id,
            error_notification_rules.ErrorNotificationRuleUpdate(
                name="updated",
                enabled=False,
                condition_level="warning",
                condition_min_count=3,
                condition_window_seconds=120,
                action_type="in_app",
                webhook_url=None,
                cooldown_seconds=15,
            ),
            session,
            principal,
            plan,
        )
        assert up["name"] == "updated"
        await error_notification_rules.delete_notification_rule(rule.id, session, principal)

    for exc, expected in (
        (ProgrammingError("", {}, Exception()), 501),
        (SQLAlchemyError("db"), 503),
        (RuntimeError("x"), 500),
    ):
        session_fail = _session(begin_error=exc)
        with patch.object(error_notification_rules, "set_rls_org", AsyncMock()), pytest.raises(HTTPException) as caught:
            await error_notification_rules.update_notification_rule(
                rule.id,
                error_notification_rules.ErrorNotificationRuleUpdate(name="fail"),
                session_fail,
                principal,
                plan,
            )
        assert caught.value.status_code == expected

    # Tier update check refusal
    with pytest.raises(HTTPException) as caught:
        error_notification_rules._validate_webhook_tier_for_update(
            error_notification_rules.ErrorNotificationRuleUpdate(action_type="webhook"), False
        )
    assert caught.value.status_code == 402
    with pytest.raises(HTTPException) as caught:
        error_notification_rules._validate_webhook_tier_for_update(
            error_notification_rules.ErrorNotificationRuleUpdate(webhook_url="https://x"), False
        )
    assert caught.value.status_code == 402

    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    with patch.object(error_notification_rules, "set_rls_org", AsyncMock()):
        with pytest.raises(HTTPException) as caught:
            await error_notification_rules.update_notification_rule(
                rule.id, error_notification_rules.ErrorNotificationRuleUpdate(name="u"), session, principal, plan
            )
        assert caught.value.status_code == 404
        with pytest.raises(HTTPException) as caught:
            await error_notification_rules.delete_notification_rule(rule.id, session, principal)
        assert caught.value.status_code == 404

    # None org branches
    none_org_user = TenantPrincipal(username="u", organisation_id=None, account_id=USER_ID, org_role="admin")
    for call in (
        lambda: error_notification_rules.list_notification_rules(session=session, principal=none_org_user),
        lambda: error_notification_rules.create_notification_rule(
            error_notification_rules.ErrorNotificationRuleCreate(name="n"), session, none_org_user, plan
        ),
        lambda: error_notification_rules.update_notification_rule(
            rule.id, error_notification_rules.ErrorNotificationRuleUpdate(name="n"), session, none_org_user, plan
        ),
        lambda: error_notification_rules.delete_notification_rule(rule.id, session, none_org_user),
    ):
        with pytest.raises(HTTPException) as caught:
            await call()
        assert caught.value.status_code == 400

    # Error branches on list/create/delete
    for func, prefix in (
        (error_notification_rules.list_notification_rules, ()),
        (
            error_notification_rules.create_notification_rule,
            (error_notification_rules.ErrorNotificationRuleCreate(name="n"),),
        ),
        (error_notification_rules.delete_notification_rule, (rule.id,)),
    ):
        for exc, expected in (
            (ProgrammingError("", {}, Exception()), 501),
            (SQLAlchemyError("db"), 503),
            (RuntimeError("x"), 500),
        ):
            broken = _session(begin_error=exc)
            if func is error_notification_rules.list_notification_rules:
                call = func(session=broken, principal=principal)
            elif func is error_notification_rules.create_notification_rule:
                call = func(prefix[0], broken, principal, plan)
            else:
                call = func(prefix[0], broken, principal)
            with (
                patch.object(error_notification_rules, "set_rls_org", AsyncMock()),
                pytest.raises(HTTPException) as caught,
            ):
                await call
            assert caught.value.status_code == expected


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_notifications_all_routes_and_error_mappings() -> None:
    principal = _tenant()
    settings = _settings()
    ep = _endpoint()
    session = _session(results=[_result(scalars=[ep])])

    with (
        patch.object(notifications, "set_rls_org", AsyncMock()),
        patch.object(notifications, "set_rls_user_context", AsyncMock()),
    ):
        assert len(await notifications.list_endpoints(session, principal)) == 1

    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=ep))
    with (
        patch.object(notifications, "set_rls_org", AsyncMock()),
        patch.object(notifications, "set_rls_user_context", AsyncMock()),
        patch.object(notifications, "validate_outbound_url_async", AsyncMock()),
    ):
        assert (await notifications.get_endpoint(ep.id, session, principal)).id == ep.id
        updated = await notifications.update_endpoint(
            ep.id,
            notifications.NotificationEndpointUpdate(
                url="https://hooks.example.com/new",
                secret="new-secret",
                events=["run_failed"],
                description="desc",
                team_id=str(uuid.uuid4()),
            ),
            session,
            principal,
            settings,
        )
        assert updated.url == "https://hooks.example.com/new"
        assert (await notifications.restore_endpoint(ep.id, session, principal)).id == ep.id

    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=ep.id))
    with (
        patch.object(notifications, "set_rls_org", AsyncMock()),
        patch.object(notifications, "set_rls_user_context", AsyncMock()),
    ):
        await notifications.delete_endpoint(ep.id, session, principal)

    session.execute = AsyncMock(return_value=_result(scalar_one_or_none=None))
    for call in (
        lambda: notifications.get_endpoint(ep.id, session, principal),
        lambda: notifications.update_endpoint(
            ep.id, notifications.NotificationEndpointUpdate(), session, principal, settings
        ),
        lambda: notifications.delete_endpoint(ep.id, session, principal),
        lambda: notifications.restore_endpoint(ep.id, session, principal),
    ):
        with (
            patch.object(notifications, "set_rls_org", AsyncMock()),
            patch.object(notifications, "set_rls_user_context", AsyncMock()),
            pytest.raises(HTTPException) as caught,
        ):
            await call()
        assert caught.value.status_code == 404

    # Non-admin team endpoint rejection
    viewer = _tenant(role="viewer")
    with pytest.raises(HTTPException) as caught:
        await notifications.create_endpoint(
            notifications.NotificationEndpointCreate(url="https://hooks.example.com", team_id=str(uuid.uuid4())),
            session,
            viewer,
            settings,
        )
    assert caught.value.status_code == 403

    with pytest.raises(HTTPException) as caught:
        await notifications.update_endpoint(
            ep.id,
            notifications.NotificationEndpointUpdate(team_id=str(uuid.uuid4())),
            session,
            viewer,
            settings,
        )
    assert caught.value.status_code == 403

    # All DB error mappings
    calls = (
        (notifications.list_endpoints, ()),
        (notifications.create_endpoint, (notifications.NotificationEndpointCreate(url="https://hooks.example.com"),)),
        (notifications.get_endpoint, (ep.id,)),
        (notifications.update_endpoint, (ep.id, notifications.NotificationEndpointUpdate())),
        (notifications.delete_endpoint, (ep.id,)),
        (notifications.restore_endpoint, (ep.id,)),
    )
    for func, prefix in calls:
        for exc, expected in (
            (ProgrammingError("", {}, Exception()), 501),
            (SQLAlchemyError("db"), 503),
            (RuntimeError("x"), 500),
        ):
            broken = _session(begin_error=exc)
            if func is notifications.list_endpoints:
                call = func(broken, principal)
            elif func is notifications.create_endpoint:
                call = func(prefix[0], broken, principal, settings)
            elif func is notifications.update_endpoint:
                call = func(prefix[0], prefix[1], broken, principal, settings)
            else:
                call = func(prefix[0], broken, principal)
            with (
                patch.object(notifications, "set_rls_org", AsyncMock()),
                patch.object(notifications, "set_rls_user_context", AsyncMock()),
                pytest.raises(HTTPException) as caught,
            ):
                await call
            assert caught.value.status_code == expected


# ---------------------------------------------------------------------------
# admin_email
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_admin_email_full_paths_and_error_mappings() -> None:
    auth = _auth()
    org_id = uuid.uuid4()
    org = SimpleNamespace(settings_json={"email": {"smtp_host": "smtp.example.com", "smtp_password": "enc"}})
    session = _session()

    with (
        patch.object(admin_email, "get_organisation", AsyncMock(return_value=org)),
        patch.object(admin_email, "set_rls_org", AsyncMock()),
        patch.object(admin_email, "update_org_settings_patch", AsyncMock(return_value={})),
    ):
        get_res = await admin_email.admin_get_email_settings(org_id, auth, session)
        assert get_res.smtp_host == "smtp.example.com"

        put_res = await admin_email.admin_update_email_settings(
            org_id,
            admin_email.EmailSettingsUpdate(smtp_host="smtp.new", clear_password=True),
            auth,
            session,
            _settings(),
        )
        assert put_res.smtp_host == "smtp.new"

    # Not found branches
    with (
        patch.object(admin_email, "get_organisation", AsyncMock(return_value=None)),
        patch.object(admin_email, "set_rls_org", AsyncMock()),
    ):
        with pytest.raises(HTTPException) as caught:
            await admin_email.admin_get_email_settings(org_id, auth, session)
        assert caught.value.status_code == 404

        with pytest.raises(HTTPException) as caught:
            await admin_email.admin_update_email_settings(
                org_id, admin_email.EmailSettingsUpdate(), auth, session, _settings()
            )
        assert caught.value.status_code == 404

    # DB error paths on get and update fetch
    for exc, expected in (
        (ProgrammingError("", {}, Exception()), 501),
        (SQLAlchemyError("db"), 503),
        (RuntimeError("x"), 500),
    ):
        broken = _session(begin_error=exc)
        with pytest.raises(HTTPException) as caught:
            await admin_email.admin_get_email_settings(org_id, auth, broken)
        assert caught.value.status_code == expected

        with patch.object(admin_email, "set_rls_org", AsyncMock()), pytest.raises(HTTPException) as caught:
            await admin_email.admin_update_email_settings(
                org_id, admin_email.EmailSettingsUpdate(), auth, broken, _settings()
            )
        assert caught.value.status_code == expected

    # DB error on second transaction of update
    for exc, expected in (
        (ProgrammingError("", {}, Exception()), 501),
        (SQLAlchemyError("db"), 503),
        (RuntimeError("x"), 500),
    ):
        with (
            patch.object(admin_email, "get_organisation", AsyncMock(return_value=org)),
            patch.object(admin_email, "set_rls_org", AsyncMock()),
            patch.object(admin_email, "update_org_settings_patch", AsyncMock(side_effect=exc)),
            pytest.raises(HTTPException) as caught,
        ):
            await admin_email.admin_update_email_settings(
                org_id, admin_email.EmailSettingsUpdate(smtp_host="h"), auth, session, _settings()
            )
        assert caught.value.status_code == expected

    # test email helpers
    for exc, expected in (
        (ProgrammingError("", {}, Exception()), 501),
        (SQLAlchemyError("db"), 503),
        (RuntimeError("x"), 500),
    ):
        with pytest.raises(HTTPException) as caught:
            await admin_email._load_org_for_test_email(_session(begin_error=exc), org_id)
        assert caught.value.status_code == expected

    with pytest.raises(HTTPException):
        admin_email._validated_test_email_config(
            SimpleNamespace(settings_json={}), admin_email.TestEmailRequest(to="x@test.com")
        )

    with patch.object(admin_email, "decode_stored_secret_scoped", AsyncMock(side_effect=RuntimeError("x"))):
        with pytest.raises(HTTPException) as caught:
            await admin_email._build_test_email_settings(session, {"smtp_host": "h"}, _settings(), org_id)
        assert caught.value.status_code == 500

    built = SimpleNamespace()
    with patch.object(admin_email, "send_email", MagicMock(side_effect=EmailSendingError("boom"))):
        assert (await admin_email._send_test_email(built, "x@test.com"))["ok"] is False
    with patch.object(admin_email, "send_email", MagicMock(side_effect=RuntimeError("boom"))):
        assert (await admin_email._send_test_email(built, "x@test.com"))["ok"] is False


# ---------------------------------------------------------------------------
# admin_system_config
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_admin_system_config_full_paths_and_error_mappings() -> None:
    principal = _auth()
    entry = SystemConfig(key="k", value="v")
    session = _session(results=[_result(scalars=[entry])])

    listed = await admin_system_config.admin_list_config(principal, session)
    assert listed[0].key == "k"

    with patch.object(admin_system_config, "update_config", AsyncMock(return_value=entry)):
        set_res = await admin_system_config.admin_set_config(
            "k", admin_system_config.SetConfigRequest(value="v"), principal, session
        )
        assert set_res.key == "k"

    with patch.object(admin_system_config, "delete_config", AsyncMock(return_value=True)):
        assert await admin_system_config.admin_delete_config("k", principal, session) is None

    with (
        patch.object(admin_system_config, "delete_config", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as caught,
    ):
        await admin_system_config.admin_delete_config("missing", principal, session)
    assert caught.value.status_code == 404

    # DB error paths
    cases = (
        (admin_system_config.admin_list_config, (principal,)),
        (
            admin_system_config.admin_set_config,
            ("k", admin_system_config.SetConfigRequest(value="v"), principal),
        ),
        (admin_system_config.admin_delete_config, ("k", principal)),
    )
    for func, prefix in cases:
        for exc, expected in (
            (ProgrammingError("", {}, Exception()), 501),
            (SQLAlchemyError("db"), 503),
            (RuntimeError("x"), 500),
        ):
            broken = _session(begin_error=exc)
            if func is admin_system_config.admin_list_config:
                call = func(prefix[0], broken)
            elif func is admin_system_config.admin_set_config:
                call = func(prefix[0], prefix[1], prefix[2], broken)
            else:
                call = func(prefix[0], prefix[1], broken)
            with pytest.raises(HTTPException) as caught:
                await call
            assert caught.value.status_code == expected

    # IntegrityError specifically on set_config
    with pytest.raises(HTTPException) as caught:
        await admin_system_config.admin_set_config(
            "k",
            admin_system_config.SetConfigRequest(value="v"),
            principal,
            _session(begin_error=IntegrityError("", {}, Exception())),
        )
    assert caught.value.status_code == 409
