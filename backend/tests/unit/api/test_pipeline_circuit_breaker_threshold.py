"""FAR-1182: configurable, Community-tier pipeline spend circuit breaker.

Covers the shared threshold validator, the CRUD create/update writers (incl.
the ``pipeline.circuit_breaker_threshold_changed`` audit event), the REST
create/update/get surface, the MCP create + set tools, and an enforcement
regression proving a threshold set through the new surface trips the breaker.

FAR-1184: raising or clearing the threshold requires ``cost.manage`` — the
shared ``circuit_breaker_threshold_change_allowed`` rule is exercised on every
surface (REST create + PATCH, MCP create + set), with the
``pipeline.circuit_breaker_threshold_change_denied`` audit on each refusal.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.core.cost_controller import check_pipeline_circuit_breaker
from modulo.db.crud.pipeline import (
    CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT,
    CIRCUIT_BREAKER_THRESHOLD_CHANGED_EVENT,
    CircuitBreakerThresholdChangeDenied,
    circuit_breaker_threshold_change_allowed,
    create_pipeline,
    normalize_circuit_breaker_threshold,
    update_pipeline,
)
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
_NOW = datetime(2026, 9, 1, tzinfo=UTC)
_CRUD = "modulo.db.crud.pipeline."
_ROUTES = "modulo.api.routes.pipelines."


# ---------------------------------------------------------------------------
# normalize_circuit_breaker_threshold
# ---------------------------------------------------------------------------


class TestNormalizeThreshold:
    def test_none_disables(self) -> None:
        assert normalize_circuit_breaker_threshold(None) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (50, Decimal("50.000000")),
            (12.5, Decimal("12.500000")),
            ("0.000001", Decimal("0.000001")),
            (Decimal("1.2345675"), Decimal("1.234568")),
        ],
    )
    def test_valid_values_quantized_to_column_scale(self, value: object, expected: Decimal) -> None:
        assert normalize_circuit_breaker_threshold(value) == expected  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "value",
        [0, -1, 0.0000001, "abc", float("nan"), float("inf"), True, 100_000_000],
        ids=["zero", "negative", "rounds_to_zero", "not_a_number", "nan", "inf", "bool", "over_max"],
    )
    def test_invalid_values_rejected(self, value: object) -> None:
        with pytest.raises(ValueError, match="circuit_breaker_threshold"):
            normalize_circuit_breaker_threshold(value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FAR-1184: the shared raise/clear permission rule
# ---------------------------------------------------------------------------


class TestThresholdChangeAllowed:
    """The ONE shared rule: raise/clear needs cost.manage; lower/set/no-op don't."""

    def test_operator_lowering_allowed(self) -> None:
        assert circuit_breaker_threshold_change_allowed(Decimal(100), Decimal(50), may_manage_cost=False)

    def test_operator_setting_where_none_allowed(self) -> None:
        assert circuit_breaker_threshold_change_allowed(None, Decimal(50), may_manage_cost=False)

    def test_operator_noop_allowed(self) -> None:
        assert circuit_breaker_threshold_change_allowed(Decimal(50), Decimal(50), may_manage_cost=False)

    def test_create_without_threshold_is_noop_not_clear(self) -> None:
        # None -> None (create with no breaker) must never read as a "clear".
        assert circuit_breaker_threshold_change_allowed(None, None, may_manage_cost=False)

    def test_operator_raising_denied(self) -> None:
        assert not circuit_breaker_threshold_change_allowed(Decimal(50), Decimal(100), may_manage_cost=False)

    def test_operator_clearing_denied(self) -> None:
        assert not circuit_breaker_threshold_change_allowed(Decimal(50), None, may_manage_cost=False)

    def test_cost_manage_always_allowed(self) -> None:
        assert circuit_breaker_threshold_change_allowed(Decimal(50), Decimal(100), may_manage_cost=True)
        assert circuit_breaker_threshold_change_allowed(Decimal(50), None, may_manage_cost=True)

    def test_unexpected_previous_type_fails_closed(self) -> None:
        # A non-numeric stored value must never authorise a raise (TypeError
        # from the comparison -> deny), never crash-open.
        assert not circuit_breaker_threshold_change_allowed("100", Decimal(50), may_manage_cost=False)


# ---------------------------------------------------------------------------
# CRUD writers + audit
# ---------------------------------------------------------------------------


def _crud_session(pipeline: object | None = None) -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = pipeline
    session.execute = AsyncMock(return_value=result)
    return session


def _orm_like_pipeline(threshold: Decimal | None) -> MagicMock:
    pipeline = MagicMock()
    pipeline.id = _PIPELINE_ID
    pipeline.organisation_id = _ORG_ID
    pipeline.owner_team_id = None
    pipeline.circuit_breaker_threshold = threshold
    return pipeline


class TestCrudCreate:
    async def test_create_with_threshold_persists_and_audits(self) -> None:
        session = _crud_session()
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            pipeline = await create_pipeline(
                session,
                org_id=_ORG_ID,
                name="p",
                account_id=_USER_ID,
                circuit_breaker_threshold=25.5,
            )

        assert pipeline.circuit_breaker_threshold == Decimal("25.500000")
        audit.assert_awaited_once()
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGED_EVENT
        assert kwargs["actor_user_id"] == _USER_ID
        assert kwargs["payload_json"]["previous_threshold_usd"] is None
        assert kwargs["payload_json"]["new_threshold_usd"] == 25.5

    async def test_create_without_threshold_writes_no_audit(self) -> None:
        session = _crud_session()
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            pipeline = await create_pipeline(session, org_id=_ORG_ID, name="p", account_id=_USER_ID)

        assert pipeline.circuit_breaker_threshold is None
        audit.assert_not_awaited()

    async def test_create_rejects_invalid_threshold(self) -> None:
        session = _crud_session()
        with pytest.raises(ValueError, match="greater than 0"):
            await create_pipeline(session, org_id=_ORG_ID, name="p", account_id=_USER_ID, circuit_breaker_threshold=0)
        session.add.assert_not_called()


class TestCrudUpdate:
    async def test_set_threshold_audits_previous_and_new(self) -> None:
        pipeline = _orm_like_pipeline(None)
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            updated = await update_pipeline(
                _crud_session(pipeline),
                _PIPELINE_ID,
                {"circuit_breaker_threshold": 100},
                org_id=_ORG_ID,
                account_id=_USER_ID,
                request_id="req-1",
            )

        assert updated is pipeline
        assert pipeline.circuit_breaker_threshold == Decimal("100.000000")
        kwargs = audit.await_args.kwargs
        assert kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGED_EVENT
        assert kwargs["resource_id"] == _PIPELINE_ID
        assert kwargs["request_id"] == "req-1"
        assert kwargs["payload_json"] == {
            "previous_threshold_usd": None,
            "new_threshold_usd": 100.0,
            "changed_by": str(_USER_ID),
        }

    async def test_change_threshold_audits(self) -> None:
        pipeline = _orm_like_pipeline(Decimal("100.000000"))
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            await update_pipeline(
                _crud_session(pipeline),
                _PIPELINE_ID,
                {"circuit_breaker_threshold": 250.75},
                org_id=_ORG_ID,
                account_id=_USER_ID,
            )

        payload = audit.await_args.kwargs["payload_json"]
        assert payload["previous_threshold_usd"] == 100.0
        assert payload["new_threshold_usd"] == 250.75

    async def test_clear_threshold_disables_and_audits(self) -> None:
        pipeline = _orm_like_pipeline(Decimal("100.000000"))
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            await update_pipeline(
                _crud_session(pipeline),
                _PIPELINE_ID,
                {"circuit_breaker_threshold": None},
                org_id=_ORG_ID,
                account_id=_USER_ID,
            )

        assert pipeline.circuit_breaker_threshold is None
        assert audit.await_args.kwargs["payload_json"]["new_threshold_usd"] is None

    async def test_unchanged_threshold_writes_no_audit(self) -> None:
        pipeline = _orm_like_pipeline(Decimal("100.000000"))
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            await update_pipeline(
                _crud_session(pipeline),
                _PIPELINE_ID,
                {"circuit_breaker_threshold": 100},
                org_id=_ORG_ID,
                account_id=_USER_ID,
            )

        audit.assert_not_awaited()

    async def test_update_without_threshold_key_leaves_it_untouched(self) -> None:
        pipeline = _orm_like_pipeline(Decimal("100.000000"))
        audit = AsyncMock()
        with patch(f"{_CRUD}append_audit_event", new=audit):
            await update_pipeline(_crud_session(pipeline), _PIPELINE_ID, {"description": "x"})

        assert pipeline.circuit_breaker_threshold == Decimal("100.000000")
        audit.assert_not_awaited()

    async def test_update_rejects_invalid_threshold_before_mutating(self) -> None:
        pipeline = _orm_like_pipeline(Decimal("100.000000"))
        with pytest.raises(ValueError, match="greater than 0"):
            await update_pipeline(_crud_session(pipeline), _PIPELINE_ID, {"circuit_breaker_threshold": -5})
        assert pipeline.circuit_breaker_threshold == Decimal("100.000000")


# ---------------------------------------------------------------------------
# REST: create / update / get
# ---------------------------------------------------------------------------


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _response_pipeline(
    threshold: Decimal | None = None,
    *,
    tripped: bool = False,
    tripped_at: datetime | None = None,
) -> MagicMock:
    p = MagicMock()
    p.rate_limit_config = None
    p.retry_policy = {}
    p.max_duration_seconds = None
    p.archived_at = None
    p.snapshot_count = 0
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Test Pipeline"
    p.description = None
    p.visibility = "org"
    p.owner_team_id = None
    p.folder_id = None
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.stale_run_timeout_minutes = 30
    p.account_id = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    p.circuit_breaker_threshold = threshold
    p.circuit_breaker_tripped = tripped
    p.circuit_breaker_tripped_at = tripped_at
    return p


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser", organisation_id=_ORG_ID, account_id=_USER_ID, org_role="admin"
    )
    # Community plan: every plan feature off - the threshold must still work.
    community_plan = MagicMock()
    community_plan.feature_enabled.return_value = False
    community_plan.list_enabled_features.return_value = []
    app.dependency_overrides[get_plan_context] = lambda: community_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


def _route_patches(**extra: object) -> list:
    return [
        patch(f"{_ROUTES}set_rls_org"),
        patch(f"{_ROUTES}set_rls_user_context"),
        *(patch(f"{_ROUTES}{name}", new=value) for name, value in extra.items()),
    ]


def _enter(stack_patches: list) -> list:
    return [p.start() for p in stack_patches]


class TestRestSurface:
    def teardown_method(self) -> None:
        patch.stopall()

    def test_create_passes_threshold_and_returns_it(self, client: TestClient) -> None:
        create = AsyncMock(return_value=_response_pipeline(Decimal("40.000000")))
        _enter(_route_patches(create_pipeline=create))

        resp = client.post("/api/v1/pipelines", json={"name": "p", "circuit_breaker_threshold": 40})

        assert resp.status_code == 201, resp.text
        assert create.await_args.kwargs["circuit_breaker_threshold"] == 40.0
        body = resp.json()
        assert body["circuit_breaker_threshold"] == 40.0
        assert body["circuit_breaker_tripped"] is False

    def test_create_without_threshold_defaults_to_disabled(self, client: TestClient) -> None:
        create = AsyncMock(return_value=_response_pipeline(None))
        _enter(_route_patches(create_pipeline=create))

        resp = client.post("/api/v1/pipelines", json={"name": "p"})

        assert resp.status_code == 201, resp.text
        assert create.await_args.kwargs["circuit_breaker_threshold"] is None
        assert resp.json()["circuit_breaker_threshold"] is None

    @pytest.mark.parametrize("bad", [0, -10, 0.0000001, 1e9, "lots", True])
    def test_create_rejects_invalid_threshold(self, client: TestClient, bad: object) -> None:
        resp = client.post("/api/v1/pipelines", json={"name": "p", "circuit_breaker_threshold": bad})

        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize("bad", [0, -1, 1e9, True])
    def test_update_rejects_invalid_threshold(self, client: TestClient, bad: object) -> None:
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"circuit_breaker_threshold": bad})

        assert resp.status_code == 422, resp.text

    @pytest.mark.parametrize(("sent", "stored"), [(75.25, Decimal("75.250000")), (None, None)])
    def test_update_sets_or_clears_threshold(self, client: TestClient, sent: float | None, stored: Decimal) -> None:
        pipeline = _response_pipeline(stored)
        update = AsyncMock(return_value=pipeline)
        _enter(
            _route_patches(
                get_pipeline=AsyncMock(return_value=pipeline),
                update_pipeline=update,
                _assert_team_transition_allowed=AsyncMock(),
                _reapply_team_gate_inside_mutation_txn=AsyncMock(),
                append_audit_event=AsyncMock(),
            )
        )

        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"circuit_breaker_threshold": sent})

        assert resp.status_code == 200, resp.text
        updates = update.await_args.args[2]
        assert updates["circuit_breaker_threshold"] == sent
        assert update.await_args.kwargs["account_id"] == _USER_ID
        assert resp.json()["circuit_breaker_threshold"] == (float(stored) if stored is not None else None)

    def test_update_without_threshold_key_does_not_send_it(self, client: TestClient) -> None:
        pipeline = _response_pipeline(Decimal("10.000000"))
        update = AsyncMock(return_value=pipeline)
        _enter(
            _route_patches(
                get_pipeline=AsyncMock(return_value=pipeline),
                update_pipeline=update,
                _assert_team_transition_allowed=AsyncMock(),
                _reapply_team_gate_inside_mutation_txn=AsyncMock(),
                append_audit_event=AsyncMock(),
            )
        )

        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"description": "d"})

        assert resp.status_code == 200, resp.text
        assert "circuit_breaker_threshold" not in update.await_args.args[2]

    def test_get_exposes_tripped_state(self, client: TestClient) -> None:
        tripped_at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
        pipeline = _response_pipeline(Decimal("5.000000"), tripped=True, tripped_at=tripped_at)
        _enter(_route_patches(get_pipeline=AsyncMock(return_value=pipeline)))
        with patch("modulo.api.team_scope.resolve_pipeline_team_scope", new=AsyncMock(return_value=None)):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["circuit_breaker_threshold"] == 5.0
        assert body["circuit_breaker_tripped"] is True
        assert body["circuit_breaker_tripped_at"].startswith("2026-09-20T12:00:00")


# ---------------------------------------------------------------------------
# FAR-1184: REST PATCH / create — raise/clear requires cost.manage
# ---------------------------------------------------------------------------


class TestRestThresholdPermission:
    """FAR-1184: PATCH refuses raise/clear without cost.manage; create keeps working.

    Each refusal asserts 403 AND the ``..._change_denied`` audit — without
    the shared guard the raise/clear would return 200, so these tests fail
    when the guard is removed (prove-the-fix).
    """

    def teardown_method(self) -> None:
        patch.stopall()

    @staticmethod
    def _use_role(role: str) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role=role,
        )

    def _patch_threshold(
        self,
        client: TestClient,
        *,
        role: str,
        previous: Decimal | None,
        sent: float | None,
    ) -> tuple[Any, AsyncMock, AsyncMock]:
        """Drive PATCH /pipelines/{id} with the given role and stored threshold."""
        self._use_role(role)
        pipeline = _response_pipeline(previous)
        update = AsyncMock(return_value=_response_pipeline(sent if sent is not None else previous))
        denial_audit = AsyncMock()
        _enter(
            _route_patches(
                get_pipeline=AsyncMock(return_value=pipeline),
                update_pipeline=update,
                _assert_team_transition_allowed=AsyncMock(),
                _reapply_team_gate_inside_mutation_txn=AsyncMock(),
                append_audit_event=AsyncMock(),
                append_audit_event_isolated=denial_audit,
            )
        )
        # Team-gate membership leg for non-admin principals (the gate's other
        # legs are stubbed by the strict session's pipeline-row default).
        _enter([patch("modulo.api.dependencies.team_membership_exists", new=AsyncMock(return_value=True))])
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"circuit_breaker_threshold": sent})
        return resp, update, denial_audit

    def _assert_denied(
        self,
        resp: Any,
        denial_audit: AsyncMock,
        update: AsyncMock,
        *,
        previous: Decimal | None,
        new: float | None,
    ) -> None:
        assert resp.status_code == 403, resp.text
        assert "cost.manage" in resp.json()["detail"]
        update.assert_not_awaited()
        denial_audit.assert_awaited_once()
        kwargs = denial_audit.await_args.kwargs
        assert kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT
        assert kwargs["resource_id"] == _PIPELINE_ID
        payload = kwargs["payload"]
        assert payload["denied"] is True
        expected_previous = float(previous) if previous is not None else None
        expected_new = float(new) if new is not None else None
        assert payload["previous_threshold_usd"] == expected_previous
        assert payload["new_threshold_usd"] == expected_new

    def test_operator_lowering_allowed(self, client: TestClient) -> None:
        resp, update, denial_audit = self._patch_threshold(
            client, role="operator", previous=Decimal("100.000000"), sent=50
        )

        assert resp.status_code == 200, resp.text
        assert update.await_args.args[2]["circuit_breaker_threshold"] == 50
        denial_audit.assert_not_awaited()

    def test_operator_raising_denied_with_audit(self, client: TestClient) -> None:
        resp, update, denial_audit = self._patch_threshold(
            client, role="operator", previous=Decimal("50.000000"), sent=100
        )

        self._assert_denied(resp, denial_audit, update, previous=Decimal("50.000000"), new=100)

    def test_operator_clearing_denied_with_audit(self, client: TestClient) -> None:
        resp, update, denial_audit = self._patch_threshold(
            client, role="operator", previous=Decimal("50.000000"), sent=None
        )

        self._assert_denied(resp, denial_audit, update, previous=Decimal("50.000000"), new=None)

    def test_operator_setting_where_none_allowed(self, client: TestClient) -> None:
        resp, update, denial_audit = self._patch_threshold(client, role="operator", previous=None, sent=50)

        assert resp.status_code == 200, resp.text
        assert update.await_args.args[2]["circuit_breaker_threshold"] == 50
        denial_audit.assert_not_awaited()

    def test_admin_raising_allowed(self, client: TestClient) -> None:
        resp, update, denial_audit = self._patch_threshold(
            client, role="admin", previous=Decimal("50.000000"), sent=100
        )

        assert resp.status_code == 200, resp.text
        assert update.await_args.args[2]["circuit_breaker_threshold"] == 100
        denial_audit.assert_not_awaited()

    def test_admin_clearing_allowed(self, client: TestClient) -> None:
        resp, update, denial_audit = self._patch_threshold(
            client, role="admin", previous=Decimal("50.000000"), sent=None
        )

        assert resp.status_code == 200, resp.text
        assert update.await_args.args[2]["circuit_breaker_threshold"] is None
        denial_audit.assert_not_awaited()

    def test_operator_create_with_threshold_allowed(self, client: TestClient) -> None:
        # Rule 1: create is "set where none exists" — operator keeps working.
        self._use_role("operator")
        create = AsyncMock(return_value=_response_pipeline(Decimal("40.000000")))
        _enter(_route_patches(create_pipeline=create))

        resp = client.post("/api/v1/pipelines", json={"name": "p", "circuit_breaker_threshold": 40})

        assert resp.status_code == 201, resp.text
        assert create.await_args.kwargs["circuit_breaker_threshold"] == 40

    def test_create_guard_is_wired(self, client: TestClient) -> None:
        # The create surface RUNS the shared check: force-refuse it and the
        # route must 403 + audit. Removing the guard call site makes this
        # return 201 -> test fails (prove-the-fix for create).
        self._use_role("operator")
        denial_audit = AsyncMock()
        create = AsyncMock(return_value=_response_pipeline(Decimal("40.000000")))
        _enter(
            _route_patches(
                create_pipeline=create,
                circuit_breaker_threshold_change_allowed=MagicMock(return_value=False),
                append_audit_event_isolated=denial_audit,
            )
        )

        resp = client.post("/api/v1/pipelines", json={"name": "p", "circuit_breaker_threshold": 40})

        assert resp.status_code == 403, resp.text
        assert "cost.manage" in resp.json()["detail"]
        create.assert_not_awaited()
        denial_audit.assert_awaited_once()
        assert denial_audit.await_args.kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


def _session_cm(session: AsyncMock) -> AsyncMock:
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestMcpTools:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        _ctx_org_id.set(_ORG_ID)
        _ctx_role.set("operator")
        _ctx_user_id.set(_USER_ID)
        _ctx_auth_token.set("mk_testprefix_testsecretkey1234567890abc")
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role, _ctx_user_id

        for var in (_ctx_org_id, _ctx_role, _ctx_user_id, _ctx_auth_token, _ctx_auth_type):
            var.set(None)

    async def test_create_pipeline_forwards_threshold(self) -> None:
        from modulo.api.mcp_server import create_pipeline as mcp_create_pipeline

        created = _response_pipeline(Decimal("30.000000"))
        db_create = AsyncMock(return_value=created)
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch(f"{_CRUD}create_pipeline", new=db_create),
        ):
            result = await mcp_create_pipeline(name="p", circuit_breaker_threshold=30)

        assert db_create.await_args.kwargs["circuit_breaker_threshold"] == 30
        assert result["circuit_breaker_threshold"] == 30.0

    async def test_create_pipeline_rejects_invalid_threshold(self) -> None:
        from modulo.api.mcp_server import create_pipeline as mcp_create_pipeline

        session_factory = MagicMock()
        with patch("modulo.api.mcp_server._session", new=session_factory):
            result = await mcp_create_pipeline(name="p", circuit_breaker_threshold=0)

        assert result["error"] == "validation_failed"
        assert result["field"] == "circuit_breaker_threshold"
        session_factory.assert_not_called()

    async def test_create_pipeline_forwards_max_autonomy_level(self) -> None:
        """FAR-1163: a valid ceiling is validated and forwarded to the CRUD."""
        from modulo.api.mcp_server import create_pipeline as mcp_create_pipeline

        created = _response_pipeline(Decimal("30.000000"))
        db_create = AsyncMock(return_value=created)
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch(f"{_CRUD}create_pipeline", new=db_create),
        ):
            result = await mcp_create_pipeline(
                name="p",
                default_autonomy_level="manual_approval",
                max_autonomy_level="fully_autonomous",
            )

        assert db_create.await_args.kwargs["max_autonomy_level"] == "fully_autonomous"
        assert result["id"] == str(_PIPELINE_ID)

    async def test_create_pipeline_rejects_invalid_max_autonomy_level(self) -> None:
        """FAR-1163: a ceiling below the default is rejected before any DB work."""
        from modulo.api.mcp_server import create_pipeline as mcp_create_pipeline

        session_factory = MagicMock()
        with patch("modulo.api.mcp_server._session", new=session_factory):
            result = await mcp_create_pipeline(
                name="p",
                default_autonomy_level="fully_autonomous",
                max_autonomy_level="manual_approval",
            )

        assert result["error"] == "invalid_max_autonomy_level"
        assert "must be >=" in result["detail"]
        session_factory.assert_not_called()

    async def test_set_circuit_breaker_updates_with_audit_context(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        pipeline = _response_pipeline(Decimal("12.000000"))
        db_update = AsyncMock(return_value=pipeline)
        # FAR-1184: the tool reads the stored threshold for the permission
        # check — stub the previous value (lowering 100 -> 12, operator-OK).
        db_get = AsyncMock(return_value=_response_pipeline(Decimal("100.000000")))
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(f"{_CRUD}get_pipeline", new=db_get),
            patch(f"{_CRUD}update_pipeline", new=db_update),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=12)

        assert result == {
            "pipeline_id": str(_PIPELINE_ID),
            "circuit_breaker_threshold": 12.0,
            "circuit_breaker_tripped": False,
        }
        assert db_update.await_args.args[2] == {"circuit_breaker_threshold": 12}
        assert db_update.await_args.kwargs["account_id"] == _USER_ID
        assert db_update.await_args.kwargs["org_id"] == _ORG_ID

    async def test_set_circuit_breaker_null_disables(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        db_update = AsyncMock(return_value=_response_pipeline(None))
        # FAR-1184: clearing requires cost.manage (admin) — model an active
        # threshold being cleared by an admin.
        from modulo.api.mcp_server import _ctx_role

        _ctx_role.set("admin")
        db_get = AsyncMock(return_value=_response_pipeline(Decimal("50.000000")))
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(f"{_CRUD}get_pipeline", new=db_get),
            patch(f"{_CRUD}update_pipeline", new=db_update),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=None)

        assert result["circuit_breaker_threshold"] is None
        assert db_update.await_args.args[2] == {"circuit_breaker_threshold": None}

    async def test_set_circuit_breaker_not_found(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(f"{_CRUD}get_pipeline", new=AsyncMock(return_value=None)),
            patch(f"{_CRUD}update_pipeline", new=AsyncMock(return_value=None)),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=5)

        assert result == {"error": "pipeline_not_found", "pipeline_id": str(_PIPELINE_ID)}

    async def test_set_circuit_breaker_rejects_invalid_threshold(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        session_factory = MagicMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", new=session_factory),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=-3)

        assert result["error"] == "validation_failed"
        session_factory.assert_not_called()

    async def test_set_circuit_breaker_denied_below_operator(self) -> None:
        from modulo.api.mcp_server import _ctx_role, set_pipeline_circuit_breaker

        _ctx_role.set("viewer")
        with patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=5)

        assert result["error"] == "insufficient_scope"

    async def test_set_circuit_breaker_team_scope_mismatch(self) -> None:
        from modulo.api.mcp_server import _ctx_team_id, set_pipeline_circuit_breaker

        db_update = AsyncMock()
        token = _ctx_team_id.set(uuid.uuid4())
        try:
            with (
                patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
                patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
                patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=uuid.uuid4())),
                patch(f"{_CRUD}update_pipeline", new=db_update),
            ):
                result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=5)
        finally:
            _ctx_team_id.reset(token)

        assert "error" in result
        db_update.assert_not_awaited()

    async def test_set_circuit_breaker_auth_failure(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        with patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=False)):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=5)

        assert result["error"] == "auth_expired"

    async def test_set_circuit_breaker_invalid_pipeline_id(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        with patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)):
            result = await set_pipeline_circuit_breaker(pipeline_id="not-a-uuid", circuit_breaker_threshold=5)

        assert result["error"] == "invalid_id"
        assert result["field"] == "pipeline_id"

    async def test_set_circuit_breaker_unparseable_id_without_error_dict(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._parse_uuid_param", return_value=(None, None)),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id="whatever", circuit_breaker_threshold=5)

        assert result == {"error": "invalid_id", "detail": "UUID parse failed"}

    async def test_set_circuit_breaker_migration_required(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch(
                "modulo.api.mcp_server._pipeline_owner_team_id",
                new=AsyncMock(side_effect=ProgrammingError("SELECT 1", {}, Exception("missing column"))),
            ),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=5)

        assert result["error"] == "migration_required"

    async def test_set_circuit_breaker_unexpected_error(self) -> None:
        from modulo.api.mcp_server import set_pipeline_circuit_breaker

        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch(
                "modulo.api.mcp_server._pipeline_owner_team_id",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
        ):
            result = await set_pipeline_circuit_breaker(pipeline_id=str(_PIPELINE_ID), circuit_breaker_threshold=5)

        assert result["error"] == "internal_error"

    # ------------------------------------------------------------------
    # FAR-1184: raise/clear requires cost.manage (org admin)
    # ------------------------------------------------------------------

    async def _drive_set(
        self,
        *,
        threshold: float | None,
        previous: Decimal | None,
        role: str | None = None,
    ) -> tuple[dict[str, Any], AsyncMock, AsyncMock]:
        """Run set_pipeline_circuit_breaker with a stubbed previous threshold."""
        from modulo.api.mcp_server import _ctx_role, set_pipeline_circuit_breaker

        if role is not None:
            _ctx_role.set(role)
        db_update = AsyncMock(return_value=_response_pipeline(threshold))
        db_get = AsyncMock(return_value=_response_pipeline(previous))
        denial_audit = AsyncMock()
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.api.mcp_server._pipeline_owner_team_id", new=AsyncMock(return_value=None)),
            patch(f"{_CRUD}get_pipeline", new=db_get),
            patch(f"{_CRUD}update_pipeline", new=db_update),
            patch("modulo.core.audit_logger.append_audit_event", new=denial_audit),
        ):
            result = await set_pipeline_circuit_breaker(
                pipeline_id=str(_PIPELINE_ID),
                circuit_breaker_threshold=threshold,
            )
        return result, db_update, denial_audit

    async def test_set_operator_raising_denied_with_audit(self) -> None:
        result, db_update, denial_audit = await self._drive_set(
            threshold=100, previous=Decimal("50.000000"), role="operator"
        )

        assert result["error"] == "permission_denied"
        assert result["field"] == "circuit_breaker_threshold"
        assert "cost.manage" in result["detail"]
        db_update.assert_not_awaited()
        denial_audit.assert_awaited_once()
        assert denial_audit.await_args.kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT

    async def test_set_operator_clearing_denied_with_audit(self) -> None:
        result, db_update, denial_audit = await self._drive_set(
            threshold=None, previous=Decimal("50.000000"), role="operator"
        )

        assert result["error"] == "permission_denied"
        db_update.assert_not_awaited()
        denial_audit.assert_awaited_once()
        assert denial_audit.await_args.kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT
        payload = denial_audit.await_args.kwargs["payload_json"]
        assert payload["previous_threshold_usd"] == 50.0
        assert payload["new_threshold_usd"] is None

    async def test_set_operator_lowering_allowed(self) -> None:
        result, db_update, denial_audit = await self._drive_set(
            threshold=50, previous=Decimal("100.000000"), role="operator"
        )

        assert result["circuit_breaker_threshold"] == 50
        assert db_update.await_args.args[2] == {"circuit_breaker_threshold": 50}
        denial_audit.assert_not_awaited()

    async def test_set_admin_raising_allowed(self) -> None:
        result, db_update, denial_audit = await self._drive_set(
            threshold=100, previous=Decimal("50.000000"), role="admin"
        )

        assert result["circuit_breaker_threshold"] == 100
        assert db_update.await_args.args[2] == {"circuit_breaker_threshold": 100}
        denial_audit.assert_not_awaited()

    async def test_set_admin_clearing_allowed(self) -> None:
        result, db_update, denial_audit = await self._drive_set(
            threshold=None, previous=Decimal("50.000000"), role="admin"
        )

        assert result["circuit_breaker_threshold"] is None
        assert db_update.await_args.args[2] == {"circuit_breaker_threshold": None}
        denial_audit.assert_not_awaited()

    async def test_create_guard_is_wired(self) -> None:
        """The MCP create surface RUNS the shared check (prove-the-fix).

        Force-refuse the predicate: create must return permission_denied +
        the denial audit and never reach the CRUD. Without the guard call
        site the tool would create the pipeline -> test fails.
        """
        from modulo.api.mcp_server import create_pipeline as mcp_create_pipeline

        denial_audit = AsyncMock()
        db_create = AsyncMock(return_value=_response_pipeline(Decimal("30.000000")))
        with (
            patch("modulo.api.mcp_server.validate_current_auth", new=AsyncMock(return_value=True)),
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.core.audit_logger.append_audit_event", new=denial_audit),
            patch(
                f"{_CRUD}circuit_breaker_threshold_change_allowed",
                new=MagicMock(return_value=False),
            ),
            patch(f"{_CRUD}create_pipeline", new=db_create),
        ):
            result = await mcp_create_pipeline(name="p", circuit_breaker_threshold=30)

        assert result["error"] == "permission_denied"
        assert result["field"] == "circuit_breaker_threshold"
        assert "cost.manage" in result["detail"]
        db_create.assert_not_awaited()
        denial_audit.assert_awaited_once()
        assert denial_audit.await_args.kwargs["event_type"] == CIRCUIT_BREAKER_THRESHOLD_CHANGE_DENIED_EVENT


class TestMcpThresholdDenialAuditBestEffort:
    """FAR-1184: the MCP refusal audit never masks the permission-denied result.

    Exercised directly (the denial surfaces above always supply a tenant
    context and a successful audit) so the failure arms are covered: an unset
    user context degrades to an anonymous actor, a generic audit failure is
    logged rather than raised, and task cancellation still propagates.
    """

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_user_id

        _ctx_user_id.set(None)

    @staticmethod
    def _denial() -> CircuitBreakerThresholdChangeDenied:
        return CircuitBreakerThresholdChangeDenied(previous=Decimal("50.000000"), new=Decimal("100.000000"))

    async def test_missing_user_context_audits_anonymously(self) -> None:
        from modulo.api.mcp_server import _append_mcp_threshold_denial_audit, _ctx_user_id

        _ctx_user_id.set(None)
        audit = AsyncMock()
        with (
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch("modulo.core.audit_logger.append_audit_event", new=audit),
        ):
            await _append_mcp_threshold_denial_audit(_ORG_ID, None, self._denial())

        audit.assert_awaited_once()
        assert audit.await_args.kwargs["actor_user_id"] is None
        assert audit.await_args.kwargs["payload_json"]["changed_by"] is None

    async def test_audit_failure_is_logged_not_raised(self) -> None:
        from modulo.api.mcp_server import _append_mcp_threshold_denial_audit

        with (
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new=AsyncMock(side_effect=RuntimeError("audit backend down")),
            ),
            patch("modulo.api.mcp_server._log") as log,
        ):
            await _append_mcp_threshold_denial_audit(_ORG_ID, _PIPELINE_ID, self._denial())

        log.exception.assert_called_once()

    async def test_cancellation_propagates(self) -> None:
        from modulo.api.mcp_server import _append_mcp_threshold_denial_audit

        with (
            patch("modulo.api.mcp_server._session", return_value=_session_cm(AsyncMock())),
            patch(
                "modulo.core.audit_logger.append_audit_event",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await _append_mcp_threshold_denial_audit(_ORG_ID, _PIPELINE_ID, self._denial())


# ---------------------------------------------------------------------------
# Enforcement regression: a threshold set via the new surface trips the breaker
# ---------------------------------------------------------------------------


class TestEnforcementRegression:
    async def test_run_exceeding_configured_threshold_trips_breaker(self) -> None:
        pipeline = MagicMock()
        pipeline.name = "p"
        pipeline.circuit_breaker_tripped = False
        # Exactly what the create/update writers persist for a user-entered 25.5.
        pipeline.circuit_breaker_threshold = normalize_circuit_breaker_threshold(25.5)
        pipeline_result = MagicMock()
        pipeline_result.scalar_one_or_none.return_value = pipeline
        monthly_result = MagicMock()
        monthly_result.scalar_one.return_value = Decimal("20.00")
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[pipeline_result, monthly_result, MagicMock()])
        with (
            patch("modulo.core.cost_controller.set_rls_execution_context", new=AsyncMock()),
            patch("modulo.core.cost_controller._dispatch_circuit_breaker_tripped", new=AsyncMock()),
        ):
            approved, reason = await check_pipeline_circuit_breaker(
                session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=Decimal("6.00")
            )

        assert approved is False
        assert reason == "circuit_breaker_tripped"
        assert pipeline.circuit_breaker_tripped is True

    async def test_run_within_configured_threshold_is_approved(self) -> None:
        pipeline = MagicMock()
        pipeline.circuit_breaker_tripped = False
        pipeline.circuit_breaker_threshold = normalize_circuit_breaker_threshold(25.5)
        pipeline_result = MagicMock()
        pipeline_result.scalar_one_or_none.return_value = pipeline
        monthly_result = MagicMock()
        monthly_result.scalar_one.return_value = Decimal("20.00")
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[pipeline_result, monthly_result])
        with patch("modulo.core.cost_controller.set_rls_execution_context", new=AsyncMock()):
            approved, reason = await check_pipeline_circuit_breaker(
                session, org_id=_ORG_ID, pipeline_id=_PIPELINE_ID, cost_usd=Decimal("5.00")
            )

        assert approved is True
        assert reason is None
