"""Unit coverage for the AI-assisted System Maps discovery route branches.

These exercise the guards inside ``discover_system_map`` and
``_resolve_discovery_eval`` directly. The end-to-end behaviour (real Postgres,
real credential decryption) is covered by
``tests/integration/test_system_maps_ai_discovery.py``; this module pins the
route's decision matrix so the changed-lines coverage gate has unit-level
evidence for every branch.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from farna.api.routes import system_maps as routes
from farna.api.schemas.system_maps import SystemMapDiscoveryRequest
from farna.auth.jwt import TenantPrincipal
from farna.core.eval_engine import EvalDefinition, EvalType
from farna.core.eval_engine.typed_judge import JudgeOutcome
from farna.core.model_backend_hub import BackendNotFoundError, BackendUnavailableError
from farna.core.system_maps_ai_discovery import PROMPT_VERSION, RUBRIC_VERSION
from farna.core.system_maps_discovery import DiscoveryDraft
from tests.unit.api.mock_session import configure_mock_session

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_MAP_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
_REVISION_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")
_BACKEND_ID = uuid.UUID("00000000-0000-0000-0000-000000000005")
_EVAL_ID = uuid.UUID("00000000-0000-0000-0000-000000000006")
_PIPELINE_ID = uuid.UUID("00000000-0000-0000-0000-000000000007")


def _principal() -> TenantPrincipal:
    return TenantPrincipal(
        username="internal",
        organisation_id=_ORG_ID,
        account_id=_USER_ID,
        org_role="operator",
        key_grants=frozenset({"system_maps_access"}),
    )


def _draft() -> DiscoveryDraft:
    return DiscoveryDraft(nodes=(), relationships=(), warnings=(), skipped=())


def _detail(exc: HTTPException) -> dict[str, Any]:
    assert isinstance(exc.detail, dict)
    return exc.detail


def _eval_definition() -> EvalDefinition:
    return EvalDefinition(
        id=_EVAL_ID,
        org_id=_ORG_ID,
        pipeline_id=_PIPELINE_ID,
        name="judge",
        eval_type=EvalType.MODEL_JUDGE,
        config={"answer_contract": "typed_questions", "questions": []},
        failure_behaviour="block",
        pass_threshold=0.5,
        version=3,
    )


def _eval_row(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": _EVAL_ID,
        "organisation_id": _ORG_ID,
        "pipeline_id": _PIPELINE_ID,
        "name": "judge",
        "eval_type": EvalType.MODEL_JUDGE.value,
        "config_json": {"answer_contract": "typed_questions", "questions": []},
        "pass_threshold": 0.5,
        "version": 3,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _inventory_session(backend_rows: list[Any] | None = None) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock()
    session.execute.side_effect = [
        *[MagicMock(scalars=MagicMock(return_value=[])) for _ in range(4)],
        MagicMock(scalars=MagicMock(return_value=backend_rows or [])),
    ]
    return session


class _Hub:
    def __init__(
        self,
        *,
        backend_ids: set[uuid.UUID],
        backend: Any = None,
        get_error: Exception | None = None,
    ) -> None:
        self.backend_ids = frozenset(backend_ids)
        self._backend = backend
        self._get_error = get_error
        self.initialise = AsyncMock()
        self.exited = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self.exited = True
        return False

    async def get(self, _backend_id: uuid.UUID) -> Any:
        if self._get_error is not None:
            raise self._get_error
        return self._backend


class _EvalEngine:
    def __init__(self, outcome: JudgeOutcome) -> None:
        self._outcome = outcome
        self.calls: list[tuple[Any, Any, Any]] = []

    async def evaluate_result_async(
        self,
        output: Any,
        eval_def: Any,
        *,
        model_judge_callable: Any = None,
    ) -> SimpleNamespace:
        self.calls.append((output, eval_def, model_judge_callable))
        return SimpleNamespace(outcome=self._outcome)


@contextmanager
def _persistable() -> Iterator[None]:
    with patch.object(
        routes,
        "_persist_discovery_draft",
        new=AsyncMock(return_value=(SimpleNamespace(id=_MAP_ID), _REVISION_ID, 1)),
    ):
        yield


async def _call(
    session: AsyncMock,
    *,
    model_backend_id: uuid.UUID | None = None,
    eval_definition_id: uuid.UUID | None = None,
) -> Any:
    request = SystemMapDiscoveryRequest(
        name="ai draft",
        model_backend_id=model_backend_id,
        eval_definition_id=eval_definition_id,
    )
    with _persistable():
        return await routes.discover_system_map(request, session=session, principal=_principal())


async def test_discover_rejects_eval_definition_without_backend() -> None:
    session = _inventory_session()

    with pytest.raises(HTTPException) as exc_info:
        await _call(session, eval_definition_id=_EVAL_ID)

    assert exc_info.value.status_code == 422
    assert _detail(exc_info.value)["code"] == "invalid_request"


def _eval_session(row: Any) -> AsyncMock:
    session = configure_mock_session(AsyncMock())
    session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=row)))
    return session


@pytest.mark.parametrize(
    ("row", "expected_code"),
    [
        (None, "invalid_eval_definition"),
        (_eval_row(eval_type="guardrail"), "invalid_eval_definition"),
    ],
)
async def test_resolve_discovery_eval_rejects_missing_or_non_model_judge(
    row: Any,
    expected_code: str,
) -> None:
    session = _eval_session(row)

    with pytest.raises(HTTPException) as exc_info:
        await routes._resolve_discovery_eval(
            session,
            eval_id=_EVAL_ID,
            org_id=_ORG_ID,
        )

    assert exc_info.value.status_code == 422
    assert _detail(exc_info.value)["code"] == expected_code


async def test_resolve_discovery_eval_does_not_consult_the_team_gate() -> None:
    session = _eval_session(_eval_row())

    with patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=object())) as gate:
        definition = await routes._resolve_discovery_eval(
            session,
            eval_id=_EVAL_ID,
            org_id=_ORG_ID,
        )

    gate.assert_not_awaited()
    assert definition.id == _EVAL_ID


async def test_resolve_discovery_eval_rejects_non_typed_contract() -> None:
    session = _eval_session(_eval_row(config_json={"answer_contract": "legacy"}))

    with pytest.raises(HTTPException) as exc_info:
        await routes._resolve_discovery_eval(
            session,
            eval_id=_EVAL_ID,
            org_id=_ORG_ID,
        )

    assert exc_info.value.status_code == 422
    assert _detail(exc_info.value)["code"] == "invalid_eval_definition"


async def test_resolve_discovery_eval_builds_definition_from_row() -> None:
    session = _eval_session(_eval_row())

    definition = await routes._resolve_discovery_eval(
        session,
        eval_id=_EVAL_ID,
        org_id=_ORG_ID,
    )

    assert definition.id == _EVAL_ID
    assert definition.org_id == _ORG_ID
    assert definition.pipeline_id == _PIPELINE_ID
    assert definition.eval_type is EvalType.MODEL_JUDGE
    assert definition.config["answer_contract"] == "typed_questions"
    assert definition.failure_behaviour == "block"
    assert definition.pass_threshold == 0.5
    assert definition.version == 3


async def test_discover_rejects_backend_not_visible_to_org() -> None:
    session = _inventory_session(backend_rows=[])

    with pytest.raises(HTTPException) as exc_info:
        await _call(session, model_backend_id=_BACKEND_ID)

    assert exc_info.value.status_code == 403
    assert _detail(exc_info.value)["code"] == "model_backend_unauthorised"


async def test_discover_rejects_team_scoped_backend() -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=uuid.uuid4(), visibility="team")
    session = _inventory_session(backend_rows=[backend])

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=object())),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _call(session, model_backend_id=_BACKEND_ID)

    assert exc_info.value.status_code == 403
    assert _detail(exc_info.value)["code"] == "model_backend_unauthorised"


async def test_discover_reports_unavailable_backend_when_hub_skips_it() -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids=set())

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _call(session, model_backend_id=_BACKEND_ID)

    assert exc_info.value.status_code == 503
    assert _detail(exc_info.value)["code"] == "model_backend_unavailable"
    assert hub.exited is True


@pytest.mark.parametrize(
    ("get_error", "expected_status", "expected_code"),
    [
        (BackendNotFoundError(_BACKEND_ID), 403, "model_backend_unauthorised"),
        (BackendUnavailableError(_BACKEND_ID), 503, "model_backend_unavailable"),
    ],
)
async def test_discover_maps_hub_get_errors(
    get_error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids={_BACKEND_ID}, get_error=get_error)

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _call(session, model_backend_id=_BACKEND_ID)

    assert exc_info.value.status_code == expected_status
    assert _detail(exc_info.value)["code"] == expected_code
    assert hub.exited is True


@pytest.mark.parametrize(
    ("outcome", "expected_code"),
    [
        ("error", "model_backend_unavailable"),
        ("fallback", "model_backend_invalid_response"),
    ],
)
async def test_discover_refuses_non_accepted_ai_outcome(outcome: str, expected_code: str) -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids={_BACKEND_ID}, backend=MagicMock())

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        patch.object(
            routes,
            "enhance_discovery_with_outcome",
            new=AsyncMock(return_value=(_draft(), outcome)),
        ),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _call(session, model_backend_id=_BACKEND_ID)

    assert exc_info.value.status_code == 503
    assert _detail(exc_info.value)["code"] == expected_code
    assert hub.exited is True


async def test_discover_reraises_and_closes_hub_when_initialise_fails() -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids={_BACKEND_ID})
    hub.initialise = AsyncMock(side_effect=RuntimeError("hub init failed"))
    request = SystemMapDiscoveryRequest(name="ai draft", model_backend_id=_BACKEND_ID)

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        pytest.raises(RuntimeError, match="hub init failed"),
    ):
        await routes.discover_system_map.__wrapped__(  # type: ignore[attr-defined]
            request,
            session=session,
            principal=_principal(),
        )

    assert hub.exited is True


async def test_discover_ai_without_eval_gate_stays_unrequested() -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids={_BACKEND_ID}, backend=MagicMock())

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        patch.object(
            routes,
            "enhance_discovery_with_outcome",
            new=AsyncMock(return_value=(_draft(), "accepted")),
        ),
    ):
        report = await _call(session, model_backend_id=_BACKEND_ID)

    assert report.ai_assisted is True
    assert report.eval_gate_passed is None
    assert report.eval_gate_verdict == "not_requested"
    assert report.provenance.evaluator_version is None
    assert hub.exited is True


async def test_discover_ai_success_records_provenance_and_eval_pass() -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids={_BACKEND_ID}, backend=MagicMock())
    engine = _EvalEngine(JudgeOutcome.PASS)
    judge = MagicMock()

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        patch.object(
            routes,
            "enhance_discovery_with_outcome",
            new=AsyncMock(return_value=(_draft(), "accepted")),
        ),
        patch.object(routes, "_resolve_discovery_eval", new=AsyncMock(return_value=_eval_definition())),
        patch.object(routes, "build_discovery_model_judge_callable", return_value=judge),
        patch.object(routes, "EvalEngine", new=lambda: engine),
    ):
        report = await _call(
            session,
            model_backend_id=_BACKEND_ID,
            eval_definition_id=_EVAL_ID,
        )

    assert report.ai_assisted is True
    assert report.eval_gate_passed is True
    assert report.eval_gate_verdict == "pass"
    assert report.provenance.mode == "ai_assisted"
    assert report.provenance.prompt_version == PROMPT_VERSION
    assert report.provenance.rubric_version == RUBRIC_VERSION
    assert report.provenance.evaluator_version == routes._DISCOVERY_EVALUATOR_VERSION
    assert report.provenance.selected_backend_id == _BACKEND_ID
    assert engine.calls[0][2] is judge
    assert hub.exited is True


@pytest.mark.parametrize(
    ("outcome", "verdict"),
    [
        (JudgeOutcome.FAIL, "fail"),
        (JudgeOutcome.ERROR, "error"),
    ],
)
async def test_discover_ai_eval_gate_failure_is_422(outcome: JudgeOutcome, verdict: str) -> None:
    backend = SimpleNamespace(id=_BACKEND_ID, owner_team_id=None, visibility="org")
    session = _inventory_session(backend_rows=[backend])
    hub = _Hub(backend_ids={_BACKEND_ID}, backend=MagicMock())
    engine = _EvalEngine(outcome)

    with (
        patch.object(routes, "evaluate_team_gate", new=AsyncMock(return_value=None)),
        patch.object(routes, "create_secrets_backend", return_value=MagicMock()),
        patch.object(routes, "ModelBackendHub", new=lambda: hub),
        patch.object(
            routes,
            "enhance_discovery_with_outcome",
            new=AsyncMock(return_value=(_draft(), "accepted")),
        ),
        patch.object(routes, "_resolve_discovery_eval", new=AsyncMock(return_value=_eval_definition())),
        patch.object(routes, "build_discovery_model_judge_callable", return_value=MagicMock()),
        patch.object(routes, "EvalEngine", new=lambda: engine),
        pytest.raises(HTTPException) as exc_info,
    ):
        await _call(
            session,
            model_backend_id=_BACKEND_ID,
            eval_definition_id=_EVAL_ID,
        )

    assert exc_info.value.status_code == 422
    assert _detail(exc_info.value)["code"] == "eval_gate_failed"
    assert _detail(exc_info.value)["verdict"] == verdict
    assert hub.exited is True
