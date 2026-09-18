"""Determination route error-handling tests.

Pins the ProgrammingError -> 501 / SQLAlchemyError -> 503 / IntegrityError ->
409 / Exception -> 500 mapping for /api/v1/determination and
/api/v1/determination/draft.

Regression for PR #922: both route bodies previously ordered `except
SQLAlchemyError` BEFORE `except ProgrammingError` in the outer try/except,
which made the ProgrammingError (501, "run database migrations") branch
unreachable dead code — migration-needed errors from the outer section
(ConnectorHub / run_scan / infer) silently returned 503 instead. The route
bodies now order specific exceptions before their bases, and this file locks
that behaviour in.

Note: `list_connector_instances` internally swallows ProgrammingError (returns
an empty PageResult), so the session-level cases below exercise the decorator
+ inner-handler mapping, and the run_scan-patch cases exercise the outer
handler where the dead code lived.
"""

import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.base import PageResult
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_mock_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    mock_session = _make_mock_session()

    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_plan_context] = lambda: mock_plan
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="admin",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="admin",
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _make_exc(error_type: type) -> Exception:
    if issubclass(error_type, IntegrityError):
        return IntegrityError("stmt", {}, Exception("mock constraint violation"))
    if issubclass(error_type, ProgrammingError):
        return ProgrammingError("stmt", {}, Exception("mock table does not exist"))
    if issubclass(error_type, SQLAlchemyError):
        return SQLAlchemyError("mock", "mock", "mock")
    return error_type("mock unexpected error")


# ── Session-level: DB errors raised on session.execute (decorator + inner) ─

SESSION_CASES: list[tuple[str, str, type, int, str | None]] = [
    # GET /api/v1/determination
    ("determination_prog", "GET", ProgrammingError, 501, "migrations"),
    ("determination_sqla", "GET", SQLAlchemyError, 503, None),
    ("determination_integrity", "GET", IntegrityError, 409, "already exists"),
    ("determination_exc", "GET", ValueError, 500, None),
    # POST /api/v1/determination/draft
    ("draft_prog", "POST", ProgrammingError, 501, "migrations"),
    ("draft_sqla", "POST", SQLAlchemyError, 503, None),
    ("draft_integrity", "POST", IntegrityError, 409, "already exists"),
    ("draft_exc", "POST", ValueError, 500, None),
]


class TestDeterminationSessionErrors:
    @pytest.mark.parametrize(
        ("test_id", "method", "error_type", "expected_status", "detail_check"),
        SESSION_CASES,
        ids=[c[0] for c in SESSION_CASES],
    )
    def test_session_error(
        self,
        client: TestClient,
        test_id: str,
        method: str,
        error_type: type[Exception],
        expected_status: int,
        detail_check: str | None,
    ) -> None:
        exc = _make_exc(error_type)
        session = configure_mock_session(AsyncMock())
        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
        # Raise on session.execute — surfaces on set_rls_org / list_connector_instances.
        session.execute = AsyncMock(side_effect=exc)
        bind_mock = MagicMock()
        bind_mock.dialect.name = "postgresql"
        session.get_bind = AsyncMock(return_value=bind_mock)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        client.app.dependency_overrides[get_db_session] = override_session

        url = "/api/v1/determination" if method == "GET" else "/api/v1/determination/draft"
        resp = client.get(url) if method == "GET" else client.post(url, json={})

        assert resp.status_code == expected_status
        if detail_check and resp.status_code != 500:
            detail = resp.json().get("detail", "")
            if isinstance(detail, str):
                assert detail_check in detail.lower()


# ── Outer-handler regression: errors from the ConnectorHub/run_scan section ─
# These are the cases that FAIL on pre-#922 code: a ProgrammingError from the
# outer section was caught by `except SQLAlchemyError` (its base) and returned
# 503 instead of 501.


def _make_outer_session() -> AsyncMock:
    session = configure_mock_session(AsyncMock(), allow_empty_execute=True)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    bind_mock = MagicMock()
    bind_mock.dialect.name = "postgresql"
    session.get_bind = AsyncMock(return_value=bind_mock)
    return session


def _override_session(client: TestClient, session: AsyncMock) -> None:
    async def _get_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    client.app.dependency_overrides[get_db_session] = _get_session

    class _MockFactory:
        def __init__(self, s: AsyncMock) -> None:
            self._session = s

        def __call__(self):
            return self

        async def __aenter__(self) -> AsyncMock:
            return self._session

        async def __aexit__(self, *args: object) -> None:
            pass

    client.app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)


class TestDeterminationOuterHandlerOrdering:
    """Regression for PR #922: outer handler must return 501 for ProgrammingError.

    The dead code was `except SQLAlchemyError` before `except ProgrammingError`
    in the route body's outer try/except. ProgrammingError subclasses
    SQLAlchemyError, so the 501 branch was unreachable and migration-needed
    errors from run_scan returned 503. This test would fail on the old order.
    """

    def test_get_programming_error_returns_501(self, client: TestClient) -> None:
        # Regression: pre-#922 this returned 503 (dead-code ordering bug).
        session = _make_outer_session()
        _override_session(client, session)
        exc = ProgrammingError("stmt", {}, Exception("mock table does not exist"))

        empty_page = PageResult(items=[], total=0, page=1, page_size=100)
        hub = AsyncMock()
        hub.initialise = AsyncMock(return_value=None)

        class _MockHubCM:
            async def __aenter__(self):
                return hub

            async def __aexit__(self, *args: object) -> None:
                pass

        with (
            patch("modulo.api.routes.determination.set_rls_org", AsyncMock()),
            patch("modulo.api.routes.determination.list_connector_instances", AsyncMock(return_value=empty_page)),
            patch("modulo.api.routes.determination.create_secrets_backend", MagicMock()),
            patch("modulo.api.routes.determination.ConnectorHub", MagicMock(return_value=_MockHubCM())),
            patch("modulo.api.routes.determination.run_scan", AsyncMock(side_effect=exc)),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 501
        assert "migrations" in resp.json().get("detail", "").lower()

    def test_draft_programming_error_returns_501(self, client: TestClient) -> None:
        session = _make_outer_session()
        _override_session(client, session)
        exc = ProgrammingError("stmt", {}, Exception("mock table does not exist"))

        empty_page = PageResult(items=[], total=0, page=1, page_size=100)
        hub = AsyncMock()
        hub.initialise = AsyncMock(return_value=None)

        class _MockHubCM:
            async def __aenter__(self):
                return hub

            async def __aexit__(self, *args: object) -> None:
                pass

        with (
            patch("modulo.api.routes.determination.set_rls_org", AsyncMock()),
            patch("modulo.api.routes.determination.list_connector_instances", AsyncMock(return_value=empty_page)),
            patch("modulo.api.routes.determination.create_secrets_backend", MagicMock()),
            patch("modulo.api.routes.determination.ConnectorHub", MagicMock(return_value=_MockHubCM())),
            patch("modulo.api.routes.determination.run_scan", AsyncMock(side_effect=exc)),
        ):
            resp = client.post("/api/v1/determination/draft", json={})

        assert resp.status_code == 501
        assert "migrations" in resp.json().get("detail", "").lower()

    def test_get_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        session = _make_outer_session()
        _override_session(client, session)
        exc = SQLAlchemyError("mock", "mock", "mock")

        empty_page = PageResult(items=[], total=0, page=1, page_size=100)
        hub = AsyncMock()
        hub.initialise = AsyncMock(return_value=None)

        class _MockHubCM:
            async def __aenter__(self):
                return hub

            async def __aexit__(self, *args: object) -> None:
                pass

        with (
            patch("modulo.api.routes.determination.set_rls_org", AsyncMock()),
            patch("modulo.api.routes.determination.list_connector_instances", AsyncMock(return_value=empty_page)),
            patch("modulo.api.routes.determination.create_secrets_backend", MagicMock()),
            patch("modulo.api.routes.determination.ConnectorHub", MagicMock(return_value=_MockHubCM())),
            patch("modulo.api.routes.determination.run_scan", AsyncMock(side_effect=exc)),
        ):
            resp = client.get("/api/v1/determination")

        assert resp.status_code == 503

    def test_draft_sqlalchemy_error_returns_503(self, client: TestClient) -> None:
        session = _make_outer_session()
        _override_session(client, session)
        exc = SQLAlchemyError("mock", "mock", "mock")

        empty_page = PageResult(items=[], total=0, page=1, page_size=100)
        hub = AsyncMock()
        hub.initialise = AsyncMock(return_value=None)

        class _MockHubCM:
            async def __aenter__(self):
                return hub

            async def __aexit__(self, *args: object) -> None:
                pass

        with (
            patch("modulo.api.routes.determination.set_rls_org", AsyncMock()),
            patch("modulo.api.routes.determination.list_connector_instances", AsyncMock(return_value=empty_page)),
            patch("modulo.api.routes.determination.create_secrets_backend", MagicMock()),
            patch("modulo.api.routes.determination.ConnectorHub", MagicMock(return_value=_MockHubCM())),
            patch("modulo.api.routes.determination.run_scan", AsyncMock(side_effect=exc)),
        ):
            resp = client.post("/api/v1/determination/draft", json={})

        assert resp.status_code == 503
