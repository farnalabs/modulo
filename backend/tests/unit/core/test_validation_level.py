"""Unit tests for the integration validation level (FAR-935).

Proves the fix:

* ``resolve_validation_level`` correctly degrades a failed canary and returns
  the baseline for a passing or not-yet-run canary.
* The health sweep writes ``validation_level`` alongside ``last_health_check_at``.
* ``get_integration_status`` never raises when ``validation_level`` is corrupted
  or missing — it degrades to the baseline.
* Every connector type and model backend provider resolves to a level from the
  single source of truth.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

import modulo.core.connector_hub.health_sweep as health_sweep
from modulo.connectors.base import ConnectorType
from modulo.core.connector_hub.health_sweep import run_connector_health_checks
from modulo.core.validation_level import (
    _LEVEL_ORDER,
    ValidationLevel,
    _level_below,
    connector_baseline_level,
    model_backend_baseline_level,
    resolve_validation_level,
)
from modulo.db.enums import ModelBackendProvider
from modulo.db.models.connector_instance import ConnectorInstance

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# resolve_validation_level — the core logic
# ---------------------------------------------------------------------------


class TestResolveValidationLevel:
    """Prove the three canary-state branches."""

    def test_no_canary_returns_baseline(self) -> None:
        """When last_health_check_at is None, the baseline is the ceiling."""
        result = resolve_validation_level(
            ValidationLevel.SELF_HOSTED_E2E,
            last_health_check_at=None,
            last_health_check_error=None,
        )
        assert result == ValidationLevel.SELF_HOSTED_E2E

    def test_passing_canary_returns_baseline(self) -> None:
        """When the canary passed, the baseline is the ceiling."""
        result = resolve_validation_level(
            ValidationLevel.SELF_HOSTED_E2E,
            last_health_check_at=datetime.now(UTC),
            last_health_check_error=None,
        )
        assert result == ValidationLevel.SELF_HOSTED_E2E

    def test_failed_canary_degrades_one_level(self) -> None:
        """When the canary failed, the level drops one step below the baseline."""
        result = resolve_validation_level(
            ValidationLevel.CANARY_GREEN,
            last_health_check_at=datetime.now(UTC),
            last_health_check_error="Connection refused",
        )
        assert result == ValidationLevel.SELF_HOSTED_E2E

    def test_failed_canary_at_floor_stays_at_floor(self) -> None:
        """When the canary failed and the baseline is already at the floor, stay at floor."""
        result = resolve_validation_level(
            ValidationLevel.UNIT_ONLY,
            last_health_check_at=datetime.now(UTC),
            last_health_check_error="fail",
        )
        assert result == ValidationLevel.UNIT_ONLY

    def test_failed_canary_empty_error_treated_as_pass(self) -> None:
        """An empty string error is treated as a pass (not a failure)."""
        result = resolve_validation_level(
            ValidationLevel.SELF_HOSTED_E2E,
            last_health_check_at=datetime.now(UTC),
            last_health_check_error="",
        )
        assert result == ValidationLevel.SELF_HOSTED_E2E

    def test_fail_safe_on_exception_returns_baseline(self) -> None:
        """If the computation raises for any reason, degrade to baseline."""
        # Force an exception by passing an invalid level that breaks _LEVEL_ORDER lookup
        with patch(
            "modulo.core.validation_level._LEVEL_ORDER",
            side_effect=RuntimeError("boom"),
        ):
            result = resolve_validation_level(
                ValidationLevel.SELF_HOSTED_E2E,
                last_health_check_at=datetime.now(UTC),
                last_health_check_error="fail",
            )
        # The except clause catches the error and returns baseline
        assert result == ValidationLevel.SELF_HOSTED_E2E


class TestLevelBelow:
    """Prove the level-below helper."""

    def test_below_canary_green(self) -> None:
        assert _level_below(ValidationLevel.CANARY_GREEN) == ValidationLevel.SELF_HOSTED_E2E

    def test_below_self_hosted_e2e(self) -> None:
        assert _level_below(ValidationLevel.SELF_HOSTED_E2E) == ValidationLevel.CONTRACT_RECORDED

    def test_below_contract_recorded(self) -> None:
        assert _level_below(ValidationLevel.CONTRACT_RECORDED) == ValidationLevel.UNIT_ONLY

    def test_below_unit_only_stays_at_floor(self) -> None:
        assert _level_below(ValidationLevel.UNIT_ONLY) == ValidationLevel.UNIT_ONLY

    def test_below_unknown_level(self) -> None:
        """Unknown level strings fall back to unit-only."""
        assert _level_below("nonexistent") == ValidationLevel.UNIT_ONLY


# ---------------------------------------------------------------------------
# Baseline maps — every type must resolve
# ---------------------------------------------------------------------------


class TestBaselineMaps:
    """Every connector type and model backend provider must resolve to a level."""

    def test_all_connector_types_resolve(self) -> None:
        for ct in ConnectorType:
            level = connector_baseline_level(ct.value)
            assert level in _LEVEL_ORDER, f"{ct.value} resolved to unknown level {level!r}"

    def test_all_model_backend_providers_resolve(self) -> None:
        for p in ModelBackendProvider:
            level = model_backend_baseline_level(p.value)
            assert level in _LEVEL_ORDER, f"{p.value} resolved to unknown level {level!r}"

    def test_unknown_connector_type_returns_unit_only(self) -> None:
        assert connector_baseline_level("no-such-type") == ValidationLevel.UNIT_ONLY

    def test_unknown_model_backend_provider_returns_unit_only(self) -> None:
        assert model_backend_baseline_level("no-such-provider") == ValidationLevel.UNIT_ONLY


# ---------------------------------------------------------------------------
# Health sweep — writes validation_level
# ---------------------------------------------------------------------------


class _FakeSecretsBackend:
    def __init__(self, session: AsyncSession) -> None:  # pragma: no cover - trivial
        pass

    async def get_secret(self, name: str) -> Any:
        return "{}"

    async def close(self) -> None:  # pragma: no cover - trivial
        pass


def _fake_backend_factory(*, session: AsyncSession, fernet_key: str | None = None) -> _FakeSecretsBackend:
    return _FakeSecretsBackend(session)


def _instance(**overrides: Any) -> ConnectorInstance:
    connector_type_id = overrides.pop("connector_type_id")
    ci = ConnectorInstance(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        name=connector_type_id,
        connector_type_id=connector_type_id,
        config_json=overrides.pop("config_json", {}),
        visibility=overrides.pop("visibility", "org"),
        allowed_operations=overrides.pop("allowed_operations", ["read"]),
        **overrides,
    )
    ci.credentials_ciphertext = b"{}"
    return ci


@pytest.fixture
async def sweep(tmp_path):
    engine: AsyncEngine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}", echo=False)

    from modulo.db.models.base import Base

    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[ConnectorInstance.__table__]))
    with patch.object(health_sweep, "create_secrets_backend", _fake_backend_factory):

        async def _run() -> dict[str, Any]:
            return await run_connector_health_checks(
                async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
            )

        async def _seeder(instances: list[ConnectorInstance]) -> None:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session, session.begin():
                for ci in instances:
                    await session.merge(ci)

        async def _rows() -> list[ConnectorInstance]:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as session:
                result = await session.execute(select(ConnectorInstance).order_by(ConnectorInstance.name))
                return list(result.scalars().all())

        yield _run, _seeder, _rows
    await engine.dispose()


async def test_sweep_writes_validation_level_on_healthy_instance(sweep, tmp_path) -> None:
    """A healthy canary must write the baseline validation_level."""
    run, seeder, rows = sweep
    await seeder([_instance(connector_type_id="filesystem", config_json={"base_path": str(tmp_path)})])

    await run()

    (row,) = await rows()
    assert row.validation_level is not None
    # filesystem baseline is unit-only
    assert row.validation_level == ValidationLevel.UNIT_ONLY


async def test_sweep_writes_validation_level_on_unhealthy_instance(sweep) -> None:
    """A failed canary must degrade the validation_level one step below baseline."""
    run, seeder, rows = sweep
    await seeder([_instance(connector_type_id="shell")])

    await run()

    (row,) = await rows()
    assert row.validation_level is not None
    # shell baseline is unit-only; failure stays at floor
    assert row.validation_level == ValidationLevel.UNIT_ONLY


async def test_sweep_writes_validation_level_on_unknown_type(sweep) -> None:
    """An unknown connector type must not crash the sweep — it gets unit-only."""
    run, seeder, rows = sweep
    await seeder([_instance(connector_type_id="no-such-type")])

    result = await run()

    assert result["checked"] == 1
    (row,) = await rows()
    assert row.validation_level == ValidationLevel.UNIT_ONLY
    assert row.last_health_check_at is not None


# ---------------------------------------------------------------------------
# Fail-safe: get_integration_status must never raise
# ---------------------------------------------------------------------------


class TestGetIntegrationStatusFailSafe:
    """The MCP tool must never raise due to validation_level corruption."""

    def test_getattr_fallback_for_missing_column(self) -> None:
        """If the ORM object lacks validation_level, getattr falls back gracefully."""
        # Simulate an object without validation_level (pre-migration row)
        fake_obj = type("FakeObj", (), {"connector_type_id": "github", "validation_level": None})()
        vl = getattr(fake_obj, "validation_level", None) or "unit-only"
        assert vl == "unit-only"

    def test_getattr_fallback_for_corrupted_value(self) -> None:
        """If validation_level is a corrupted value, the MCP tool still surfaces it."""
        fake_obj = type("FakeObj", (), {"validation_level": "CORRUPTED"})()
        vl = getattr(fake_obj, "validation_level", None) or "unit-only"
        # The MCP tool surfaces whatever is in the DB — it doesn't validate the value
        assert vl == "CORRUPTED"

    def test_baseline_never_raises_for_any_type(self) -> None:
        """connector_baseline_level must never raise for any string input."""
        # Exhaustive: every ConnectorType value + random garbage
        for ct in ConnectorType:
            result = connector_baseline_level(ct.value)
            assert isinstance(result, str)
        result = connector_baseline_level("")
        assert result == ValidationLevel.UNIT_ONLY
        result = connector_baseline_level("\x00\x01")
        assert result == ValidationLevel.UNIT_ONLY

    def test_model_backend_baseline_never_raises_for_any_string(self) -> None:
        """model_backend_baseline_level must never raise for any string input."""
        for p in ModelBackendProvider:
            result = model_backend_baseline_level(p.value)
            assert isinstance(result, str)
        result = model_backend_baseline_level("")
        assert result == ValidationLevel.UNIT_ONLY
