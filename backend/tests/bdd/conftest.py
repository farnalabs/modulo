"""BDD/E2E test fixtures — pytest-bdd, Playwright, and TestClient setup."""

import uuid
from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import Page

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
ALT_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


# ---------------------------------------------------------------------------
# Playwright fixtures (E2E with ?theme=agent)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {
        **browser_context_args,
        "viewport": {"width": 1440, "height": 900},
        "color_scheme": "dark",
    }


@pytest.fixture
def agent_page(page: Page) -> Page:
    page.add_init_script(
        "document.documentElement.setAttribute('data-theme', 'agent')"
    )
    return page


@pytest.fixture(scope="session")
def base_url() -> str:
    return "http://localhost:5173"


# ---------------------------------------------------------------------------
# Mock helpers (reused across step definitions)
# ---------------------------------------------------------------------------


def make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def make_mock_pipeline(**kwargs: Any) -> MagicMock:
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.organisation_id = kwargs.get("org_id", ORG_ID)
    p.name = kwargs.get("name", "Test Pipeline")
    p.description = kwargs.get("description", None)
    p.visibility = kwargs.get("visibility", "org")
    p.max_concurrent_runs = kwargs.get("max_concurrent_runs", 5)
    p.lock_wait_timeout_seconds = kwargs.get("lock_wait_timeout_seconds", 300)
    p.node_timeout_seconds = kwargs.get("node_timeout_seconds", 300)
    p.run_context_defaults = kwargs.get("run_context_defaults", {})
    p.created_by = uuid.uuid4()
    p.created_at = None
    p.updated_at = None
    return p


def make_mock_run(**kwargs: Any) -> MagicMock:
    r = MagicMock()
    r.id = kwargs.get("id", uuid.uuid4())
    r.pipeline_id = kwargs.get("pipeline_id", uuid.uuid4())
    r.status = kwargs.get("status", "pending")
    r.langgraph_thread_id = str(uuid.uuid4())
    r.error_detail = kwargs.get("error_detail", None)
    r.input_hash = kwargs.get("input_hash", "0" * 64)
    r.trigger_type = kwargs.get("trigger_type", "manual")
    r.final_state = kwargs.get("final_state", None)
    return r


def make_mock_snapshot(**kwargs: Any) -> MagicMock:
    s = MagicMock()
    s.id = kwargs.get("id", uuid.uuid4())
    s.graph_json = kwargs.get("graph_json", {
        "nodes": [{"id": "node-a", "role": None}],
        "edges": [],
    })
    s.run_context_defaults = kwargs.get("run_context_defaults", {})
    s.connector_bindings_json = kwargs.get("connector_bindings", [])
    s.schema_pins_json = kwargs.get("schema_pins", [])
    s.prompt_pins_json = kwargs.get("prompt_pins", [])
    s.model_backend_pins_json = kwargs.get("backend_pins", [])
    return s


# ---------------------------------------------------------------------------
# TestClient fixture (API-level BDD steps)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> AsyncMock:
    return make_mock_session()


@pytest.fixture
def client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="testuser",
        organisation_id=ORG_ID,
        user_id=USER_ID,
        org_role="admin",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def unauth_client() -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_settings] = make_settings
    yield TestClient(app)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Common step definitions (shared across all step files)
# ---------------------------------------------------------------------------

from pytest_bdd import given, parsers  # noqa: E402


@given(parsers.parse('I am authenticated as an admin in org "{org}"'))
def _bdd_auth_admin_in_org(org: str) -> None:
    """No-op — the ``client`` fixture already provides an admin principal."""


@given(parsers.parse('I am authenticated in org "{org}"'))
def _bdd_auth_in_org(org: str) -> None:
    """No-op — auth fixture handles this."""


@given(parsers.parse('I am authenticated as a viewer in org "{org}"'))
def _bdd_auth_viewer_in_org(org: str) -> None:
    """No-op — viewer_client fixture handles this."""


@given(parsers.parse('the response status is {status:d}'))
def _bdd_check_response_status(status: int, request) -> None:
    """Check response status code."""
    resp = request.node._resp
    assert resp.status_code == status, (
        f"Expected status {status}, got {resp.status_code}"
    )

@pytest.fixture
def alt_org_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="otheruser",
        organisation_id=ALT_ORG_ID,
        user_id=uuid.uuid4(),
        org_role="viewer",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client(mock_session: AsyncMock) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    app.dependency_overrides[get_settings] = make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username="viewer",
        organisation_id=ORG_ID,
        user_id=uuid.uuid4(),
        org_role="viewer",
    )

    yield TestClient(app)

    app.dependency_overrides.clear()
