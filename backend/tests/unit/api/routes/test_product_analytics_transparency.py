"""Unit tests for the product-analytics transparency endpoint (GET /transparency).

Covers the config-aggregation and stale-dump warning logic that previously had
no test coverage at all. The endpoint reads five ``system_config`` keys and
derives a ``warning`` when the last successful dump is older than 3 days while
consent is ``all`` — boundary behaviour that static analysis cannot vouch for.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from modulo.api.dependencies import get_db_session
from modulo.api.routes import product_analytics_transparency as pat_module
from modulo.api.routes.product_analytics_transparency import TransparencyResponse
from modulo.api.routes.product_analytics_transparency import router as transparency_router
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal

app = FastAPI()
app.include_router(transparency_router)

_URL = "/api/v1/product-analytics/transparency"

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")

_SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="ops@test",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="admin",
    is_system_admin=True,
)

_NON_SYSTEM_ADMIN = AuthenticatedPrincipal(
    username="user@test",
    organisation_id=_ORG_ID,
    account_id=_USER_ID,
    org_role="admin",
    is_system_admin=False,
)


def _config(value: object) -> MagicMock:
    config = MagicMock()
    config.value = value
    return config


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _request(client: TestClient) -> dict[str, object]:
    resp = client.get(_URL)
    assert resp.status_code == 200
    return dict(resp.json())


def _client(principal: AuthenticatedPrincipal = _SYSTEM_ADMIN) -> TestClient:
    app.dependency_overrides[get_db_session] = _make_session
    app.dependency_overrides[get_current_user] = lambda: principal
    return TestClient(app)


def _restore_overrides() -> None:
    app.dependency_overrides.clear()


def _stale_timestamp_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


class TestPermissionGate:
    def test_non_system_admin_gets_403(self) -> None:
        client = _client(_NON_SYSTEM_ADMIN)
        resp = client.get(_URL)
        assert resp.status_code == 403
        _restore_overrides()

    def test_system_admin_gets_200(self) -> None:
        client = _client()
        with patch.object(pat_module, "get_config", new=AsyncMock(return_value=None)) as get_config:
            resp = client.get(_URL)
        assert resp.status_code == 200
        assert get_config.await_count == 5
        _restore_overrides()


# ---------------------------------------------------------------------------
# Defaults (no rows stored)
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_all_absent_returns_zeros_and_off(self) -> None:
        client = _client()
        with patch.object(pat_module, "get_config", new=AsyncMock(return_value=None)):
            body = _request(client)
        assert body == {
            "last_successful_dump_at": None,
            "dump_count_total": 0,
            "consent_level": "off",
            "instance_enabled": False,
            "enforcement_enabled": False,
            "warning": None,
        }
        _restore_overrides()

    def test_response_shape_matches_transparency_response(self) -> None:
        client = _client()
        with patch.object(pat_module, "get_config", new=AsyncMock(return_value=None)):
            body = _request(client)
        assert set(body.keys()) == set(TransparencyResponse().model_dump().keys())
        _restore_overrides()


# ---------------------------------------------------------------------------
# Config aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    @pytest.mark.parametrize(
        ("dump_count", "expected"),
        [
            (None, 0),
            ("0", 0),
            ("42", 42),
            (0, 0),
        ],
    )
    def test_dump_count_coerces_to_int(self, dump_count: object, expected: int) -> None:
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            if key == "product_analytics_dump_count":
                return _config(dump_count) if dump_count is not None else None
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["dump_count_total"] == expected
        _restore_overrides()

    def test_consent_and_enabled_flags_coerced_to_bool(self) -> None:
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            values = {
                "product_analytics_consent_level": "all",
                "product_analytics_enabled": 1,
                "product_analytics_enforcement_enabled": "1",
            }
            if key in values:
                return _config(values[key])
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["consent_level"] == "all"
        assert body["instance_enabled"] is True
        assert body["enforcement_enabled"] is True
        _restore_overrides()

    def test_non_bool_stored_values_are_bool_coerced(self) -> None:
        """Documents the storage/coercion semantics: any non-empty stored
        string coerces to True (even ``"false"``), while a scalar ``0`` is
        falsy."""
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            values = {"product_analytics_enabled": "false", "product_analytics_enforcement_enabled": 0}
            if key in values:
                return _config(values[key])
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["instance_enabled"] is True
        assert body["enforcement_enabled"] is False
        _restore_overrides()


# ---------------------------------------------------------------------------
# Stale-dump warning logic (the derived field)
# ---------------------------------------------------------------------------


class TestStaleWarning:
    @pytest.mark.parametrize(
        ("days_ago", "consent_level", "expected_warning"),
        [
            # Inside the 3-day threshold — never warns.
            (2, "all", None),
            # Fresh dump but consent not 'all' — warning suppressed by consent.
            (2, "off", None),
            # Just past the threshold with opt-in consent — warns.
            (4, "all", "not_reaching_farnalabs"),
            # Stale dump but consent opt-out — not actionable, no warning.
            (4, "off", None),
        ],
    )
    def test_warning_boundary(self, days_ago: float, consent_level: str, expected_warning: str | None) -> None:
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            values = {
                "product_analytics_last_dump_at": _stale_timestamp_ago(days_ago),
                "product_analytics_consent_level": consent_level,
            }
            if key in values:
                return _config(values[key])
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["warning"] == expected_warning
        _restore_overrides()

    def test_no_last_dump_never_warns(self) -> None:
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            if key == "product_analytics_consent_level":
                return _config("all")
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["last_successful_dump_at"] is None
        assert body["warning"] is None
        _restore_overrides()

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        """A naive ``last_dump_at`` (no tzinfo) is assumed UTC for age math."""
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            values = {
                "product_analytics_last_dump_at": (
                    datetime.now(UTC).replace(tzinfo=None) - timedelta(days=4)
                ).isoformat(),
                "product_analytics_consent_level": "all",
            }
            if key in values:
                return _config(values[key])
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["warning"] == "not_reaching_farnalabs"
        _restore_overrides()

    def test_malformed_timestamp_does_not_raise(self) -> None:
        client = _client()

        async def _get(session: object, key: str) -> MagicMock | None:
            values = {
                "product_analytics_last_dump_at": "not-a-timestamp",
                "product_analytics_consent_level": "all",
            }
            if key in values:
                return _config(values[key])
            return None

        with patch.object(pat_module, "get_config", side_effect=_get):
            body = _request(client)
        assert body["last_successful_dump_at"] == "not-a-timestamp"
        assert body["warning"] is None
        _restore_overrides()
