"""FAR-1484 — the health surface resolves no organisation; its drop must stay announced.

``/healthz`` and ``/healthz/ready`` are DEPLOYMENT-scoped, unauthenticated
probes (ADR 043): there is no principal and no tenant on them, so the ERROR
records they emit (the migration-divergence guard's
``health._check_migrations divergence check failed``, plus any
``handle_db_errors`` arm) have no organisation to attribute. Binding
``org_id_var`` here would mean inventing an org — the verdict for this surface
is therefore to KEEP the announced ``no_org_context`` drop (the
system/unknown-org persistence question is an owner design decision reported
under FAR-1484).

This test drives the REAL ``_check_migrations`` with a real failing
``check_migration_divergence`` — a real ERROR record — through the REAL
``ErrorTrackingLogHandler``, and asserts the drop is announced rather than
silent. It never sets ``org_id_var`` itself.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.api.routes import health
from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit`` (see the wiring test)."""

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []
        self.orgs: list[str | None] = []

    async def __call__(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.orgs.append(org_id_var.get())

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


@pytest.fixture(autouse=True)
def _clean_rate_limit_state() -> Any:
    """The handler forwards at most one record per org per 5s window."""
    ErrorTrackingLogHandler._last_write_time.clear()
    yield
    ErrorTrackingLogHandler._last_write_time.clear()


@pytest.fixture
def sink() -> _RecordingSink:
    return _RecordingSink()


@pytest.fixture
def capture(sink: _RecordingSink) -> Any:
    """Attach a REAL ``ErrorTrackingLogHandler`` (with a recording sink) to the root logger."""
    handler = ErrorTrackingLogHandler()
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        with patch.object(ErrorTrackingLogHandler, "_async_emit", new=sink):
            yield handler
    finally:
        root.removeHandler(handler)


def _make_settings() -> Any:
    from modulo.settings import Settings

    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_admin_password="test",
    )


def _engine_double() -> MagicMock:
    """Pooled-engine double whose single connectivity read returns one row."""
    result = MagicMock()
    result.fetchall.return_value = [("0001_health_probe",)]
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=result)
    connect_cm = MagicMock()
    connect_cm.__aenter__ = AsyncMock(return_value=conn)
    connect_cm.__aexit__ = AsyncMock(return_value=False)
    engine = MagicMock()
    engine.connect.return_value = connect_cm
    return engine


async def test_divergence_guard_failure_is_dropped_with_an_announced_no_org_context(
    capture: object,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real ERROR from the real health check: no org exists, so the drop is announced."""
    with (
        patch.object(health, "get_settings", return_value=_make_settings()),
        patch.object(health, "get_or_create_engine", return_value=_engine_double()),
        patch.object(health, "_load_repo_heads", return_value={"0001_health_probe"}),
        patch.object(health, "check_migration_divergence", side_effect=RuntimeError("guard boom")),
    ):
        result = await health._check_migrations()

    # The check itself ran its real ERROR path and degraded (never claimed clean).
    assert result.status == "degraded"
    assert result.detail is not None
    assert "divergence check could not run" in result.detail
    assert any("health._check_migrations divergence check failed" in record.getMessage() for record in caplog.records)
    # No organisation is resolvable on a deployment-scoped probe: nothing is
    # forwarded (the record stays stdout-only) and the drop leaves a trace.
    assert not sink.messages
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert org_id_var.get() is None
