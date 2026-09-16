"""BDD step definitions: Onboarding action checklist (shipped /api/v1/onboarding surface).

Describes the actual shipped product: a persisted 6-action onboarding checklist
driven by real org state (`GET /api/v1/onboarding/status`, action
complete/skip/dismiss, seed-examples, starter-pipeline) instead of the fictional
5-step SDLC wizard the previous feature file documented (the "BDD drift" gap in
`docs/product-map/auth/onboarding.md`).
"""

import contextlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.db.models.onboarding_progress import OnboardingProgress

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/onboarding/sdlc_onboarding.feature")

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

_ACTION_IDS = [
    "login",
    "add_ai_model",
    "create_first_agent",
    "create_first_schema",
    "create_first_pipeline",
    "run_first_pipeline",
]

_STARTER_PIPELINE_NAME = "SDLC Starter Pipeline"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _reset_progress(request) -> None:
    """Start the scenario's persisted onboarding state from empty."""
    progress = OnboardingProgress(
        organisation_id=_ORG_ID,
        completed_actions=[],
        skipped_actions=[],
        dismissed=False,
    )
    progress.id = uuid.uuid4()
    request.node._onboarding_progress = progress
    # The shipped API always auto-completes `login` (real default behaviour).
    request.node._onboarding_auto = {"login"}


def _progress(request) -> OnboardingProgress:
    if not hasattr(request.node, "_onboarding_progress"):
        _reset_progress(request)
    return request.node._onboarding_progress


def _progress_patch(request) -> patch:
    """Patch ``_get_or_create_progress`` to serve the scenario's shared row."""
    return patch(
        "modulo.api.routes.onboarding._get_or_create_progress",
        new=AsyncMock(side_effect=lambda *args, **kwargs: _progress(request)),
    )


def _status_patches(request):
    """Patches the status read: shared progress row + controllable auto-completion.

    By default ``_check_auto_completion`` is patched to return the scenario's
    ``_onboarding_auto`` set. When a scenario opts into real detection (via the
    "stored organisation" givens) the patch is omitted, so the six ``select``
    probes in the shipped function run against the scenario's ``mock_session``.

    The default is resolved with ``getattr`` so a status read never depends on a
    scenario having run a given that seeds ``_onboarding_auto``; it falls back to
    the real default of only ``login`` auto-completing.
    """
    auto = getattr(request.node, "_onboarding_auto", {"login"})
    patches = [_progress_patch(request)]
    if not getattr(request.node, "_onboarding_real_detection", False):
        patches.append(patch("modulo.api.routes.onboarding._check_auto_completion", new=AsyncMock(return_value=auto)))
    return tuple(patches)


def _read_status(request) -> None:
    """Re-run ``GET /api/v1/onboarding/status`` against the current shared row."""
    with contextlib.ExitStack() as stack:
        for patcher in _status_patches(request):
            stack.enter_context(patcher)
        request.node._resp = request.node._client.get("/api/v1/onboarding/status")


# ---------------------------------------------------------------------------
# Background / Given
# ---------------------------------------------------------------------------


@given("the onboarding progress is fully empty")
def onboarding_progress_empty(request) -> None:
    _reset_progress(request)


@given(parsers.parse('the org state auto-completes only the "{action}" action'))
def auto_completes_single(action: str, request) -> None:
    _reset_progress(request)
    request.node._onboarding_auto = {action}


@given("the org state auto-completes every onboarding action")
def auto_completes_all(request) -> None:
    _reset_progress(request)
    request.node._onboarding_auto = set(_ACTION_IDS)


@given("the stored organisation already has every onboarding primitive")
def stored_org_has_every_primitive(mock_session, request) -> None:
    """Drive the real ``_check_auto_completion`` probes to report a populated org.

    The shipped probes call ``mock_session.execute(<select>)`` once per
    primitive and read ``.scalar_one_or_none()`` off each result, so setting
    that return value is what makes "real detection" report state. A truthy row
    means the primitive exists; ``None`` means it is absent (see the bare-org
    given below).
    """
    _reset_progress(request)
    request.node._onboarding_real_detection = True
    mock_session.execute.return_value.scalar_one_or_none.return_value = MagicMock()


@given("the stored organisation has no onboarding primitives")
def stored_org_has_no_primitives(mock_session, request) -> None:
    """Drive the real probes to report a bare org (only ``login`` auto-completes)."""
    _reset_progress(request)
    request.node._onboarding_real_detection = True
    mock_session.execute.return_value.scalar_one_or_none.return_value = None


@given(parsers.parse('the onboarding progress has the "{action}" action completed'))
def progress_has_completed(action: str, request) -> None:
    _reset_progress(request)
    _progress(request).completed_actions = [action]


@given("the org has a model backend configured")
def org_has_model_backend(request) -> None:
    model_backend = MagicMock()
    model_backend.id = uuid.uuid4()
    request.node._onboarding_model_backend = model_backend


@given("the org has no model backend configured")
def org_has_no_model_backend(request) -> None:
    request.node._onboarding_model_backend = None


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("I GET the onboarding status")
def get_onboarding_status(client, request) -> None:
    with contextlib.ExitStack() as stack:
        for patcher in _status_patches(request):
            stack.enter_context(patcher)
        request.node._resp = client.get("/api/v1/onboarding/status")


@when(parsers.parse('I complete the onboarding action "{action}"'))
def complete_onboarding_action(action: str, client, request) -> None:
    with _progress_patch(request):
        request.node._resp = client.post(f"/api/v1/onboarding/actions/{action}/complete")


@when(parsers.parse('I skip the onboarding action "{action}"'))
def skip_onboarding_action(action: str, client, request) -> None:
    with _progress_patch(request):
        request.node._resp = client.post(f"/api/v1/onboarding/actions/{action}/skip")


@when("I dismiss the onboarding wizard")
def dismiss_onboarding_wizard(client, request) -> None:
    with _progress_patch(request):
        request.node._resp = client.post("/api/v1/onboarding/dismiss")


@when("I seed the onboarding examples")
def seed_onboarding_examples(client, mock_session, request) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_agent = MagicMock()
    mock_agent.id = uuid.uuid4()
    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    mock_pipeline.rate_limit_config = None
    mock_pipeline.max_duration_seconds = None
    mock_pipeline.archived_at = None
    mock_pipeline.snapshot_count = 0

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = request.node._onboarding_model_backend
    mock_session.execute = AsyncMock(return_value=mock_result)

    request.node._onboarding_seed_ids = {
        "schema_id": str(mock_schema.id),
        "agent_id": str(mock_agent.id),
        "pipeline_id": str(mock_pipeline.id),
    }

    with (
        _progress_patch(request),
        patch("modulo.api.routes.onboarding.create_schema", return_value=mock_schema) as create_schema,
        patch("modulo.api.routes.onboarding.create_schema_version"),
        patch("modulo.api.routes.onboarding.create_agent", return_value=mock_agent) as create_agent,
        patch("modulo.api.routes.onboarding.create_pipeline", return_value=mock_pipeline) as create_pipeline,
        patch("modulo.api.routes.onboarding.replace_pipeline_graph"),
        patch("modulo.api.routes.onboarding.set_rls_org"),
        patch("modulo.api.routes.onboarding.set_rls_user_context"),
    ):
        request.node._onboarding_create_schema = create_schema
        request.node._onboarding_create_agent = create_agent
        request.node._onboarding_create_pipeline = create_pipeline
        request.node._resp = client.post("/api/v1/onboarding/seed-examples")


@when("I create the onboarding starter pipeline")
def create_starter_pipeline(client, request) -> None:
    mock_schema = MagicMock()
    mock_schema.id = uuid.uuid4()
    mock_pipeline = MagicMock()
    mock_pipeline.id = uuid.uuid4()
    mock_pipeline.name = _STARTER_PIPELINE_NAME
    mock_pipeline.rate_limit_config = None
    mock_pipeline.max_duration_seconds = None
    mock_pipeline.archived_at = None
    mock_pipeline.snapshot_count = 0
    request.node._onboarding_starter_pipeline_id = str(mock_pipeline.id)
    with (
        patch("modulo.api.routes.onboarding.create_schema", return_value=mock_schema),
        patch("modulo.api.routes.onboarding.create_pipeline", return_value=mock_pipeline),
        patch("modulo.api.routes.onboarding.replace_pipeline_graph"),
        patch("modulo.api.routes.onboarding.set_rls_org"),
        patch("modulo.api.routes.onboarding.set_rls_user_context"),
    ):
        request.node._resp = client.post("/api/v1/onboarding/starter-pipeline")


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then("the response indicates this is the first run")
def response_is_first_run(request) -> None:
    body = request.node._resp.json()
    assert body["is_first_run"] is True, f"Expected is_first_run=true, got {body}"


@then(parsers.parse('the response exposes {n:d} onboarding actions in order starting with "{first}"'))
def response_exposes_actions(n: int, first: str, request) -> None:
    body = request.node._resp.json()
    actions = body["actions"]
    ids = [action["id"] for action in actions]
    assert ids == _ACTION_IDS, f"Expected actions {_ACTION_IDS}, got {ids}"
    assert len(actions) == n
    assert actions[0]["id"] == first


@then(parsers.parse('the response reports the "{action}" action as completed'))
def response_reports_action_completed(action: str, request) -> None:
    body = request.node._resp.json()
    assert action in set(body["completed_actions"]), f"Action {action} not completed: {body}"
    entry = next(a for a in body["actions"] if a["id"] == action)
    assert entry["completed"] is True, f"Action {action} not marked completed: {entry}"


@then(parsers.re(r"the response reports (?:all )?(?P<n>\d+) onboarding actions? as completed"))
def response_reports_n_actions_completed(n: str, request) -> None:
    count = int(n)
    body = request.node._resp.json()
    assert len(body["completed_actions"]) == count, f"Expected {count} completed actions, got {body}"


@then("the response reports 100% progress")
def response_reports_full_progress(request) -> None:
    body = request.node._resp.json()
    assert body["progress_pct"] == pytest.approx(100.0), f"Expected 100% progress, got {body}"


@then(parsers.parse('the response reports action "{action}" as completed'))
def response_action_completed_payload(action: str, request) -> None:
    body = request.node._resp.json()
    assert body["action_id"] == action
    assert body["completed"] is True


@then(parsers.parse('the response reports action "{action}" as skipped'))
def response_action_skipped_payload(action: str, request) -> None:
    body = request.node._resp.json()
    assert body["action_id"] == action
    assert body["skipped"] is True


@then(parsers.parse("a subsequent status read reports {n:d} completed actions at {pct}% progress"))
def subsequent_status_read(n: int, pct: str, request) -> None:
    _read_status(request)
    body = request.node._resp.json()
    assert len(body["completed_actions"]) == n, f"Expected {n} completed actions, got {body}"
    assert body["progress_pct"] == pytest.approx(float(pct), rel=0.05), f"Unexpected progress, got {body}"


@then(parsers.parse("a subsequent status read still reports exactly {n:d} completed action"))
def subsequent_status_read_still_n(n: int, request) -> None:
    _read_status(request)
    body = request.node._resp.json()
    assert len(body["completed_actions"]) == n, f"Expected exactly {n} completed actions, got {body}"


@then(parsers.parse('a subsequent status read reports "{action}" as skipped'))
def subsequent_status_read_skipped(action: str, request) -> None:
    _read_status(request)
    body = request.node._resp.json()
    assert action in set(body["skipped_actions"]), f"Action {action} not skipped: {body}"
    entry = next(a for a in body["actions"] if a["id"] == action)
    assert entry["skipped"] is True, f"Action {action} not marked skipped: {entry}"
    assert entry["completed"] is False, f"Skipped action {action} must not be completed: {entry}"


@then("a subsequent status read no longer reports a first run")
def subsequent_status_read_not_first(request) -> None:
    _read_status(request)
    body = request.node._resp.json()
    assert body["dismissed"] is True, f"Expected dismissed=true, got {body}"
    assert body["is_first_run"] is False, f"Expected is_first_run=false, got {body}"


@then("the seed response carries an agent, a schema, and a pipeline")
def seed_response_carries_ids(request) -> None:
    body = request.node._resp.json()
    expected = request.node._onboarding_seed_ids
    assert body["agent_id"] == expected["agent_id"], f"Unexpected agent_id: {body}"
    assert body["schema_id"] == expected["schema_id"], f"Unexpected schema_id: {body}"
    assert body["pipeline_id"] == expected["pipeline_id"], f"Unexpected pipeline_id: {body}"


@then("the seed marks the schema, agent, and pipeline actions completed")
def seed_marks_actions_completed(request) -> None:
    progress = _progress(request)
    for action in ("create_first_schema", "create_first_agent", "create_first_pipeline"):
        assert action in progress.completed_actions, f"Action {action} not completed by seed"


@then("the refusal says a model backend is required")
def seed_refusal_mentions_model_backend(request) -> None:
    detail = request.node._resp.json()["detail"]
    assert "model backend" in str(detail).lower(), f"Unexpected refusal detail: {detail}"


@then("no schema, agent, or pipeline was created")
def seed_refusal_writes_nothing(request) -> None:
    request.node._onboarding_create_schema.assert_not_called()
    request.node._onboarding_create_agent.assert_not_called()
    request.node._onboarding_create_pipeline.assert_not_called()


@then("the starter pipeline response carries a pipeline id and the starter name")
def starter_pipeline_response(request) -> None:
    body = request.node._resp.json()
    assert body["pipeline_id"] == request.node._onboarding_starter_pipeline_id
    assert body["name"] == _STARTER_PIPELINE_NAME
