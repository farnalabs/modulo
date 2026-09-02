"""Unit tests for the pipeline ``retry_policy`` API schema validation.

Covers create persistence, create/update validation rejection, and clearing.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.uuid4()
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline() -> MagicMock:
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
    p.created_by = uuid.uuid4()
    p.account_id = p.created_by
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.begin_nested = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# FAR-525 delegation parity — _validate_retry_policy raises GraphValidator
# messages BYTE-IDENTICAL to the retired inline implementation
# ---------------------------------------------------------------------------


_OBJ_MSG = "retry_policy must be an object like {'on': ['stall','timeout','failure','eval_failed'], 'max_retries': 0-5}"
_ON_LIST_MSG = "retry_policy 'on' must be a list of strings from ['stall','timeout','failure','eval_failed']"
_BOGUS_MSG = (
    "retry_policy 'on' contains unknown values ['bogus']; "
    "allowed values are ['stall','timeout','failure','eval_failed']"
)
_BUDGET_MSG = "retry_policy 'max_retries' must be an integer between 0 and 5"


@pytest.mark.parametrize(
    ("payload", "expected_message"),
    [
        ("stall", _OBJ_MSG),
        (["stall"], _OBJ_MSG),
        ({"on": "stall", "max_retries": 2}, _ON_LIST_MSG),
        ({"on": ["stall", 42], "max_retries": 2}, _ON_LIST_MSG),
        ({"on": ["bogus"], "max_retries": 2}, _BOGUS_MSG),
        ({"on": ["stall"], "max_retries": "lots"}, _BUDGET_MSG),
        ({"on": ["stall"], "max_retries": True}, _BUDGET_MSG),
        ({"on": ["stall"], "max_retries": 6}, _BUDGET_MSG),
    ],
    ids=[
        "payload_not_object_scalar",
        "payload_not_object_list",
        "on_not_a_list_string",
        "on_not_a_list_with_int",
        "on_contains_bogus_value",
        "max_retries_not_int_string",
        "max_retries_not_int_bool",
        "max_retries_out_of_range",
    ],
)
def test_validate_retry_policy_message_parity(payload: object, expected_message: str) -> None:
    """Each failure class raises the EXACT message the pre-refactor inline
    validator raised (first-issue parity — the delegation must not change a
    single 422 detail byte)."""
    from modulo.api.routes.pipelines import _validate_retry_policy

    with pytest.raises(ValueError) as exc_info:
        _validate_retry_policy(payload)
    assert str(exc_info.value) == expected_message


def test_validate_retry_policy_delegation_equivalence_multi_error_payload() -> None:
    """A payload with MULTIPLE faults surfaces the FIRST issue only (the inline
    validator raised on first fault — no error aggregation was introduced)."""
    from modulo.api.routes.pipelines import _validate_retry_policy

    # 'on' fault comes before the max_retries fault — 'on' message wins.
    with pytest.raises(ValueError, match=r"'on' contains unknown values"):
        _validate_retry_policy({"on": ["bogus"], "max_retries": 9})


def test_validate_retry_policy_delegates_valid_payloads_unchanged() -> None:
    from modulo.api.routes.pipelines import _validate_retry_policy

    valid = {"on": ["stall", "timeout", "failure", "eval_failed"], "max_retries": 5}
    assert _validate_retry_policy(valid) is valid
    assert _validate_retry_policy(None) is None


# ---------------------------------------------------------------------------
# POST /api/v1/pipelines — retry_policy create + validation
# ---------------------------------------------------------------------------


def test_create_pipeline_persists_retry_policy(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["stall"], "max_retries": 2}

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/pipelines",
            json={"name": "Pipeline", "retry_policy": {"on": ["stall"], "max_retries": 2}},
        )

    assert resp.status_code == 201
    assert resp.json()["retry_policy"] == {"on": ["stall"], "max_retries": 2}


def test_create_pipeline_rejects_unknown_retry_event(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={"name": "Pipeline", "retry_policy": {"on": ["bogus"], "max_retries": 2}},
    )

    assert resp.status_code == 422


def test_create_pipeline_rejects_max_retries_over_budget(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={"name": "Pipeline", "retry_policy": {"on": ["stall"], "max_retries": 9}},
    )

    assert resp.status_code == 422


def test_create_pipeline_rejects_non_integer_max_retries(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={"name": "Pipeline", "retry_policy": {"on": ["stall"], "max_retries": "lots"}},
    )

    assert resp.status_code == 422


def test_create_pipeline_accepts_empty_retry_policy(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {}

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Pipeline", "retry_policy": {}})

    assert resp.status_code == 201
    assert not resp.json()["retry_policy"]


def test_create_pipeline_accepts_all_valid_events(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["stall", "timeout", "failure", "eval_failed"], "max_retries": 5}

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post(
            "/api/v1/pipelines",
            json={
                "name": "Pipeline",
                "retry_policy": {"on": ["stall", "timeout", "failure", "eval_failed"], "max_retries": 5},
            },
        )

    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# PATCH /api/v1/pipelines/{id} — retry_policy update + clear
# ---------------------------------------------------------------------------


def test_update_pipeline_sets_retry_policy(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["timeout"], "max_retries": 1}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"retry_policy": {"on": ["timeout"], "max_retries": 1}},
        )

    assert resp.status_code == 200
    assert resp.json()["retry_policy"] == {"on": ["timeout"], "max_retries": 1}


def test_update_pipeline_clears_retry_policy_with_empty_dict(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = {}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"retry_policy": {}})

    assert resp.status_code == 200
    assert not resp.json()["retry_policy"]


def test_update_pipeline_accepts_eval_failed_retry_event(client: TestClient) -> None:
    """FAR-503: PATCH with the new "eval_failed" event is accepted."""
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["eval_failed"], "max_retries": 1}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"retry_policy": {"on": ["eval_failed"], "max_retries": 1}},
        )

    assert resp.status_code == 200
    assert resp.json()["retry_policy"] == {"on": ["eval_failed"], "max_retries": 1}


def test_update_pipeline_rejects_unknown_retry_event(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"retry_policy": {"on": ["nope"], "max_retries": 1}},
    )

    assert resp.status_code == 422


def test_update_pipeline_rejects_max_retries_over_budget(client: TestClient) -> None:
    resp = client.patch(
        f"/api/v1/pipelines/{_PIPELINE_ID}",
        json={"retry_policy": {"on": ["failure"], "max_retries": 6}},
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# FAR-525 — backoff_schedule (run-level re-dispatch pacing) at the write sites
# ---------------------------------------------------------------------------


_SCHEDULE_OK = {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 30, "multiplier": 1.5}}


def test_create_pipeline_accepts_backoff_schedule(client: TestClient) -> None:
    pipeline = _make_pipeline()
    pipeline.retry_policy = _SCHEDULE_OK

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Pipeline", "retry_policy": _SCHEDULE_OK})

    assert resp.status_code == 201
    assert resp.json()["retry_policy"] == _SCHEDULE_OK


def test_create_pipeline_rejects_out_of_bounds_delay_seconds(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={
            "name": "Pipeline",
            "retry_policy": {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": 301}},
        },
    )
    assert resp.status_code == 422
    assert "delay_seconds" in resp.json()["detail"]


def test_create_pipeline_rejects_huge_int_delay_seconds_with_422(client: TestClient) -> None:
    """FAR-525 qa gate: a JSON int literal with >308 digits (e.g. 10**400)
    parses to an arbitrary-precision Python int whose float() conversion
    raises OverflowError. The validator contains it, so the API answers a
    VALIDATION 422 (the standard malformed-schedule message) — never a 500."""
    for huge in (10**309, 10**400):
        resp = client.post(
            "/api/v1/pipelines",
            json={
                "name": "Pipeline",
                "retry_policy": {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"delay_seconds": huge}},
            },
        )
        assert resp.status_code == 422, huge
        assert "delay_seconds" in resp.json()["detail"], huge


def test_create_pipeline_rejects_schedule_missing_delay_seconds(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={
            "name": "Pipeline",
            "retry_policy": {"on": ["failure"], "max_retries": 2, "backoff_schedule": {"multiplier": 2.0}},
        },
    )
    assert resp.status_code == 422


def test_create_pipeline_rejects_unknown_schedule_inner_key(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={
            "name": "Pipeline",
            "retry_policy": {
                "on": ["failure"],
                "max_retries": 2,
                "backoff_schedule": {"delay_seconds": 45, "backof": 2},
            },
        },
    )
    assert resp.status_code == 422
    assert "backof" in resp.json()["detail"]


def test_create_pipeline_rejects_out_of_bounds_multiplier(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/pipelines",
        json={
            "name": "Pipeline",
            "retry_policy": {
                "on": ["failure"],
                "max_retries": 2,
                "backoff_schedule": {"delay_seconds": 45, "multiplier": 10.1},
            },
        },
    )
    assert resp.status_code == 422


def test_create_pipeline_accepts_legacy_backoff_alongside_new_validation(client: TestClient) -> None:
    """Write-side legacy coexistence: the legacy numeric `backoff` key passes
    the NEW validation untouched (it is node-default-inherited, not run-level)."""
    pipeline = _make_pipeline()
    legacy = {"on": ["failure"], "max_retries": 2, "backoff": 1.5}
    pipeline.retry_policy = legacy

    with (
        patch("modulo.api.routes.pipelines.create_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
    ):
        resp = client.post("/api/v1/pipelines", json={"name": "Pipeline", "retry_policy": legacy})

    assert resp.status_code == 201


def test_update_pipeline_canonicalizes_integral_float_delay(client: TestClient) -> None:
    """PATCH delay 300.0 -> stored 300 (type-stable storage so the topology
    hash cannot flip on a float/int spelling)."""
    pipeline = _make_pipeline()
    pipeline.retry_policy = {
        "on": ["failure"],
        "max_retries": 2,
        "backoff_schedule": {"delay_seconds": 300, "multiplier": 2.0},
    }

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={
                "retry_policy": {
                    "on": ["failure"],
                    "max_retries": 2,
                    "backoff_schedule": {"delay_seconds": 300.0, "multiplier": 2},
                }
            },
        )

    assert resp.status_code == 200
    assert resp.json()["retry_policy"] == {
        "on": ["failure"],
        "max_retries": 2,
        "backoff_schedule": {"delay_seconds": 300, "multiplier": 2.0},
    }


def test_update_pipeline_unknown_top_level_key_warns_but_writes(client: TestClient) -> None:
    """A typo'd top-level key is accepted NON-blocking with a warning log."""
    pipeline = _make_pipeline()
    pipeline.retry_policy = {"on": ["failure"], "max_retries": 2}

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.update_pipeline", return_value=pipeline),
        patch("modulo.api.routes.pipelines.set_rls_org"),
        patch("modulo.api.routes.pipelines.set_rls_user_context"),
        patch("modulo.api.routes.pipelines._assert_team_transition_allowed", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.append_audit_event", new=AsyncMock()),
        patch("modulo.api.routes.pipelines.logger.warning") as warn_mock,
    ):
        resp = client.patch(
            f"/api/v1/pipelines/{_PIPELINE_ID}",
            json={"retry_policy": {"on": ["failure"], "max_retries": 2, "backof_schedule": {"delay_seconds": 45}}},
        )

    assert resp.status_code == 200
    warn_calls = [c for c in warn_mock.call_args_list if c.args and c.args[0] == "pipeline.retry_policy_unknown_keys"]
    assert warn_calls, warn_mock.call_args_list
    assert warn_calls[0].kwargs["extra"]["keys"] == ["backof_schedule"]


def test_validate_retry_policy_graph_validator_parity_for_valid_legacy_payload() -> None:
    """GraphValidator emits NO error for a legacy payload the API accepts —
    the delegation cannot silently tighten the accepted surface."""
    from modulo.api.routes.pipelines import _validate_retry_policy
    from modulo.core.graph_validator import GraphValidator, ValidationResult

    legacy = {"on": ["failure"], "max_retries": 2, "backoff": 1.5}
    result = ValidationResult()
    GraphValidator.check_retry_policy(legacy, result)
    GraphValidator.check_retry_policy_schedule(legacy, result)
    assert result.is_valid
    assert _validate_retry_policy(legacy) == legacy
