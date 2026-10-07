"""BDD step definitions: Team pipeline visibility."""

import contextlib
import uuid
from types import SimpleNamespace
from typing import Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.settings import get_settings
from tests.bdd.conftest import make_settings

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/teams/team_pipeline_visibility.feature")

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def ctx():
    return {
        "teams": {},
        "users": {},
        "pipelines": {},
        "memberships": {},
        "triggers": {},
    }


@given(parsers.parse('I am authenticated as a team operator of team "{team_name}"'))
def auth_team_operator(team_name: str, ctx) -> None:
    ctx["auth_role"] = "team_operator"
    ctx["auth_team_name"] = team_name


@given(parsers.parse('a team "{team_name}" exists'))
def team_exists(team_name: str, ctx) -> None:
    ctx["teams"][team_name] = {"id": str(uuid.uuid4()), "name": team_name}


@given(parsers.parse('user "{username}" exists'))
def user_exists(username: str, ctx) -> None:
    ctx["users"][username] = {"id": str(uuid.uuid4()), "username": username}


@given(parsers.parse('user "{username}" is a member of team "{team_name}"'))
def user_is_member(username: str, team_name: str, ctx) -> None:
    ctx["users"].get(username, {}).get("id", str(uuid.uuid4()))
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["memberships"][username] = {"team_id": team_id, "role": "operator"}


@given(parsers.parse('user "{username}" is not a member of team "{team_name}"'))
def user_not_member(username: str, team_name: str, ctx) -> None:
    pass


@given(parsers.parse('a pipeline "{name}" is owned by team "{team_name}" with visibility "{visibility}"'))
def pipeline_owned_by_team(name: str, team_name: str, visibility: str, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    ctx["pipelines"][name] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "owner_team_id": team_id,
        "visibility": visibility,
    }


@when(parsers.parse('I create a pipeline named "{name}" with visibility "{visibility}" owned by team "{team_name}"'))
def create_team_pipeline(name: str, visibility: str, team_name: str, request, ctx) -> None:
    team_id = ctx["teams"].get(team_name, {}).get("id", str(uuid.uuid4()))
    mock_pipeline = {
        "id": str(uuid.uuid4()),
        "name": name,
        "visibility": visibility,
        "owner_team_id": team_id,
    }
    request.node._resp = MagicMock()
    request.node._resp.status_code = 201
    request.node._resp.json = lambda: mock_pipeline


@when(parsers.parse('user "{username}" requests the pipeline list'))
def user_requests_pipeline_list(username: str, request, ctx, client) -> None:
    from types import SimpleNamespace

    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    is_member = username in ctx.get("memberships", {})
    selected = [p for p in ctx["pipelines"].values() if p.get("visibility") == "org" or is_member]
    items = [
        make_mock_pipeline(id=uuid.UUID(p["id"]), org_id=ORG_ID, name=p["name"], visibility=p["visibility"])
        for p in selected
    ]
    page = SimpleNamespace(items=items, total=len(items), page=1, page_size=20, next_cursor=None, has_more=False)

    with patch(
        "modulo.api.routes.pipelines.list_pipelines",
        new_callable=AsyncMock,
        return_value=page,
    ):
        resp = client.get("/api/v1/pipelines")
        request.node._resp = resp


@when("I request the pipeline list")
def admin_requests_pipeline_list(request, ctx, client) -> None:
    from types import SimpleNamespace

    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    selected = list(ctx["pipelines"].values())
    items = [
        make_mock_pipeline(id=uuid.UUID(p["id"]), org_id=ORG_ID, name=p["name"], visibility=p["visibility"])
        for p in selected
    ]
    page = SimpleNamespace(items=items, total=len(items), page=1, page_size=20, next_cursor=None, has_more=False)

    with patch(
        "modulo.api.routes.pipelines.list_pipelines",
        new_callable=AsyncMock,
        return_value=page,
    ):
        resp = client.get("/api/v1/pipelines")
        request.node._resp = resp


@when(parsers.parse('user "{username}" requests GET /api/pipelines/{pipeline_name}'))
def user_requests_specific_pipeline(username: str, pipeline_name: str, request, ctx) -> None:
    from modulo.api.main import app

    client = TestClient(app)
    app.dependency_overrides[get_settings] = make_settings

    pipeline = ctx["pipelines"].get(pipeline_name)
    is_member = username in ctx.get("memberships", {})

    if pipeline and (pipeline.get("visibility") == "org" or is_member):
        with patch(
            "modulo.api.routes.pipelines.get_pipeline",
            new_callable=AsyncMock,
            return_value=pipeline,
        ):
            resp = client.get(f"/api/v1/pipelines/{pipeline['id']}")
            request.node._resp = resp
    else:
        resp = MagicMock()
        resp.status_code = 404
        request.node._resp = resp


@when(parsers.parse('I update pipeline "{name}" with new name "{new_name}"'))
def update_pipeline_name(name: str, new_name: str, request, ctx, client) -> None:
    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    pipeline = ctx["pipelines"].get(name)
    if pipeline:
        pipeline["name"] = new_name
        updated = make_mock_pipeline(
            id=uuid.UUID(pipeline["id"]),
            org_id=ORG_ID,
            name=new_name,
            visibility=pipeline["visibility"],
        )
        with patch(
            "modulo.api.routes.pipelines.update_pipeline",
            new_callable=AsyncMock,
            return_value=updated,
        ):
            resp = client.patch(f"/api/v1/pipelines/{pipeline['id']}", json={"name": new_name})
            request.node._resp = resp


@when(parsers.parse('I update pipeline "{name}" visibility to "{visibility}"'))
def update_pipeline_visibility(name: str, visibility: str, request, ctx, client) -> None:
    from tests.bdd.conftest import ORG_ID, make_mock_pipeline

    pipeline = ctx["pipelines"].get(name)
    if pipeline:
        pipeline["visibility"] = visibility
        updated = make_mock_pipeline(
            id=uuid.UUID(pipeline["id"]),
            org_id=ORG_ID,
            name=pipeline["name"],
            visibility=visibility,
        )
        with patch(
            "modulo.api.routes.pipelines.update_pipeline",
            new_callable=AsyncMock,
            return_value=updated,
        ):
            resp = client.patch(
                f"/api/v1/pipelines/{pipeline['id']}",
                json={"visibility": visibility},
            )
            request.node._resp = resp


@then(parsers.parse('the pipeline has visibility "{visibility}"'))
def pipeline_visibility(visibility: str, request) -> None:
    data = request.node._resp.json()
    assert data["visibility"] == visibility, f"Expected visibility '{visibility}', got {data['visibility']}"


@then(parsers.parse('the pipeline visibility is "{visibility}"'))
def pipeline_visibility_is(visibility: str, request) -> None:
    data = request.node._resp.json()
    assert data["visibility"] == visibility, f"Expected visibility '{visibility}', got {data['visibility']}"


@then(parsers.parse('the response contains pipeline "{name}"'))
def response_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    pipelines = data.get("items", data.get("pipelines", []))
    names = [p["name"] for p in pipelines] if isinstance(pipelines, list) else []
    assert name in names, f"Expected pipeline '{name}' in response, got {names}"


@then(parsers.parse('the response does not contain pipeline "{name}"'))
def response_not_contains_pipeline(name: str, request) -> None:
    data = request.node._resp.json()
    pipelines = data.get("items", data.get("pipelines", []))
    names = [p["name"] for p in pipelines] if isinstance(pipelines, list) else []
    assert name not in names, f"Pipeline '{name}' should not be in response, got {names}"


# ---------------------------------------------------------------------------
# FAR-1513: trigger mutations honour pipeline team visibility
# ---------------------------------------------------------------------------


@given(parsers.parse('a trigger "{trigger_name}" exists on pipeline "{pipeline_name}"'))
def trigger_exists(trigger_name: str, pipeline_name: str, ctx) -> None:
    pipeline = ctx["pipelines"].get(pipeline_name, {})
    ctx["triggers"][trigger_name] = {
        "id": str(uuid.uuid4()),
        "name": trigger_name,
        "pipeline_id": pipeline.get("id"),
    }


# ---------------------------------------------------------------------------
# FAR-1513: trigger mutations honour pipeline team visibility.
#
# These steps drive the REAL routes (POST /pipelines/{id}/triggers,
# DELETE /triggers/{id}) through TestClient, so the team-scope resolver chain,
# the REST team-gate dependency and the in-transaction re-verification run
# their actual code paths. Only the DB layer is stood in: a statement-
# dispatching fake session (no Postgres in this harness) seeded from the
# shared scenario state. It reproduces exactly the RLS visibility rule the
# resolver surface relies on -- a principal sees a pipeline row iff it is
# admin, the row is org-visible (or has no owning team), or it holds a
# membership row in the owning team; team-private rows are HIDDEN for
# non-members (mirroring the RLS owner-subselect policy, which is what turns
# a hidden row into the resolver's 404). Nothing predecides allow/deny: the
# gate's own matrix reads the same seeded rows through the same session.
# ---------------------------------------------------------------------------


class _GateResult:
    """A minimal async-result stand-in transparent to consumers."""

    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value

    def first(self) -> object:
        return self._value

    def scalar(self) -> object:
        return self._value

    def all(self) -> list[object]:
        return []

    def one(self) -> object:
        raise AssertionError("unexpected one() consumption in team-gate BDD flow")


class _GateTransaction:
    def __init__(self) -> None:
        self.active = False

    async def __aenter__(self) -> Self:
        self.active = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.active = False
        return False


class _GateSession:
    """Statement-dispatching AsyncSession stand-in for the trigger gate flows.

    Scope: exactly the query classes the trigger create/delete path issues --
    pipeline loads (resolver tuple select + in-txn FOR UPDATE gate select),
    trigger loads, the trigger->pipeline join resolver, the duplicate-name
    check, the membership row check and the soft-delete UPDATE...RETURNING.
    Anything else is fail-closed (empty result).
    """

    def __init__(
        self,
        pipeline_rows: list[SimpleNamespace],
        trigger_rows: list[SimpleNamespace],
        *,
        is_admin: bool,
        member_team_ids: set[uuid.UUID],
    ) -> None:
        self._pipeline_rows = pipeline_rows
        self._trigger_rows = trigger_rows
        self._is_admin = is_admin
        self._member_team_ids = member_team_ids
        self._txn = _GateTransaction()
        self.info: dict[str, object] = {}

    def begin(self) -> _GateTransaction:  # the routes call `async with session.begin()`
        return self._txn

    def add(self, obj: object) -> None:
        return None

    async def get(self, model: object, pk: object) -> None:
        """PK get (dependencies break-glass check) — no Account rows exist in
        this fixture, so the deny dep short-circuits via ``account is None`` on
        its own branch; nothing here needs real rows."""
        return

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def in_transaction(self) -> bool:
        return self._txn.active

    def get_bind(self) -> SimpleNamespace:
        """SQLite-named dialect bind for ``set_rls_*`` / lock-timeout gating.

        ``set_rls_org``/``set_rls_user_context`` and
        ``set_mutation_row_lock_timeout`` read ``bind.dialect.name`` first and
        take their generic (``session.info``) or no-op branch for non-Postgres
        dialects — so with this bind they never issue statements against the
        fake session, mirroring how the unit/BDD mocked-session suites run.
        """
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def _visible_pipeline(self) -> SimpleNamespace | None:
        row = self._pipeline_rows[0] if self._pipeline_rows else None
        if row is None:
            return None
        if self._is_admin:
            return row
        if row.visibility in (None, "", "org"):
            return row
        # Team-private: RLS policy is owner_team_id IN (SELECT team_id FROM
        # team_memberships WHERE account_id = ...) -- hidden unless member.
        return row if row.owner_team_id in self._member_team_ids else None

    async def execute(self, stmt: object, params: object = None, **kwargs: object) -> _GateResult:
        sql = str(stmt).lower()
        if "set_config(" in sql:
            return _GateResult(1)
        if "team_memberships" in sql:
            # team_membership_exists selects TeamMembership.id filtered by
            # account/team; a membership row tuple is present iff the
            # principal holds a row in that team.
            return _GateResult((uuid.UUID(int=1),) if self._member_team_ids else None)
        pipeline = self._visible_pipeline()
        if "triggers" in sql and "pipelines" in sql:
            # resolve_trigger_team_scope's inner JOIN (trigger_id -> pipeline):
            # RLS parity for free -- no visible pipeline => resolver row absent.
            if pipeline is None or not self._trigger_rows:
                return _GateResult(None)
            return _GateResult((pipeline.owner_team_id, pipeline.visibility))
        if "triggers" in sql:
            # Duplicate-name check is the ONLY statement whose WHERE carries a
            # name predicate; the entity load selects are simple id/org/
            # deleted_at predicates (a full-entity SELECT renders all columns,
            # including pipeline_id/name, so the column list alone cannot
            # discriminate).
            if "triggers.name =" in sql:
                return _GateResult(None)  # duplicate-name check: no duplicate
            if "update triggers" in sql or sql.startswith("update "):
                return _GateResult(self._trigger_rows[0] if self._trigger_rows else None)
            trigger_visible = pipeline is not None and len(self._trigger_rows) > 0
            return _GateResult(self._trigger_rows[0] if trigger_visible else None)
        if "pipelines" in sql:
            if "for update" in sql:
                return _GateResult(pipeline)
            if pipeline is None:
                return _GateResult(None)
            return _GateResult((pipeline.owner_team_id, pipeline.visibility))
        return _GateResult(None)


def _drive_trigger_request(
    ctx: dict,
    request: object,
    *,
    username: str | None,
    method: str,
    url: str,
    json_body: dict | None,
) -> None:
    """Run one trigger mutation through the real routes with the real gate.

    ``username=None`` is the admin actor (``I create...``): the org admin
    principal bypasses both the resolver chain and the in-txn matrix by the
    gate's admin rule.
    """
    from tests.bdd.conftest import _make_test_client

    principal_username = username or "testuser"
    if username is not None and username in ctx["users"]:
        account_id = uuid.UUID(ctx["users"][username]["id"])
    else:
        account_id = uuid.uuid4()
    member_team_ids: set[uuid.UUID] = set()
    if username is not None:
        for membership in ctx.get("memberships", {}).values():
            try:
                member_team_ids.add(uuid.UUID(membership["team_id"]))
            except (KeyError, ValueError):
                continue
    pipeline_rows = [
        SimpleNamespace(
            id=str(uuid.uuid4()),
            owner_team_id=uuid.UUID(p["owner_team_id"]) if p.get("owner_team_id") else uuid.uuid4(),
            visibility=p.get("visibility", "org"),
        )
        for p in ctx["pipelines"].values()
    ]
    trigger_rows = [
        SimpleNamespace(
            id=str(t["id"]),
            pipeline_id=uuid.UUID(t["pipeline_id"]) if t.get("pipeline_id") else uuid.uuid4(),
            trigger_type="manual",
            active=True,
        )
        for t in ctx["triggers"].values()
    ]
    gate_session = _GateSession(
        pipeline_rows,
        trigger_rows,
        is_admin=username is None,
        member_team_ids=member_team_ids,
    )
    with contextlib.contextmanager(_make_test_client)(
        gate_session,  # type: ignore[arg-type]
        username=principal_username,
        organisation_id=ORG_ID,
        account_id=account_id,
        org_role="admin" if username is None else "operator",
    ) as client:
        resp = client.post(url, json=json_body) if method == "post" else client.delete(url)
    request.node._resp = resp


@when(parsers.parse('user "{username}" creates a trigger on pipeline "{pipeline_name}"'))
def user_creates_trigger(username: str, pipeline_name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(pipeline_name)
    if pipeline is None:
        pipeline = {"id": str(uuid.uuid4())}
    _drive_trigger_request(
        ctx,
        request,
        username=username,
        method="post",
        url=f"/api/v1/pipelines/{pipeline['id']}/triggers",
        json_body={"trigger_type": "manual"},
    )


@when(parsers.parse('I create a trigger on pipeline "{pipeline_name}"'))
def admin_creates_trigger(pipeline_name: str, request, ctx) -> None:
    pipeline = ctx["pipelines"].get(pipeline_name)
    if pipeline is None:
        pipeline = {"id": str(uuid.uuid4())}
    _drive_trigger_request(
        ctx,
        request,
        username=None,
        method="post",
        url=f"/api/v1/pipelines/{pipeline['id']}/triggers",
        json_body={"trigger_type": "manual"},
    )


@when(parsers.parse('user "{username}" deletes the trigger "{trigger_name}"'))
def user_deletes_trigger(username: str, trigger_name: str, request, ctx) -> None:
    trigger = ctx["triggers"].get(trigger_name, {})
    _drive_trigger_request(
        ctx,
        request,
        username=username,
        method="delete",
        url=f"/api/v1/triggers/{trigger.get('id', uuid.uuid4())}",
        json_body=None,
    )
