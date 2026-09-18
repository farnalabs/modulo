"""Regression test for the PATCH /pipelines/{id} 422 silent-success bug.

Changing a pipeline's autonomy level or team ownership returned 422
"Data validation failed" while the change WAS committed. Root cause:
``PipelineResponse.model_validate(pipeline)`` reads ``pipeline.updated_at``
AFTER the ``async with session.begin():`` block exits. The ``updated_at``
column uses ``onupdate=func.current_timestamp()`` (DB-side), so SQLAlchemy
expires the attribute after flush; accessing it post-commit with
``autobegin=False`` triggers a refresh that fails with ``InvalidRequestError``,
which ``handle_db_errors`` maps to 422.

The fix refreshes the ORM row inside the transaction (``await
session.refresh(pipeline)``) so ``updated_at`` is loaded while the transaction
is active. This test proves the PATCH returns 200 (not 422) when ``updated_at``
is DB-computed and expired until refreshed.
"""

import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import InvalidRequestError

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.db.crud.pipeline import PipelineHasActiveRunsError
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


class _ExpiredPipeline:
    """Simulates a flushed ORM row whose ``updated_at`` is expired.

    ``updated_at`` is DB-computed (``onupdate=func.current_timestamp()``), so
    SQLAlchemy expires the attribute after flush. Accessing it before the
    in-transaction refresh raises ``InvalidRequestError`` (the pre-fix 422);
    after ``session.refresh`` marks the row refreshed it returns a datetime.
    """

    def __init__(self) -> None:
        self.id = _PIPELINE_ID
        self.organisation_id = _ORG_ID
        self.name = "Expired"
        self.description = None
        self.visibility = "org"
        self.max_concurrent_runs = 5
        self.lock_wait_timeout_seconds = 300
        self.node_timeout_seconds = 300
        self.run_context_defaults = {}
        self.default_autonomy_level = "manual_approval"
        self.max_duration_seconds = 3600
        self.stale_run_timeout_minutes = 30
        self.rate_limit_config = None
        self.retry_policy = {}
        self.snapshot_count = 0
        self.archived_at = None
        self.owner_team_id = None
        self.folder_id = None
        self.account_id = _USER_ID
        self.created_at = _NOW
        self._refreshed = False
        self.updated_at = None  # uses the setter; raises until refreshed

    @property
    def updated_at(self) -> datetime:
        if not self._refreshed:
            raise InvalidRequestError("Autobegin is disabled on this Session")
        return self._updated_at

    @updated_at.setter
    def updated_at(self, value: datetime | None) -> None:
        self._updated_at = value


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_current_pipeline() -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Expired"
    p.description = "before"
    p.visibility = "org"
    p.owner_team_id = None
    p.default_autonomy_level = "manual_approval"
    p.updated_at = _NOW
    return p


def _make_mock_session(pipeline: _ExpiredPipeline) -> AsyncMock:
    session = configure_mock_session(AsyncMock())

    async def _refresh(target: object) -> None:
        pipeline._refreshed = True
        pipeline._updated_at = _NOW

    session.refresh = AsyncMock(side_effect=_refresh)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def make_client() -> Generator[Callable[[], tuple[TestClient, _ExpiredPipeline]], None, None]:
    """Factory that builds a TestClient bound to a fresh expired pipeline."""

    def _make() -> tuple[TestClient, _ExpiredPipeline]:
        pipeline = _ExpiredPipeline()
        mock_session = _make_mock_session(pipeline)

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
        return TestClient(app), pipeline

    yield _make
    app.dependency_overrides.clear()


def test_patch_pipeline_returns_200_when_updated_at_is_db_computed(
    make_client: Callable[[], tuple[TestClient, _ExpiredPipeline]],
) -> None:
    client, pipeline = make_client()

    async def _apply_update(
        session: object, pipeline_id: uuid.UUID, updates: dict[str, object], **kwargs: object
    ) -> _ExpiredPipeline:
        pipeline.description = updates.get("description", pipeline.description)
        return pipeline

    with (
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=_make_current_pipeline())),
        patch("modulo.api.routes.pipelines.update_pipeline", new=AsyncMock(side_effect=_apply_update)),
    ):
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"description": "after"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "after"
    assert body["updated_at"] is not None
    assert datetime.fromisoformat(body["updated_at"]) == _NOW


def test_patch_pipeline_still_rejects_missing_pipeline(
    make_client: Callable[[], tuple[TestClient, _ExpiredPipeline]],
) -> None:
    client, _ = make_client()
    with patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=None)):
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"description": "after"})

    assert resp.status_code == 404


def test_patch_pipeline_still_409_on_active_runs(
    make_client: Callable[[], tuple[TestClient, _ExpiredPipeline]],
) -> None:
    client, _ = make_client()
    with (
        patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=_make_current_pipeline())),
        patch(
            "modulo.api.routes.pipelines.update_pipeline",
            new=AsyncMock(side_effect=PipelineHasActiveRunsError(3)),
        ),
    ):
        resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"owner_team_id": str(uuid.uuid4())})

    assert resp.status_code == 409
