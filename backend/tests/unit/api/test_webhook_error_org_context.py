"""FAR-1484 — webhook ingress must attribute its ERRORs to the trigger's org.

The webhook route resolves its organisation ONLY at the trigger bootstrap
(``load_trigger_and_org_global``): the unauthenticated HMAC delivery path has
no principal at all. Before FAR-1484 nothing bound ``org_id_var`` there, so
every ERROR logged after the bootstrap — including the ``db_transient`` 503s
of the 2026-09-04 incident — was dropped by ``ErrorTrackingLogHandler`` with
only a ``no_org_context`` WARNING.

These tests drive the REAL route through a real ASGI app (real
``CorrelationIdMiddleware`` + ``CatchAllMiddleware``, real bootstrap helper,
real ``handle_db_errors``/``log_service_unavailable``, real
``ErrorTrackingLogHandler``) and deliberately do NOT set ``org_id_var``
themselves:

1. a DB failure AFTER the bootstrap is forwarded with the trigger's org, and
   the scope carrier (``request.state.organisation_id``) is published for the
   outer middleware;
2. a failure BEFORE the bootstrap (malformed body) has no organisation to
   resolve — it keeps the announced drop, pinning that the bind sits at the
   resolution point and is never fabricated earlier.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from modulo.api.dependencies import _get_engine, get_db_session, get_system_db_session
from modulo.api.middleware.catch_all import CatchAllMiddleware
from modulo.api.middleware.correlation_id import CorrelationIdMiddleware
from modulo.api.routes.webhooks import router
from modulo.core.logging_config import ErrorTrackingLogHandler, org_id_var
from tests.unit.api.conftest import make_system_session_mock

_TRIGGER_ID = uuid.UUID("00000000-0000-0000-0000-000000000071")
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000072")
_WEBHOOK_LOGGER = "modulo.api.db_error_reporting"


class _RecordingSink:
    """Stand-in for ``ErrorTrackingLogHandler._async_emit``.

    Records each forwarded record AND the ``org_id_var`` value visible at
    forward time — the value the real ingest would have used to attribute the
    row. Plain instance (not a function), so ``self._async_emit(record)``
    resolves to ``sink(record)`` without binding the handler as ``self``.
    """

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


def _app_session() -> AsyncMock:
    """App-session double: real ``session.begin()`` shape, harmless executes."""
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    session.execute = AsyncMock(return_value=MagicMock())
    session.add = MagicMock()
    return session


def _build_app() -> tuple[FastAPI, dict[str, Any]]:
    """A real app: production middleware order + the REAL webhook router.

    Middleware registration mirrors ``api.main``: ``CorrelationIdMiddleware``
    first (inner), ``CatchAllMiddleware`` second, then the scope-recorder
    decorator last (outermost — ``add_middleware`` puts the last-added layer on
    the outside). The recorder captures ``request.state.organisation_id`` after
    the response, proving the ASGI-scope carrier the outer middleware reads.
    """
    seen: dict[str, Any] = {}
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(CatchAllMiddleware)
    app.include_router(router)

    app_session = _app_session()
    system_session = make_system_session_mock(trigger_org_id=_ORG_ID)

    async def _override_app_session() -> AsyncIterator[AsyncMock]:
        yield app_session

    async def _override_system_session() -> AsyncIterator[AsyncMock]:
        yield system_session

    app.dependency_overrides[get_db_session] = _override_app_session
    app.dependency_overrides[get_system_db_session] = _override_system_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()

    @app.middleware("http")
    async def _record_scope_carrier(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        seen["state_organisation_id"] = getattr(request.state, "organisation_id", None)
        return response

    app.state.seen = seen
    return app, seen


async def _drain() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


async def test_db_failure_after_the_bootstrap_is_attributed_to_the_trigger_org(
    capture: object,
    sink: _RecordingSink,
) -> None:
    """The incident path (2026-09-04): a post-bootstrap DB failure must persist.

    The snapshot read fails with a ``SQLAlchemyError``; the route's
    ``except SQLAlchemyError`` arm calls ``log_service_unavailable`` at ERROR.
    The real handler must forward it with the organisation the REAL bootstrap
    helper resolved — this test never touches ``org_id_var``.
    """
    app, seen = _build_app()

    with (
        patch("modulo.api.routes.webhooks.set_rls_org", new_callable=AsyncMock),
        patch("modulo.api.routes.webhooks.ensure_triggers_resumable", new_callable=AsyncMock),
        patch(
            "modulo.db.crud.pipeline_snapshot.create_snapshot_from_live_graph",
            new_callable=AsyncMock,
            side_effect=SQLAlchemyError("db down"),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(f"/api/v1/triggers/{_TRIGGER_ID}/webhook", json={"event": "test"})
        await _drain()

    assert resp.status_code == 503
    # The real structured 503 record was forwarded by the real handler...
    assert any("service_unavailable" in message for message in sink.messages)
    assert any("reason=db_transient" in message for message in sink.messages)
    assert any(record.name == _WEBHOOK_LOGGER for record in sink.records)
    # ...with the organisation the trigger bootstrap resolved (not hand-set).
    assert sink.orgs == [str(_ORG_ID)]
    # Both carriers: the scope carrier the outer middleware reads was published.
    assert seen["state_organisation_id"] == str(_ORG_ID)
    # ...and the bind stayed in the request's task context — no caller leak.
    assert org_id_var.get() is None


async def test_failure_before_the_bootstrap_keeps_the_announced_drop(
    capture: object,
    sink: _RecordingSink,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pre-resolution ERROR has no organisation — the drop stays, never fabricated.

    A JSON-array body fails the object check BEFORE the trigger bootstrap, so
    there is no org to attribute: the handler must announce ``no_org_context``
    rather than forward a record (or worse, inherit a previous request's org).
    """
    app, seen = _build_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            f"/api/v1/triggers/{_TRIGGER_ID}/webhook",
            content=b"[1, 2, 3]",
            headers={"Content-Type": "application/json"},
        )
        await _drain()

    assert resp.status_code == 400
    assert not sink.messages
    assert any("no_org_context" in record.getMessage() for record in caplog.records)
    assert seen["state_organisation_id"] is None
    assert org_id_var.get() is None
