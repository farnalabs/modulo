"""FAR-1464 — every converted route-local ``except SQLAlchemyError`` arm must
surface an ``InvalidRequestError`` (a client-side session-contract violation)
as HTTP 500, never the arm's own 503 ``db_transient``.

The PR sweeps ~430 arms across the ``modulo.api`` route layer.  A handful of
representative arms are exercised end-to-end in
``test_far1464_session_contract_guard.py``; this module drives the REMAINING
converted arms so the changed-lines coverage gate measures the new guard call
on each.  Each test invokes the endpoint (or helper) directly with a session
whose FIRST DB operation raises ``InvalidRequestError`` and asserts the shared
guard converts it to a 500 with ``MSG_SESSION_CONTRACT`` (never a 503).

These are coverage-of-intent tests: they pin "this arm calls the guard", not
the arm's full happy path (existing per-route suites own that).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy.exc import InvalidRequestError

from modulo.api import dependencies as deps
from modulo.api.db_error_handling import MSG_SESSION_CONTRACT
from modulo.api.routes import (
    admin_email,
    admin_feature_flags,
    admin_housekeeping,
    admin_license,
    admin_notifications,
    admin_orgs,
    admin_run_retention,
    admin_system_config,
    admin_tiers,
    agents,
    api_keys,
    audit,
    community_library,
    contributions,
    environment_profiles,
    error_forwarder_config,
    evals,
    mcp_oauth,
    metrics,
    model_backends,
    notifications,
    org_settings,
    product_analytics_identity,
    runners,
    runs,
    slack,
    sso,
    viewmodel,
)
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.settings import Settings

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_FERNET_KEY = Fernet.generate_key().decode()


def _session_error() -> InvalidRequestError:
    return InvalidRequestError("Autobegin is disabled on this Session; please call session.begin()")


class _OkBeginContext:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _RaisingBeginContext:
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def __aenter__(self) -> None:
        raise self._exc

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _SecondBeginRaisesSession:
    """Begin transaction succeeds; a LATER ``session.begin()`` raises.

    Models the two-transaction route body (fetch, then write) where the first
    transaction works and the second is the one hitting the session-contract
    violation.
    """

    def __init__(self, *, fail_after: int = 1) -> None:
        self._begins = 0
        self._fail_after = fail_after
        self._exc = _session_error()

    def begin(self) -> object:
        self._begins += 1
        if self._begins > self._fail_after:
            return _RaisingBeginContext(self._exc)
        return _OkBeginContext()


class _RaisingSession:
    """Session whose first DB touch raises ``InvalidRequestError``.

    ``begin()`` / ``begin_nested()`` return *self* so ``async with session.begin()``
    raises at ``__aenter__``; every async DB operation raises directly.
    """

    def __init__(self) -> None:
        self._exc = _session_error()

    def begin(self) -> _RaisingSession:
        return self

    def begin_nested(self) -> _RaisingSession:
        return self

    def in_transaction(self) -> bool:
        # FAR-1516: pre-auth routes read this sync guard before their first
        # ``begin()``; report an active transaction so the route's own
        # session-contract arm (not an early AttributeError) is exercised.
        return True

    async def __aenter__(self) -> Self:
        raise self._exc

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, *args: object, **kwargs: object) -> object:
        raise self._exc

    async def scalar(self, *args: object, **kwargs: object) -> object:
        raise self._exc

    async def scalars(self, *args: object, **kwargs: object) -> object:
        raise self._exc

    async def get(self, *args: object, **kwargs: object) -> object:
        raise self._exc

    async def commit(self) -> None:
        raise self._exc

    async def rollback(self) -> None:
        raise self._exc

    async def flush(self) -> None:
        raise self._exc

    async def refresh(self, *args: object, **kwargs: object) -> None:
        raise self._exc

    async def delete(self, *args: object, **kwargs: object) -> None:
        raise self._exc

    async def close(self) -> None:
        raise self._exc

    def add(self, *args: object, **kwargs: object) -> None:
        raise self._exc

    def add_all(self, *args: object, **kwargs: object) -> None:
        raise self._exc


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": "a" * 32,
        "fernet_key": _FERNET_KEY,
        "modulo_admin_password": "testpass",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _tenant(org_role: str = "admin", org_id: uuid.UUID | None = _ORG_ID) -> TenantPrincipal:
    return TenantPrincipal(
        username="user@test",
        organisation_id=org_id,
        account_id=_ACCOUNT_ID,
        org_role=org_role,
    )


def _principal(*, system_admin: bool = False, org_id: uuid.UUID | None = _ORG_ID) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        username="admin@test",
        organisation_id=org_id,
        account_id=_ACCOUNT_ID,
        org_role="admin",
        is_system_admin=system_admin,
    )


async def _assert_500(coro: object) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await coro  # type: ignore[misc]
    assert excinfo.value.status_code == 500, excinfo.value.detail
    assert excinfo.value.detail == MSG_SESSION_CONTRACT


# ---------------------------------------------------------------------------
# admin_system_config
# ---------------------------------------------------------------------------


async def test_admin_system_config_arms_guard_session_contract() -> None:
    session = _RaisingSession()
    await _assert_500(admin_system_config.admin_list_config(_current_user=_principal(), session=session))
    await _assert_500(
        admin_system_config.admin_set_config(
            key="k",
            req=admin_system_config.SetConfigRequest.model_construct(value="v"),
            current_user=_principal(),
            session=session,
        )
    )
    await _assert_500(admin_system_config.admin_delete_config(key="k", _current_user=_principal(), session=session))


# ---------------------------------------------------------------------------
# admin_tiers
# ---------------------------------------------------------------------------


class _FakeRedis:
    async def get(self, _key: str) -> None:
        return None

    async def setex(self, *_args: object) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _FakeRedisFactory:
    @staticmethod
    def from_url(*_args: object, **_kwargs: object) -> _FakeRedis:
        return _FakeRedis()


async def test_admin_tiers_arm_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_tiers, "Redis", _FakeRedisFactory)
    await _assert_500(
        admin_tiers.list_tiers_endpoint(settings=_settings(), current_user=_tenant(), session=_RaisingSession())
    )


# ---------------------------------------------------------------------------
# admin_license
# ---------------------------------------------------------------------------


async def test_admin_license_arm_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_license, "_read_license_cache", AsyncMock(return_value=None))
    await _assert_500(
        admin_license.get_license_status(
            settings=_settings(), session=_RaisingSession(), current_user=_tenant(org_role="admin")
        )
    )


# ---------------------------------------------------------------------------
# admin_email
# ---------------------------------------------------------------------------


async def test_admin_email_arms_guard_session_contract() -> None:
    org_id = uuid.uuid4()
    await _assert_500(admin_email.admin_get_email_settings(org_id=org_id, _=_principal(), session=_RaisingSession()))
    await _assert_500(
        admin_email.admin_update_email_settings(
            org_id=org_id, req=None, _=_principal(), session=_RaisingSession(), settings=_settings()
        )
    )
    await _assert_500(
        admin_email.admin_test_email_settings(
            org_id=org_id, req=None, _=_principal(), session=_RaisingSession(), settings=_settings()
        )
    )


async def test_admin_email_update_write_arm_guard_session_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_email, "get_organisation", AsyncMock(return_value=SimpleNamespace(settings_json={})))
    req = SimpleNamespace(
        smtp_host="smtp.test",
        smtp_port=587,
        smtp_username="u",
        clear_password=True,
        smtp_password=None,
        email_from="a@b.c",
        smtp_timeout=30,
    )
    await _assert_500(
        admin_email.admin_update_email_settings(
            org_id=uuid.uuid4(),
            req=req,
            _=_principal(),
            session=_SecondBeginRaisesSession(),
            settings=_settings(),
        )
    )


# ---------------------------------------------------------------------------
# admin_feature_flags
# ---------------------------------------------------------------------------


async def test_admin_feature_flags_arms_guard_session_contract() -> None:
    user = _principal()
    await _assert_500(
        admin_feature_flags.get_feature_flag(
            flag_name="f", settings=_settings(), session=_RaisingSession(), current_user=user
        )
    )
    await _assert_500(
        admin_feature_flags.toggle_feature_flag(
            flag_name="f",
            req=SimpleNamespace(enabled=True),
            settings=_settings(),
            session=_RaisingSession(),
            current_user=user,
        )
    )
    await _assert_500(
        admin_feature_flags.get_org_flag_override(flag_name="f", current_user=user, session=_RaisingSession())
    )
    await _assert_500(
        admin_feature_flags.set_org_flag_override(
            flag_name="f",
            req=SimpleNamespace(enabled=True),
            settings=_settings(),
            current_user=user,
            session=_RaisingSession(),
        )
    )
    await _assert_500(
        admin_feature_flags.clear_org_flag_override(
            flag_name="f", settings=_settings(), current_user=user, session=_RaisingSession()
        )
    )


# ---------------------------------------------------------------------------
# admin_orgs
# ---------------------------------------------------------------------------


async def test_admin_orgs_arms_guard_session_contract() -> None:
    await _assert_500(
        admin_orgs.admin_set_org_license(org_id=uuid.uuid4(), req=None, _=_principal(), session=_RaisingSession())
    )
    await _assert_500(
        admin_orgs.admin_remove_org_license(org_id=uuid.uuid4(), _=_principal(), session=_RaisingSession())
    )


async def test_admin_orgs_set_license_inner_fetch_arm_guard_session_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_orgs, "get_organisation", AsyncMock(side_effect=_session_error()))
    await _assert_500(
        admin_orgs.admin_set_org_license(
            org_id=uuid.uuid4(),
            req=None,
            _=_principal(),
            session=_SecondBeginRaisesSession(fail_after=99),
        )
    )


# ---------------------------------------------------------------------------
# admin_run_retention
# ---------------------------------------------------------------------------


async def test_admin_run_retention_arms_guard_session_contract() -> None:
    await _assert_500(admin_run_retention.candidates(session=_RaisingSession(), principal=_tenant(org_role="admin")))
    await _assert_500(
        admin_run_retention.purge(
            req=SimpleNamespace(
                confirm=True,
                organisation_id=None,
                date_from=None,
                date_to=None,
                pipeline_id=None,
                status=None,
            ),
            session=_RaisingSession(),
            principal=_tenant(org_role="admin"),
        )
    )


# ---------------------------------------------------------------------------
# admin_housekeeping
# ---------------------------------------------------------------------------


async def test_admin_housekeeping_arms_guard_session_contract() -> None:
    await _assert_500(
        admin_housekeeping.perform_cleanup(
            req=SimpleNamespace(items=[]),
            session=_RaisingSession(),
            principal=_tenant(),
            _sys=_tenant(),
        )
    )
    await _assert_500(
        admin_housekeeping.purge_checkpoints(
            req=SimpleNamespace(confirm=True, max_age_days=None),
            session=_RaisingSession(),
            principal=_tenant(),
            _sys=_tenant(),
        )
    )


# ---------------------------------------------------------------------------
# admin_notifications helpers
# ---------------------------------------------------------------------------


async def test_admin_notifications_arm_helpers_guard_session_contract() -> None:
    session = _RaisingSession()
    principal = _tenant()
    delivery = MagicMock()
    ep = MagicMock()
    resp = MagicMock()
    await _assert_500(admin_notifications._record_delivery_result(session, principal, delivery, ep, resp))
    await _assert_500(
        admin_notifications._record_delivery_error(session, principal, delivery, ep, RuntimeError("boom"))
    )


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------


async def test_agents_arms_guard_session_contract() -> None:
    await _assert_500(
        agents.replace_bindings_endpoint(
            agent_id=uuid.uuid4(),
            req=SimpleNamespace(bindings=[]),
            session=_RaisingSession(),
            principal=_tenant(),
        )
    )
    await _assert_500(
        agents.delete_binding_endpoint(
            agent_id=uuid.uuid4(),
            binding_id=uuid.uuid4(),
            session=_RaisingSession(),
            principal=_tenant(),
        )
    )


# ---------------------------------------------------------------------------
# api_keys
# ---------------------------------------------------------------------------


async def test_api_keys_arms_guard_session_contract() -> None:
    principal = _tenant()
    await _assert_500(api_keys._mint_api_key(_RaisingSession(), principal, "n", "admin", None, None, "org"))
    await _assert_500(api_keys.list_api_keys_endpoint(session=_RaisingSession(), principal=principal))
    await _assert_500(api_keys._apply_key_update(_RaisingSession(), uuid.uuid4(), principal, None, None, None, None))
    await _assert_500(
        api_keys.revoke_api_key_endpoint(key_id=uuid.uuid4(), session=_RaisingSession(), principal=principal)
    )


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


async def test_audit_arms_guard_session_contract() -> None:
    await _assert_500(
        audit.list_audit_events_endpoint(
            cursor=None,
            limit=50,
            event_type=None,
            actor_user_id=None,
            resource_type=None,
            from_date=None,
            to_date=None,
            session=_RaisingSession(),
            principal=_tenant(),
        )
    )
    await _assert_500(
        audit.batch_detail_endpoint(req=SimpleNamespace(event_ids=[]), session=_RaisingSession(), principal=_tenant())
    )


# ---------------------------------------------------------------------------
# community_library
# ---------------------------------------------------------------------------


async def test_community_library_arm_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(community_library, "_community_objects_enabled", AsyncMock(return_value=True))
    await _assert_500(
        community_library.install(
            entry_id="entry",
            req=SimpleNamespace(target_team_id=None),
            session=_RaisingSession(),
            principal=_tenant(),
        )
    )


# ---------------------------------------------------------------------------
# contributions
# ---------------------------------------------------------------------------


async def test_contributions_arms_guard_session_contract() -> None:
    principal = _tenant()
    await _assert_500(contributions.create_contribution(req=None, session=_RaisingSession(), principal=principal))
    await _assert_500(
        contributions.submit_for_review(primitive_id=uuid.uuid4(), session=_RaisingSession(), principal=principal)
    )
    await _assert_500(
        contributions.publish_contribution_endpoint(
            primitive_id=uuid.uuid4(), session=_RaisingSession(), principal=principal
        )
    )
    await _assert_500(
        contributions.submit_contribution_version_endpoint(
            primitive_id=uuid.uuid4(), req=None, session=_RaisingSession(), principal=principal
        )
    )
    await _assert_500(
        contributions.list_contribution_versions_endpoint(
            primitive_id=uuid.uuid4(), session=_RaisingSession(), principal=principal
        )
    )
    await _assert_500(
        contributions.list_contributions_endpoint(
            page=1, page_size=20, contribution_status=None, session=_RaisingSession(), principal=principal
        )
    )


# ---------------------------------------------------------------------------
# environment_profiles
# ---------------------------------------------------------------------------


async def test_environment_profiles_arm_guard_session_contract() -> None:
    await _assert_500(
        environment_profiles.restore_profile(profile_id=uuid.uuid4(), session=_RaisingSession(), principal=_tenant())
    )


# ---------------------------------------------------------------------------
# error_forwarder_config
# ---------------------------------------------------------------------------


async def test_error_forwarder_config_arms_guard_session_contract() -> None:
    await _assert_500(error_forwarder_config._merge_stored_forwarder_config(_RaisingSession(), _ORG_ID, "sentry", {}))
    await _assert_500(
        error_forwarder_config.delete_forwarder(forwarder_type="sentry", session=_RaisingSession(), principal=_tenant())
    )
    await _assert_500(
        error_forwarder_config.restore_forwarder(
            forwarder_type="sentry", session=_RaisingSession(), principal=_tenant()
        )
    )


# ---------------------------------------------------------------------------
# evals helpers
# ---------------------------------------------------------------------------


async def test_evals_arm_helpers_guard_session_contract() -> None:
    principal = _tenant()
    await _assert_500(evals._fetch_eval_definitions(set(), _RaisingSession(), principal))
    await _assert_500(evals._insert_eval_definition(_RaisingSession(), principal, None, None, {}))


# ---------------------------------------------------------------------------
# mcp_oauth
# ---------------------------------------------------------------------------


async def test_mcp_oauth_arms_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_oauth, "public_url_is_configured", lambda settings: True)
    monkeypatch.setattr(mcp_oauth, "normalize_scopes", lambda joined: joined.split())
    monkeypatch.setattr(mcp_oauth, "normalize_redirect_uris", lambda uris: uris)
    principal = _tenant()
    req = SimpleNamespace(scopes=["read"], redirect_uris=["https://x/cb"], name="c")
    await _assert_500(
        mcp_oauth.register_oauth_client(req=req, session=_RaisingSession(), principal=principal, settings=_settings())
    )
    await _assert_500(mcp_oauth.list_oauth_clients_endpoint(session=_RaisingSession(), principal=principal))
    await _assert_500(mcp_oauth.remove_oauth_client(client_id="c", session=_RaisingSession(), principal=principal))
    await _assert_500(
        mcp_oauth.approve_consent(req=SimpleNamespace(state="s"), session=_RaisingSession(), principal=principal)
    )


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


async def test_metrics_arms_guard_session_contract() -> None:
    user = _tenant(org_role="viewer")
    await _assert_500(
        metrics.ingest_web_vitals(req=SimpleNamespace(events=[object()]), current_user=user, session=_RaisingSession())
    )
    await _assert_500(metrics.get_web_vitals_summary(days=7, current_user=user, session=_RaisingSession()))
    await _assert_500(
        metrics.get_web_vitals_timeseries(metric_name="LCP", days=7, current_user=user, session=_RaisingSession())
    )


# ---------------------------------------------------------------------------
# model_backends
# ---------------------------------------------------------------------------


async def test_model_backends_arms_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**kwargs: object) -> object:
        raise _session_error()

    monkeypatch.setattr(model_backends, "ModelBackendPresetResponse", _boom)
    await _assert_500(model_backends.list_model_backend_presets_endpoint(principal=_tenant()))
    await _assert_500(
        model_backends.recheck_model_backend_health_endpoint(
            backend_id=uuid.uuid4(),
            session=_RaisingSession(),
            principal=_tenant(),
            settings=_settings(),
        )
    )


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


async def test_notifications_arms_guard_session_contract() -> None:
    principal = _tenant()
    eid = uuid.uuid4()
    await _assert_500(notifications.list_endpoints(session=_RaisingSession(), principal=principal))
    await _assert_500(
        notifications.create_endpoint(
            req=SimpleNamespace(team_id=None, secret=None, url="https://x", events=[], description=None),
            session=_RaisingSession(),
            principal=principal,
            settings=_settings(),
        )
    )
    await _assert_500(notifications.get_endpoint(endpoint_id=eid, session=_RaisingSession(), principal=principal))
    await _assert_500(
        notifications.update_endpoint(
            endpoint_id=eid,
            req=SimpleNamespace(team_id=None),
            session=_RaisingSession(),
            principal=principal,
            settings=_settings(),
        )
    )
    await _assert_500(notifications.delete_endpoint(endpoint_id=eid, session=_RaisingSession(), principal=principal))
    await _assert_500(notifications.restore_endpoint(endpoint_id=eid, session=_RaisingSession(), principal=principal))


# ---------------------------------------------------------------------------
# org_settings
# ---------------------------------------------------------------------------


async def test_org_settings_arms_guard_session_contract() -> None:
    user = _tenant()
    await _assert_500(org_settings.get_org_settings(current_user=user, session=_RaisingSession()))
    await _assert_500(org_settings.get_org_guardrails_kill_switch(current_user=user, session=_RaisingSession()))


# ---------------------------------------------------------------------------
# product_analytics_identity
# ---------------------------------------------------------------------------


async def test_product_analytics_identity_arms_guard_session_contract() -> None:
    await _assert_500(product_analytics_identity.get_identity(session=_RaisingSession(), _current_user=_principal()))
    await _assert_500(
        product_analytics_identity.rotate_identity_secret(
            req=SimpleNamespace(),
            request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")),
            session=_RaisingSession(),
            _current_user=_principal(),
        )
    )


# ---------------------------------------------------------------------------
# runners
# ---------------------------------------------------------------------------


async def test_runners_arms_guard_session_contract() -> None:
    await _assert_500(runners.get_runners_status(session=_RaisingSession(), principal=_tenant()))
    await _assert_500(runners.apply_template(profile_id=uuid.uuid4(), session=_RaisingSession(), principal=_tenant()))


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------


async def test_runs_arms_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    await _assert_500(
        runs.trigger_rerun(
            run_id=uuid.uuid4(),
            session=_RaisingSession(),
            _engine=MagicMock(),
            principal=_tenant(),
        )
    )
    monkeypatch.setattr("modulo.core.artifacts.store.get_store", lambda: MagicMock())
    await _assert_500(
        runs.get_run_artifact(
            run_id=uuid.uuid4(),
            node_id="n",
            attempt_key="a",
            stream="stdout",
            session=_RaisingSession(),
            principal=_tenant(),
        )
    )


# ---------------------------------------------------------------------------
# slack
# ---------------------------------------------------------------------------


class _SlackRequest:
    def __init__(self) -> None:
        self.headers = {"X-Slack-Signature": "sig", "X-Slack-Request-Timestamp": "123"}

    async def body(self) -> bytes:
        return b"{}"

    async def json(self) -> dict[str, object]:
        return {"type": "event_callback"}


async def test_slack_arm_guard_session_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(slack, "system_engine_is_fallback", lambda: False)
    await _assert_500(
        slack.receive_slack_event(
            trigger_id=uuid.uuid4(),
            request=_SlackRequest(),
            response=MagicMock(),
            background_tasks=MagicMock(),
            session=_RaisingSession(),
            system_session=_RaisingSession(),
            principal=None,
            _engine=MagicMock(),
        )
    )


# ---------------------------------------------------------------------------
# sso
# ---------------------------------------------------------------------------


class _FormRequest:
    def __init__(self, form: dict[str, object]) -> None:
        self._form = form
        # FAR-1516: pre-auth routes publish the audit org onto request.state.
        self.state = SimpleNamespace()

    async def form(self) -> dict[str, object]:
        return self._form


async def test_sso_arms_guard_session_contract() -> None:
    await _assert_500(
        sso.oidc_callback(
            provider="p",
            request=SimpleNamespace(query_params={"code": "c", "state": "s"}),
            _=None,
            settings=_settings(),
            session=_RaisingSession(),
            system_session=_RaisingSession(),
        )
    )
    await _assert_500(
        sso.saml_acs(
            request=_FormRequest({"SAMLResponse": "assertion"}),
            _=None,
            settings=_settings(),
            session=_RaisingSession(),
            system_session=_RaisingSession(),
        )
    )


# ---------------------------------------------------------------------------
# viewmodel
# ---------------------------------------------------------------------------


async def test_viewmodel_arms_guard_session_contract() -> None:
    user = _principal()
    await _assert_500(viewmodel.me(current_user=user, session=_RaisingSession()))
    await _assert_500(
        viewmodel.viewmodel_current(
            session=_RaisingSession(),
            current_user=user,
            settings=_settings(),
            view_as_team=None,
            current_view_id=None,
        )
    )
    await _assert_500(
        viewmodel.viewmodel_list_views(session=_RaisingSession(), current_user=user, page=1, page_size=100)
    )


# ---------------------------------------------------------------------------
# api.dependencies closures
# ---------------------------------------------------------------------------


async def test_dependencies_target_org_role_check_guard_session_contract() -> None:
    dep = deps.require_target_org_role("org.email.view", "operator")
    request = SimpleNamespace(path_params={"org_id": str(_ORG_ID)})
    await _assert_500(dep.dependency(request, current_user=_principal(), session=_RaisingSession()))


async def test_dependencies_team_scope_check_guard_session_contract() -> None:
    dep = deps._team_membership_or_admin_dep(AsyncMock(), deps.get_current_tenant_user)
    await _assert_500(
        dep.dependency(request=SimpleNamespace(), principal=_tenant(org_role="operator"), session=_RaisingSession())
    )
