"""FAR-1558 slice 1: PATCH /pipelines/{id} environment-profile binding.

Covers the three contracts the per-pipeline binding adds to the update route:

* the binding PERSISTS (it reaches ``update_pipeline``'s updates dict) and
  round-trips on the response,
* VALIDATION — the profile must exist in this organisation and be visible to
  the pipeline's effective (post-update) owner team; every violation answers
  422 (never a 404, which would confirm a foreign id exists),
* ``null`` CLEARS the binding, and a scope change re-validates a STORED
  binding so a team move can never strand an ineligible profile (the same
  fail-closed re-validation the accountability-owner path applies).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql import Select

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context
from modulo.api.main import app
from modulo.auth.dependencies import get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal
from modulo.settings import Settings, get_settings
from tests.unit.api.mock_session import configure_mock_session

_VALID_32 = "a" * 32
_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_PROFILE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


class _StandInPipeline:
    """Minimal ORM stand-in carrying every field ``PipelineResponse`` reads.

    ``environment_profile_id`` is set explicitly: the FAR-1558 column is new,
    and a stand-in that omitted it would exercise only the response default.
    """

    def __init__(
        self,
        *,
        environment_profile_id: uuid.UUID | None = None,
        owner_team_id: uuid.UUID | None = None,
        visibility: str = "org",
    ) -> None:
        self.id = _PIPELINE_ID
        self.organisation_id = _ORG_ID
        self.name = "Bound pipeline"
        self.description = None
        self.visibility = visibility
        self.owner_team_id = owner_team_id
        self.max_concurrent_runs = 5
        self.lock_wait_timeout_seconds = 300
        self.node_timeout_seconds = 300
        self.run_context_defaults: dict[str, Any] = {}
        self.default_autonomy_level = "manual_approval"
        self.max_duration_seconds = 3600
        self.stale_run_timeout_minutes = 30
        self.rate_limit_config = None
        self.retry_policy: dict[str, Any] = {}
        self.snapshot_count = 0
        self.archived_at = None
        self.folder_id = None
        self.account_id = _USER_ID
        self.created_at = _NOW
        self.updated_at = _NOW
        self.environment_profile_id = environment_profile_id


def _profile(
    profile_id: uuid.UUID,
    *,
    visibility: str = "org",
    owner_team_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=profile_id,
        organisation_id=_ORG_ID,
        name="e2b profile",
        visibility=visibility,
        owner_team_id=owner_team_id,
        deleted_at=None,
    )


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


class _RouteHarness:
    """TestClient + strict session whose ``environment_profiles`` lookup and
    in-txn ``FOR UPDATE`` pipeline row are served by the test.

    ``profile_lookups`` records the profile id every lookup SELECT asked for,
    so a test can assert a lookup NEVER happened (``null`` clear path).
    """

    def __init__(
        self,
        *,
        locked_row: SimpleNamespace,
        profile_row: SimpleNamespace | None,
    ) -> None:
        self.profile_lookups: list[Any] = []
        self.captured_updates: dict[str, Any] = {}
        self.pipeline = _StandInPipeline(
            environment_profile_id=getattr(locked_row, "environment_profile_id", None),
            owner_team_id=getattr(locked_row, "owner_team_id", None),
            visibility=getattr(locked_row, "visibility", "org"),
        )

        session = configure_mock_session(AsyncMock())
        base_execute = session.execute.side_effect

        def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
            sql = str(stmt) if isinstance(stmt, Select) else ""
            if "environment_profiles" in sql:
                self.profile_lookups.append(sql)
                result = MagicMock()
                result.scalar_one_or_none.return_value = profile_row
                return result
            if "FROM pipelines" in sql:
                result = MagicMock()
                result.scalar_one_or_none.return_value = locked_row
                return result
            return base_execute(stmt, *args, **kwargs)

        session.execute.side_effect = _execute

        begin_cm = AsyncMock()
        begin_cm.__aenter__ = AsyncMock(return_value=None)
        begin_cm.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=begin_cm)

        async def _override_session() -> AsyncGenerator[AsyncMock, None]:
            yield session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = _override_session
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

        async def _fake_update(
            _session: Any, _pipeline_id: uuid.UUID, updates: dict[str, Any], **_kwargs: Any
        ) -> _StandInPipeline:
            self.captured_updates.update(updates)
            if "environment_profile_id" in updates:
                self.pipeline.environment_profile_id = updates["environment_profile_id"]
            return self.pipeline

        self._patches = (
            patch("modulo.api.routes.pipelines.get_pipeline", new=AsyncMock(return_value=self.pipeline)),
            patch("modulo.api.routes.pipelines.update_pipeline", new=AsyncMock(side_effect=_fake_update)),
        )
        self.client = TestClient(app)

    def __enter__(self) -> TestClient:
        for p in self._patches:
            p.start()
        return self.client

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()
        app.dependency_overrides.clear()

    def patch(self, body: dict[str, Any]) -> Any:
        with self as client:
            return client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json=body)


@pytest.fixture
def make_harness() -> Callable[..., _RouteHarness]:
    def _make(
        *,
        locked_row: SimpleNamespace,
        profile_row: SimpleNamespace | None = None,
    ) -> _RouteHarness:
        return _RouteHarness(locked_row=locked_row, profile_row=profile_row)

    yield _make
    app.dependency_overrides.clear()


def _org_row(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = {
        "id": _PIPELINE_ID,
        "organisation_id": _ORG_ID,
        "visibility": "org",
        "owner_team_id": None,
        "environment_profile_id": None,
        "deleted_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_binding_persists_and_round_trips_on_the_response(make_harness: Callable[..., _RouteHarness]) -> None:
    """The settable field reaches the CRUD layer and comes back on the response."""
    harness = make_harness(locked_row=_org_row(), profile_row=_profile(_PROFILE_ID))

    resp = harness.patch({"environment_profile_id": str(_PROFILE_ID)})

    assert resp.status_code == 200, resp.text
    assert harness.captured_updates["environment_profile_id"] == _PROFILE_ID
    assert resp.json()["environment_profile_id"] == str(_PROFILE_ID)
    assert len(harness.profile_lookups) == 1


def test_null_clears_the_binding_without_a_profile_lookup(make_harness: Callable[..., _RouteHarness]) -> None:
    """``null`` clears: the value reaches the CRUD layer as None and no
    existence check runs (clearing can never fail on a missing profile)."""
    harness = make_harness(
        locked_row=_org_row(environment_profile_id=_PROFILE_ID),
        profile_row=None,
    )
    harness.pipeline.environment_profile_id = _PROFILE_ID

    resp = harness.patch({"environment_profile_id": None})

    assert resp.status_code == 200, resp.text
    assert "environment_profile_id" in harness.captured_updates
    assert harness.captured_updates["environment_profile_id"] is None
    assert harness.pipeline.environment_profile_id is None
    assert resp.json()["environment_profile_id"] is None
    assert not harness.profile_lookups


def test_profile_outside_the_organisation_is_rejected(make_harness: Callable[..., _RouteHarness]) -> None:
    """Cross-org / missing / deleted all resolve to the SAME 422, so a foreign
    id is never confirmed to exist — and nothing is written."""
    harness = make_harness(locked_row=_org_row(), profile_row=None)

    resp = harness.patch({"environment_profile_id": str(_PROFILE_ID)})

    assert resp.status_code == 422, resp.text
    assert "environment_profile_id" in resp.json()["detail"]
    assert not harness.captured_updates


def test_org_visible_profile_is_accepted(make_harness: Callable[..., _RouteHarness]) -> None:
    """An org-visible profile binds to any pipeline in the organisation."""
    harness = make_harness(locked_row=_org_row(), profile_row=_profile(_PROFILE_ID, visibility="org"))

    resp = harness.patch({"environment_profile_id": str(_PROFILE_ID)})

    assert resp.status_code == 200, resp.text
    assert harness.captured_updates["environment_profile_id"] == _PROFILE_ID


def test_team_profile_matching_the_pipeline_owner_team_is_accepted(
    make_harness: Callable[..., _RouteHarness],
) -> None:
    """A team-private profile binds when the pipeline's effective owner team
    is the profile's owner team."""
    locked = _org_row(visibility="team", owner_team_id=_TEAM_A)
    harness = make_harness(
        locked_row=locked,
        profile_row=_profile(_PROFILE_ID, visibility="team", owner_team_id=_TEAM_A),
    )

    resp = harness.patch({"environment_profile_id": str(_PROFILE_ID)})

    assert resp.status_code == 200, resp.text
    assert harness.captured_updates["environment_profile_id"] == _PROFILE_ID


def test_team_profile_owned_by_another_team_is_rejected(make_harness: Callable[..., _RouteHarness]) -> None:
    """Team-visibility rule: a team-private profile may only bind to a
    pipeline owned by the SAME team."""
    locked = _org_row(visibility="team", owner_team_id=_TEAM_A)
    harness = make_harness(
        locked_row=locked,
        profile_row=_profile(_PROFILE_ID, visibility="team", owner_team_id=_TEAM_B),
    )

    resp = harness.patch({"environment_profile_id": str(_PROFILE_ID)})

    assert resp.status_code == 422, resp.text
    assert "environment_profile_team_mismatch" in resp.json()["detail"]
    assert not harness.captured_updates


def test_team_private_profile_cannot_bind_to_an_org_wide_pipeline(
    make_harness: Callable[..., _RouteHarness],
) -> None:
    """An org-wide pipeline (no owner team) has no team to share with, so a
    team-private profile is refused — it is never silently treated as org-wide."""
    harness = make_harness(
        locked_row=_org_row(visibility="org", owner_team_id=None),
        profile_row=_profile(_PROFILE_ID, visibility="team", owner_team_id=_TEAM_A),
    )

    resp = harness.patch({"environment_profile_id": str(_PROFILE_ID)})

    assert resp.status_code == 422, resp.text
    assert "environment_profile_team_mismatch" in resp.json()["detail"]
    assert not harness.captured_updates


def test_scope_change_revalidates_a_stored_binding(make_harness: Callable[..., _RouteHarness]) -> None:
    """A team move that would strand a team-private binding fails CLOSED with
    the named mismatch — the same fail-closed re-validation the
    accountability-owner path applies on a scope change."""
    locked = _org_row(
        visibility="team",
        owner_team_id=_TEAM_A,
        environment_profile_id=_PROFILE_ID,
    )
    harness = make_harness(
        locked_row=locked,
        profile_row=_profile(_PROFILE_ID, visibility="team", owner_team_id=_TEAM_A),
    )

    resp = harness.patch({"owner_team_id": str(_TEAM_B)})

    assert resp.status_code == 422, resp.text
    assert "environment_profile_team_mismatch" in resp.json()["detail"]
    assert not harness.captured_updates


def test_scope_change_to_an_eligible_team_keeps_the_binding(
    make_harness: Callable[..., _RouteHarness],
) -> None:
    """The mirror of the rejection above: re-validating a stored binding must
    not block a move the binding still allows."""
    locked = _org_row(
        visibility="team",
        owner_team_id=_TEAM_A,
        environment_profile_id=_PROFILE_ID,
    )
    harness = make_harness(
        locked_row=locked,
        profile_row=_profile(_PROFILE_ID, visibility="team", owner_team_id=_TEAM_A),
    )

    resp = harness.patch({"owner_team_id": str(_TEAM_A)})

    assert resp.status_code == 200, resp.text
    assert harness.captured_updates["owner_team_id"] == _TEAM_A


def test_unchanged_binding_is_never_revalidated(make_harness: Callable[..., _RouteHarness]) -> None:
    """An unrelated PATCH must not consult the profile at all: with no binding
    key and no scope change there is nothing to validate."""
    harness = make_harness(locked_row=_org_row(environment_profile_id=_PROFILE_ID), profile_row=None)

    resp = harness.patch({"description": "just a description"})

    assert resp.status_code == 200, resp.text
    assert "environment_profile_id" not in harness.captured_updates
    assert not harness.profile_lookups
