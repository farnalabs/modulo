"""Regression tests for the cross-team pipeline visibility leak (GET).

A user who is a member of Team B must NOT be able to read Team A's
team-private pipeline (``visibility='team'``, ``owner_team_id=Team A``) via
GET /pipelines/{id}. The DB root cause (an OR'd org-only RLS policy that made
the team policy dead weight) is fixed by migration ``0124_fix_team_rls_policies``;
post-migration the resolver SELECT runs under RLS, so a non-member's row is
invisible at the DB layer and GET returns 404. The app-layer defense-in-depth
is the ``require_team_membership_or_admin`` gate on the GET route; these tests
exercise that layer (the non-member case simulates RLS filtering via a
row-invisible resolver).

A member of Team A CAN read the pipeline; an org admin bypasses the gate; and
org-visible pipelines (``visibility='org'``, ``owner_team_id=None``) are NOT
team-gated even for a non-member.

A second section (FAR-1515) covers the WRITE direction: the REST graph-save
team gate must refuse a team pipeline that pins an org-only connector, while
leaving the org-pipeline rule alone.

A third section (FAR-1515 expansion) covers the four write paths that used to
re-create the forbidden state WITHOUT running that gate: an ownership-transfer
PATCH (stored graph gated against the EFFECTIVE NEW owner team), a confirmed
workflow import (gated after node rewiring, inside the import transaction),
and a connector visibility/owner re-scope (every bound pipeline judged with
the same predicate). Each test drives the real endpoint with only the
innermost lookup stubbed, so it fails (200 / helper never called) without the
fix.
"""

import json
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from datetime import UTC, datetime
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
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-000000000003")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _make_settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_admin_password="testpass",
    )


def _make_pipeline(*, owner_team_id: uuid.UUID | None, visibility: str) -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.organisation_id = _ORG_ID
    p.name = "Team Pipeline"
    p.description = None
    p.visibility = visibility
    p.max_concurrent_runs = 5
    p.lock_wait_timeout_seconds = 300
    p.node_timeout_seconds = 300
    p.run_context_defaults = {}
    p.default_autonomy_level = "manual_approval"
    p.max_duration_seconds = None
    p.stale_run_timeout_minutes = 30
    p.rate_limit_config = None
    p.retry_policy = {}
    p.snapshot_count = 0
    p.archived_at = None
    p.owner_team_id = owner_team_id
    p.folder_id = None
    p.account_id = _USER_ID
    p.created_at = _NOW
    p.updated_at = _NOW
    return p


class _ResolverRow:
    """Mutable holder for the team-scope resolver's row.

    The ``require_team_membership_or_admin`` dependency resolves the target
    row's ``owner_team_id``/``visibility`` with a ``SELECT ... FROM pipelines``.
    This holder lets each test set what that resolver sees without rebuilding
    the whole mock session.

    ``row_visible=False`` simulates post-migration RLS filtering: the resolver's
    SELECT returns no row (the DB layer hides it from a non-member), so the
    dependency raises 404 before the membership check ever fires.
    """

    def __init__(
        self,
        *,
        owner_team_id: uuid.UUID | None,
        visibility: str,
        row_visible: bool = True,
    ) -> None:
        self.owner_team_id = owner_team_id
        self.visibility = visibility
        self.row_visible = row_visible


def _make_mock_session(resolver: _ResolverRow) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    base_effect = session.execute.side_effect

    async def _execute(stmt: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(stmt, Select) and "FROM pipelines" in str(stmt):
            row = MagicMock()
            if resolver.row_visible:
                row.first.return_value = (resolver.owner_team_id, resolver.visibility)
            else:
                row.first.return_value = None
            return row
        return base_effect(stmt, *args, **kwargs)

    session.execute = AsyncMock(side_effect=_execute)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def make_client() -> Generator[Callable[..., tuple[TestClient, _ResolverRow]], None, None]:
    """Factory that builds a TestClient with the given principal + resolver row."""

    def _make(
        *,
        org_role: str = "operator",
        owner_team_id: uuid.UUID | None = None,
        visibility: str = "org",
        row_visible: bool = True,
    ) -> tuple[TestClient, _ResolverRow]:
        resolver = _ResolverRow(owner_team_id=owner_team_id, visibility=visibility, row_visible=row_visible)
        mock_session = _make_mock_session(resolver)

        async def override_session() -> AsyncGenerator[AsyncMock, None]:
            yield mock_session

        app.dependency_overrides[get_settings] = _make_settings
        app.dependency_overrides[get_db_session] = override_session
        app.dependency_overrides[_get_engine] = lambda: MagicMock()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedPrincipal(
            username="testuser",
            organisation_id=_ORG_ID,
            account_id=_USER_ID,
            org_role=org_role,
        )
        mock_plan = MagicMock()
        mock_plan.feature_enabled.return_value = True
        app.dependency_overrides[get_plan_context] = lambda: mock_plan
        return TestClient(app), resolver

    yield _make
    app.dependency_overrides.clear()


def _get(client: TestClient, *, membership: bool) -> int:
    with (
        patch("modulo.api.dependencies.team_membership_exists", new=AsyncMock(return_value=membership)),
        patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new=AsyncMock(return_value=_make_pipeline(owner_team_id=_TEAM_A, visibility="team")),
        ),
    ):
        resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")
    return resp.status_code


class TestCrossTeamPipelineVisibility:
    def test_non_member_cannot_get_team_private_pipeline(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        """Post-migration, a non-member gets 404, not 403.

        After migration ``0124_fix_team_rls_policies`` drops the org-only
        ``rls_org_isolation`` policy on team-scoped tables, the
        ``require_team_membership_or_admin`` dependency's resolver SELECT
        (``SELECT owner_team_id, visibility FROM pipelines WHERE id = ...``)
        runs UNDER RLS in its own transaction. A non-member's SELECT is
        filtered to no row, so the dependency raises 404 before the
        membership check ever fires — the row is invisible at the DB layer.

        ``row_visible=False`` simulates that RLS filtering (the resolver sees
        no row). The app-layer ``require_team_membership_or_admin`` gate is
        defense-in-depth that would return 403 only on non-Postgres backends
        or before the migration applies.
        """
        client, _ = make_client(
            org_role="operator",
            owner_team_id=_TEAM_A,
            visibility="team",
            row_visible=False,
        )
        assert _get(client, membership=False) == 404

    def test_member_can_get_team_private_pipeline(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="operator", owner_team_id=_TEAM_A, visibility="team")
        assert _get(client, membership=True) == 200

    def test_org_admin_can_get_team_private_pipeline(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="admin", owner_team_id=_TEAM_A, visibility="team")
        assert _get(client, membership=False) == 200

    def test_org_visible_pipeline_not_team_gated(
        self, make_client: Callable[..., tuple[TestClient, _ResolverRow]]
    ) -> None:
        client, _ = make_client(org_role="operator", owner_team_id=None, visibility="org")
        with patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new=AsyncMock(return_value=_make_pipeline(owner_team_id=None, visibility="org")),
        ):
            resp = client.get(f"/api/v1/pipelines/{_PIPELINE_ID}")
        assert resp.status_code == 200
        assert resp.json()["visibility"] == "org"


# ---------------------------------------------------------------------------
# FAR-1515: the SAVE-side team gate — a team pipeline must not pin an
# org-only connector. The GET tests above cover the read direction; this
# covers the write direction, through the exact helper the REST graph-save
# endpoints call (``_enforce_connector_team_bindings``).
# ---------------------------------------------------------------------------

_TEAM_B = uuid.UUID("00000000-0000-0000-0000-000000000005")


def _connector_row(*, visibility: str, owner_team_id: uuid.UUID | None) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.name = "shared-ci"
    row.visibility = visibility
    row.owner_team_id = owner_team_id
    return row


def _binding_for(connector: MagicMock) -> list[dict[str, str]]:
    return [{"node_id": "node-1", "connector_instance_id": str(connector.id)}]


def _enforcement_session(connector: MagicMock) -> AsyncMock:
    """Session double answering the one query the connector team gate issues."""
    session = AsyncMock()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = [connector]
    result.scalars.return_value = scalars
    session.execute = AsyncMock(return_value=result)
    return session


class TestOrgOnlyConnectorRejectedAtGraphSave:
    """FAR-1515: save must mirror what the run actually enforces.

    Every run of a team-owned pipeline is team-scoped, and the ConnectorHub
    ACL fails closed on team-scoped access to an org-only connector — so a
    save accepted here produced a graph whose runs could never execute.
    """

    async def test_team_pipeline_pinning_an_org_only_connector_is_409(self) -> None:
        """FAILS without the new check: the rule returned False for org connectors."""
        from fastapi import HTTPException

        from modulo.api.routes.pipelines import _enforce_connector_team_bindings

        connector = _connector_row(visibility="org", owner_team_id=None)
        session = _enforcement_session(connector)

        with pytest.raises(HTTPException) as excinfo:
            await _enforce_connector_team_bindings(session, _ORG_ID, _TEAM_A, _binding_for(connector))

        assert excinfo.value.status_code == 409
        detail = str(excinfo.value.detail)
        assert detail.startswith("connector_team_mismatch"), detail
        assert "shared-ci" in detail
        assert "is org-only" in detail
        assert "flip the connector to `team`" in detail

    async def test_org_pipeline_pinning_an_org_only_connector_still_saves(self) -> None:
        """Regression guard: the ORG-pipeline rule must not change (FAR-1515)."""
        from modulo.api.routes.pipelines import _enforce_connector_team_bindings

        connector = _connector_row(visibility="org", owner_team_id=None)
        session = _enforcement_session(connector)

        await _enforce_connector_team_bindings(session, _ORG_ID, None, _binding_for(connector))

        session.execute.assert_awaited_once()

    async def test_team_pipeline_pinning_its_own_team_connector_still_saves(self) -> None:
        """Regression guard: the existing valid binding must not start failing."""
        from modulo.api.routes.pipelines import _enforce_connector_team_bindings

        connector = _connector_row(visibility="team", owner_team_id=_TEAM_A)
        session = _enforcement_session(connector)

        await _enforce_connector_team_bindings(session, _ORG_ID, _TEAM_A, _binding_for(connector))

        session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# FAR-1515 expansion — the write paths that used to bypass the gate:
# MAJOR 2 (ownership-transfer PATCH), MAJOR 4 (workflow import confirm),
# MAJOR 5 (connector visibility/owner re-scope).
# ---------------------------------------------------------------------------

_PREFIX = "modulo.api.routes.pipelines."
_LIB_PREFIX = "modulo.api.routes.library."
_CONN_PREFIX = "modulo.api.routes.connectors."


def _pipeline_row_with_binding(
    *,
    owner_team_id: uuid.UUID | None,
    visibility: str,
    instance_id: uuid.UUID,
) -> MagicMock:
    """A pipeline whose STORED graph pins ``instance_id`` (the bindings the
    ownership-transfer gate extracts when no graph_json ships in the PATCH)."""
    p = _make_pipeline(owner_team_id=owner_team_id, visibility=visibility)
    p.graph_nodes_json = [
        {
            "id": "node-1",
            "node_type": "agent",
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 0, "y": 0},
            "connector_binding": {"type": "github", "instance_id": str(instance_id)},
        }
    ]
    return p


def _mismatch(*, connector_id: uuid.UUID, pipeline_owner_team_id: uuid.UUID | None) -> MagicMock:
    return MagicMock(
        connector_id=connector_id,
        connector_name="shared-ci",
        connector_owner_team_id=None,
        pipeline_owner_team_id=pipeline_owner_team_id,
        connector_visibility="org",
        node_id="node-1",
    )


class TestOwnershipTransferRunsConnectorTeamGate:
    """FAR-1515 MAJOR 2: a PATCH that changes only owner_team_id/visibility.

    The gate used to run ONLY when the payload also carried ``graph_json``
    (``_apply_graph_update``); ``connector_rebind_required`` on the response
    was advisory and consumed by nothing, so a transfer re-created the
    forbidden state (stored graph still pinning a connector the NEW owner
    team may not use) with no 409.
    """

    def test_owner_team_change_gates_the_stored_graph(self, make_client: Callable[..., tuple[TestClient, Any]]) -> None:
        """FAILS without the fix: the endpoint never calls find_connector_team_mismatches -> 200."""
        conn_id = uuid.uuid4()
        current = _pipeline_row_with_binding(owner_team_id=None, visibility="org", instance_id=conn_id)
        updated = _make_pipeline(owner_team_id=_TEAM_A, visibility="team")
        client, _ = make_client(org_role="admin", owner_team_id=None, visibility="org")
        with (
            patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
            patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
            patch(
                f"{_PREFIX}find_connector_team_mismatches",
                new=AsyncMock(return_value=[_mismatch(connector_id=conn_id, pipeline_owner_team_id=_TEAM_A)]),
            ) as find_mismatches,
        ):
            resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"owner_team_id": str(_TEAM_A)})

        assert resp.status_code == 409, resp.text
        detail = str(resp.json()["detail"])
        assert detail.startswith("connector_team_mismatch"), detail
        assert "shared-ci" in detail
        # The gate ran against the EFFECTIVE NEW owner team and the STORED
        # bindings — not the payload's (absent) graph and not the old owner.
        find_mismatches.assert_awaited_once()
        kwargs = find_mismatches.await_args.kwargs
        assert kwargs["pipeline_owner_team_id"] == _TEAM_A
        assert kwargs["connector_bindings"] == [{"node_id": "node-1", "connector_instance_id": str(conn_id)}]

    def test_owner_team_change_with_an_unbound_graph_skips_the_gate(
        self, make_client: Callable[..., tuple[TestClient, Any]]
    ) -> None:
        """No stored bindings -> nothing to gate -> the transfer still succeeds.

        Pins that the gate is scoped to the stored bindings (no spurious 409
        for an unbound pipeline).
        """
        current = _make_pipeline(owner_team_id=None, visibility="org")
        current.graph_nodes_json = []
        updated = _make_pipeline(owner_team_id=_TEAM_A, visibility="team")
        client, _ = make_client(org_role="admin", owner_team_id=None, visibility="org")
        with (
            patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
            patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
            patch(f"{_PREFIX}find_connector_team_mismatches", new=AsyncMock()) as find_mismatches,
        ):
            resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"owner_team_id": str(_TEAM_A)})

        assert resp.status_code == 200, resp.text
        find_mismatches.assert_not_awaited()

    def test_a_patch_that_keeps_the_team_boundary_does_not_gate(
        self, make_client: Callable[..., tuple[TestClient, Any]]
    ) -> None:
        """Only owner_team_id/visibility CHANGES trigger the gate — a rename
        (or a same-value owner assignment) must not issue the lookup."""
        current = _pipeline_row_with_binding(owner_team_id=_TEAM_A, visibility="team", instance_id=uuid.uuid4())
        updated = _make_pipeline(owner_team_id=_TEAM_A, visibility="team")
        updated.name = "Renamed"
        client, _ = make_client(org_role="admin", owner_team_id=_TEAM_A, visibility="team")
        with (
            patch(f"{_PREFIX}_get_pipeline_or_404", new=AsyncMock(return_value=current)),
            patch(f"{_PREFIX}update_pipeline", new=AsyncMock(return_value=updated)),
            patch(f"{_PREFIX}find_connector_team_mismatches", new=AsyncMock()) as find_mismatches,
        ):
            resp = client.patch(f"/api/v1/pipelines/{_PIPELINE_ID}", json={"name": "Renamed"})

        assert resp.status_code == 200, resp.text
        find_mismatches.assert_not_awaited()


class TestConfirmImportRunsConnectorTeamGate:
    """FAR-1515 MAJOR 4: POST /libraries/import/confirm.

    The import accepts an ``owner_team_id``, rewires node bindings to real org
    connectors, and writes the graph — with no team check of its own. The gate
    runs after materialisation (rewired ids in hand) inside the import
    transaction, so the named 409 rolls the whole import back.
    """

    def test_import_of_a_team_pipeline_pinning_an_org_connector_is_409(
        self, make_client: Callable[..., tuple[TestClient, Any]]
    ) -> None:
        """FAILS without the fix: no gate call -> the import completes 200."""
        conn_id = uuid.uuid4()
        materialized = {
            "pipeline_id": str(_PIPELINE_ID),
            "pipeline_name": "Imported",
            "primitive_id": str(uuid.uuid4()),
            "agent_count": 0,
            "edge_count": 0,
            "schema_count": 0,
            "warnings": [],
            # The rewired bindings materialize_import returns for the gate.
            "connector_bindings": [{"node_id": "node-1", "connector_instance_id": str(conn_id)}],
        }
        client, _ = make_client(org_role="admin")
        with (
            patch(f"{_LIB_PREFIX}validate_owner_team_for_create", new=AsyncMock()),
            patch(f"{_LIB_PREFIX}materialize_import", new=AsyncMock(return_value=materialized)),
            patch(
                f"{_LIB_PREFIX}find_connector_team_mismatches",
                new=AsyncMock(return_value=[_mismatch(connector_id=conn_id, pipeline_owner_team_id=_TEAM_A)]),
            ) as find_mismatches,
        ):
            resp = client.post(
                "/api/v1/libraries/import/confirm",
                json={
                    "bundle_json": json.dumps({"format_version": 1, "pipeline": {}}),
                    "owner_team_id": str(_TEAM_A),
                },
            )

        assert resp.status_code == 409, resp.text
        detail = str(resp.json()["detail"])
        assert detail.startswith("connector_team_mismatch"), detail
        assert "shared-ci" in detail
        find_mismatches.assert_awaited_once()
        kwargs = find_mismatches.await_args.kwargs
        assert kwargs["pipeline_owner_team_id"] == _TEAM_A
        assert kwargs["connector_bindings"] == [{"node_id": "node-1", "connector_instance_id": str(conn_id)}]

    def test_import_without_resolvable_bindings_still_completes(
        self, make_client: Callable[..., tuple[TestClient, Any]]
    ) -> None:
        """An import whose graph carries no connector bindings is not gated."""
        materialized = {
            "pipeline_id": str(_PIPELINE_ID),
            "pipeline_name": "Imported",
            "primitive_id": str(uuid.uuid4()),
            "agent_count": 0,
            "edge_count": 0,
            "schema_count": 0,
            "warnings": [],
            "connector_bindings": [],
        }
        client, _ = make_client(org_role="admin")
        with (
            patch(f"{_LIB_PREFIX}validate_owner_team_for_create", new=AsyncMock()),
            patch(f"{_LIB_PREFIX}materialize_import", new=AsyncMock(return_value=materialized)),
            patch(f"{_LIB_PREFIX}find_connector_team_mismatches", new=AsyncMock()) as find_mismatches,
        ):
            resp = client.post(
                "/api/v1/libraries/import/confirm",
                json={"bundle_json": json.dumps({"format_version": 1, "pipeline": {}})},
            )

        assert resp.status_code == 200, resp.text
        find_mismatches.assert_not_awaited()


def _connector_instance_row(*, visibility: str, owner_team_id: uuid.UUID | None) -> MagicMock:
    ci = MagicMock()
    ci.id = uuid.uuid4()
    ci.organisation_id = _ORG_ID
    ci.name = "shared-ci"
    ci.connector_type_id = "github"
    ci.credentials_ciphertext = b"encrypted"
    ci.config_json = {}
    ci.allowed_operations = []
    ci.status = "active"
    ci.visibility = visibility
    ci.owner_team_id = owner_team_id
    ci.tier = "native"
    ci.created_at = _NOW
    ci.updated_at = _NOW
    ci.degraded_at = None
    ci.last_skip_error = None
    ci.validation_level = None
    return ci


def _bound_pipeline(*, owner_team_id: uuid.UUID | None, instance_id: uuid.UUID) -> MagicMock:
    p = MagicMock()
    p.owner_team_id = owner_team_id
    p.graph_nodes_json = [
        {
            "id": "node-1",
            "node_type": "agent",
            "agent_id": str(uuid.uuid4()),
            "position": {"x": 0, "y": 0},
            "connector_binding": {"type": "github", "instance_id": str(instance_id)},
        }
    ]
    return p


class TestConnectorReScopeRunsConnectorTeamGate:
    """FAR-1515 MAJOR 5: PATCH /connectors/{id} changing visibility/owner.

    ``validate_team_transition_for_update`` checks team MEMBERSHIP only, so
    flipping a bound connector to ``org`` (or handing it to another team)
    used to recreate the state the graph-save gate refuses, with no check of
    the pipelines already binding it.
    """

    def test_flipping_a_bound_connector_to_org_is_409(self, make_client: Callable[..., tuple[TestClient, Any]]) -> None:
        """FAILS without the fix: the bound-pipeline lookup never runs -> 200."""
        existing = _connector_instance_row(visibility="team", owner_team_id=_TEAM_A)
        updated = _connector_instance_row(visibility="org", owner_team_id=_TEAM_A)
        bound = _bound_pipeline(owner_team_id=_TEAM_A, instance_id=existing.id)
        client, _ = make_client(org_role="admin")
        with (
            patch(f"{_CONN_PREFIX}get_connector_instance", new=AsyncMock(return_value=existing)),
            patch(f"{_CONN_PREFIX}pipelines_binding_connector", new=AsyncMock(return_value=[bound])) as find_bound,
            patch(f"{_CONN_PREFIX}update_connector_instance", new=AsyncMock(return_value=updated)),
        ):
            resp = client.patch(f"/api/v1/connectors/{existing.id}", json={"visibility": "org"})

        assert resp.status_code == 409, resp.text
        detail = str(resp.json()["detail"])
        assert detail.startswith("connector_team_mismatch"), detail
        assert "shared-ci" in detail
        # Same named error the save path uses: team pipeline + org-only connector.
        assert "is org-only" in detail
        find_bound.assert_awaited_once()

    def test_a_re_scope_with_no_actual_change_does_not_query(
        self, make_client: Callable[..., tuple[TestClient, Any]]
    ) -> None:
        """PATCHing the SAME visibility back is a no-op: no bound-pipeline lookup."""
        existing = _connector_instance_row(visibility="org", owner_team_id=None)
        updated = _connector_instance_row(visibility="org", owner_team_id=None)
        client, _ = make_client(org_role="admin")
        with (
            patch(f"{_CONN_PREFIX}get_connector_instance", new=AsyncMock(return_value=existing)),
            patch(f"{_CONN_PREFIX}pipelines_binding_connector", new=AsyncMock()) as find_bound,
            patch(f"{_CONN_PREFIX}update_connector_instance", new=AsyncMock(return_value=updated)),
        ):
            resp = client.patch(f"/api/v1/connectors/{existing.id}", json={"visibility": "org"})

        assert resp.status_code == 200, resp.text
        find_bound.assert_not_awaited()

    def test_a_re_scope_with_no_bound_pipeline_succeeds(
        self, make_client: Callable[..., tuple[TestClient, Any]]
    ) -> None:
        """Nothing binds it -> the re-scope is allowed (predicate never fires)."""
        existing = _connector_instance_row(visibility="team", owner_team_id=_TEAM_A)
        updated = _connector_instance_row(visibility="org", owner_team_id=_TEAM_A)
        client, _ = make_client(org_role="admin")
        with (
            patch(f"{_CONN_PREFIX}get_connector_instance", new=AsyncMock(return_value=existing)),
            patch(f"{_CONN_PREFIX}pipelines_binding_connector", new=AsyncMock(return_value=[])) as find_bound,
            patch(f"{_CONN_PREFIX}update_connector_instance", new=AsyncMock(return_value=updated)),
        ):
            resp = client.patch(f"/api/v1/connectors/{existing.id}", json={"visibility": "org"})

        assert resp.status_code == 200, resp.text
        find_bound.assert_awaited_once()
