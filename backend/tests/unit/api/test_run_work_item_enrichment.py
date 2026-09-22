"""Unit tests for GET /api/v1/runs/{run_id}/work-items/enrichment (FAR-737).

Exercises the connector-mediated enrichment path end-to-end at the route
layer: the happy path, the non-negotiable failure fallback (empty items,
never a 5xx), the TTL cache (no repeat upstream work), the no-connector
path, 404, and the route error convention.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError

from modulo.api.dependencies import _get_engine, _get_session_factory, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.work_item_enrichment import clear_enrichment_cache
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_RUN_ID = uuid.uuid4()
_ENDPOINT = f"/api/v1/runs/{_RUN_ID}/work-items/enrichment"

_PROG = ProgrammingError("s", {}, Exception())
_SQL = SQLAlchemyError("boom")
_RUNTIME = RuntimeError("kaboom")
_INTEGRITY = IntegrityError("s", {}, Exception())

_PR_REFS = [{"kind": "github_pr", "ref": "acme/repo#42", "source": "derived"}]


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
        redis_url="redis://localhost:6379/0",
    )


def _make_run(*, work_item_refs, input_payload=None) -> MagicMock:
    run = MagicMock()
    run.id = _RUN_ID
    run.work_item_refs = work_item_refs
    run.input_payload = input_payload
    return run


def _pr_run() -> MagicMock:
    return _make_run(work_item_refs=_PR_REFS)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = MagicMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    exec_result = MagicMock()
    exec_result.scalar_one_or_none.return_value = None
    exec_result.scalar.return_value = 0
    exec_result.scalars.return_value.all.return_value = []
    session.execute = AsyncMock(return_value=exec_result)
    return session


class _MockFactory:
    def __init__(self, session: AsyncMock) -> None:
        self._session = session

    def __call__(self) -> _MockFactory:
        return self

    async def __aenter__(self) -> AsyncMock:
        return self._session

    async def __aexit__(self, *args: object) -> None:
        return None


def _install_overrides(session: AsyncMock, *, org_role: str = "admin") -> None:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield session

    def principal() -> TenantPrincipal:
        return TenantPrincipal(
            username=f"{org_role}@test",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role=org_role,
        )

    app.dependency_overrides[get_settings] = _make_settings
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    app.dependency_overrides[_get_session_factory] = lambda: _MockFactory(session)
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
        username=f"{org_role}@test",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role=org_role,
    )
    app.dependency_overrides[get_current_tenant_user] = principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = principal
    mock_plan = MagicMock()
    mock_plan.feature_enabled.return_value = True
    app.dependency_overrides[get_plan_context] = lambda: mock_plan


@pytest.fixture(autouse=True)
def _clean_state():
    clear_enrichment_cache()
    with (
        patch("modulo.api.routes.runs.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.work_item_enrichment.set_rls_org", new_callable=AsyncMock),
    ):
        yield
    clear_enrichment_cache()
    app.dependency_overrides.clear()


@pytest.fixture
def client() -> Generator[tuple[TestClient, AsyncMock], None, None]:
    session = _make_session()
    _install_overrides(session)
    yield TestClient(app), session
    app.dependency_overrides.clear()


@contextmanager
def _fresh_client(begin_exc: Exception | None = None) -> Generator[TestClient, None, None]:
    session = _make_session()
    if begin_exc is not None:
        begin_cm = MagicMock()

        async def _raise(*_args: object, **_kwargs: object) -> None:
            raise begin_exc

        begin_cm.__aenter__ = _raise
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)
    _install_overrides(session)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _github_instance() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), connector_type_id="github")


def _hub(sample: AsyncMock | None = None) -> MagicMock:
    hub = MagicMock()
    hub.__aenter__ = AsyncMock(return_value=hub)
    hub.__aexit__ = AsyncMock(return_value=False)
    hub.initialise = AsyncMock()
    hub.sample = sample if sample is not None else AsyncMock()
    return hub


@contextmanager
def _enrich_env(*, run: MagicMock | Exception, instances: list, hub: MagicMock):
    """Patch run lookup + the enrichment service's connector dependencies.

    Yields the ``list_connector_instances`` AsyncMock and the hub class mock.
    """
    lister = AsyncMock(return_value=SimpleNamespace(items=instances))
    hub_cls = MagicMock(return_value=hub)
    run_patcher = (
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=run)
        if isinstance(run, Exception)
        else patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, return_value=run)
    )
    with (
        run_patcher,
        patch("modulo.core.work_item_enrichment.list_connector_instances", new=lister),
        patch("modulo.core.work_item_enrichment.ConnectorHub", hub_cls),
        patch(
            "modulo.core.work_item_enrichment.create_secrets_backend",
            MagicMock(return_value=MagicMock()),
        ),
    ):
        yield lister, hub_cls


# ---------------------------------------------------------------------------
# Connector path
# ---------------------------------------------------------------------------


def test_enrichment_returns_live_github_data(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    sample = AsyncMock(
        return_value=[
            {
                "number": 42,
                "title": "Fix the widget",
                "state": "open",
                "merged": False,
                "html_url": "https://github.com/acme/repo/pull/42",
            }
        ],
    )
    with _enrich_env(run=_pr_run(), instances=[_github_instance()], hub=_hub(sample)) as (
        _lister,
        _hub_cls,
    ):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["ref"] == "acme/repo#42"
    assert item["repo"] == "acme/repo"
    assert item["number"] == 42
    assert item["title"] == "Fix the widget"
    assert item["state"] == "open"
    assert item["merged"] is False
    assert item["html_url"] == "https://github.com/acme/repo/pull/42"
    # The lookup goes through the connector's single-PR resource.
    assert sample.call_count == 1
    call_args = sample.call_args
    assert call_args.args[1] == "pull"
    assert call_args.args[2] == {"repo": "acme/repo", "pull_number": "42"}
    assert not call_args.kwargs


def test_enrichment_merged_pr_reports_merged_state(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    sample = AsyncMock(return_value=[{"number": 42, "title": "Done", "state": "closed", "merged": True}])
    with _enrich_env(run=_pr_run(), instances=[_github_instance()], hub=_hub(sample)):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["merged"] is True
    assert item["state"] == "closed"


# ---------------------------------------------------------------------------
# Failure fallback (non-negotiable): never a 5xx, empty items, plain badge
# ---------------------------------------------------------------------------


def test_enrichment_github_failure_returns_200_with_no_items(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """GitHub unreachable → 200 + empty items; the view keeps the plain badge."""
    http, _session = client
    sample = AsyncMock(side_effect=RuntimeError("github down"))
    with _enrich_env(run=_pr_run(), instances=[_github_instance()], hub=_hub(sample)):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 200, resp.text
    assert not resp.json()["items"]


def test_enrichment_no_connector_configured_returns_200_empty(
    client: tuple[TestClient, AsyncMock],
) -> None:
    http, _session = client
    with _enrich_env(run=_pr_run(), instances=[], hub=_hub()) as (_lister, hub_cls):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 200, resp.text
    assert not resp.json()["items"]
    hub_cls.assert_not_called()


def test_enrichment_hub_initialise_failure_returns_200_empty(
    client: tuple[TestClient, AsyncMock],
) -> None:
    """Credential decrypt / hub init failure degrades to the plain badge."""
    http, _session = client
    hub = _hub()
    hub.initialise = AsyncMock(side_effect=RuntimeError("decrypt failed"))
    with _enrich_env(run=_pr_run(), instances=[_github_instance()], hub=hub):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 200, resp.text
    assert not resp.json()["items"]


# ---------------------------------------------------------------------------
# Cache: a hot run view does not hammer GitHub per page view
# ---------------------------------------------------------------------------


def test_enrichment_second_view_is_served_from_cache(
    client: tuple[TestClient, AsyncMock],
) -> None:
    http, _session = client
    sample = AsyncMock(return_value=[{"number": 42, "title": "Fix", "state": "open", "merged": False}])
    with _enrich_env(run=_pr_run(), instances=[_github_instance()], hub=_hub(sample)) as (
        lister,
        hub_cls,
    ):
        first = http.get(_ENDPOINT)
        second = http.get(_ENDPOINT)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert sample.call_count == 1
    assert lister.call_count == 1
    assert hub_cls.call_count == 1


# ---------------------------------------------------------------------------
# No PR targets → no connector work at all
# ---------------------------------------------------------------------------


def test_enrichment_without_pr_targets_skips_connector(
    client: tuple[TestClient, AsyncMock],
) -> None:
    http, _session = client
    run = _make_run(work_item_refs=[{"kind": "linear", "ref": "FAR-1", "source": "derived"}])
    with _enrich_env(run=run, instances=[_github_instance()], hub=_hub()) as (lister, hub_cls):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 200, resp.text
    assert not resp.json()["items"]
    lister.assert_not_awaited()
    hub_cls.assert_not_called()


# ---------------------------------------------------------------------------
# 404 + route error convention
# ---------------------------------------------------------------------------


def test_enrichment_unknown_run_returns_404(client: tuple[TestClient, AsyncMock]) -> None:
    http, _session = client
    with _enrich_env(run=None, instances=[], hub=_hub()):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Run not found"


@pytest.mark.parametrize(("exc", "expected"), [(_PROG, 501), (_INTEGRITY, 409), (_SQL, 503), (_RUNTIME, 500)])
def test_enrichment_error_mapping(exc: Exception, expected: int) -> None:
    with (
        _fresh_client() as http,
        patch("modulo.api.routes.runs.get_run", new_callable=AsyncMock, side_effect=exc),
    ):
        resp = http.get(_ENDPOINT)

    assert resp.status_code == expected, resp.text
