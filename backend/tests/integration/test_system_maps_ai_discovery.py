"""Real-Postgres integration coverage for AI-assisted System Maps discovery."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.api.dependencies import _get_engine, get_db_session, get_plan_context, get_settings
from modulo.api.main import app
from modulo.auth.dependencies import get_current_tenant_user, get_current_tenant_user_or_api_key, get_current_user
from modulo.auth.jwt import AuthenticatedPrincipal, TenantPrincipal
from modulo.core.eval_engine import EvalDefinition, EvalEngine, EvalType
from modulo.core.eval_engine.typed_judge.config import typed_config_questions
from modulo.core.eval_engine.typed_judge.envelope import build_question_prompt
from modulo.core.system_maps_ai_discovery import build_discovery_prompt
from modulo.core.system_maps_discovery import DiscoverySource, discover_draft
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_folder import PipelineFolder
from modulo.db.models.trigger import Trigger
from modulo.model_backends.base import ModelBackendBase
from modulo.model_backends.stub.backend import StubModelBackend, normalize_input
from modulo.settings import Settings

pytestmark = pytest.mark.integration

_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class _AllFeatures:
    def feature_enabled(self, _name: str) -> bool:
        return True

    def list_enabled_features(self) -> list[str]:
        return []

    def list_all_features(self) -> list[str]:
        return []

    def tier(self) -> str:
        return "enterprise"

    def has_license_key(self) -> bool:
        return True


class _StubAdapter(ModelBackendBase):
    def __init__(self, fixtures: dict[str, str]) -> None:
        self._inner = StubModelBackend(fixtures)

    async def invoke(
        self,
        messages: list[BaseMessage],
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> BaseMessage:
        return await self._inner.ainvoke(messages, **kwargs)

    def stream(
        self,
        messages: list[BaseMessage],
        tools: list[dict[str, Any]] | None = None,
        output_schema: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        return self._inner.astream(messages, tools=tools, **kwargs)

    @property
    def backend_id(self) -> str:
        return "custom/stub"


async def _inventory_draft(session: AsyncSession, org_id: uuid.UUID) -> Any:
    pipeline_result = await session.execute(
        select(Pipeline).where(
            Pipeline.organisation_id == org_id,
            Pipeline.deleted_at.is_(None),
            Pipeline.archived_at.is_(None),
        )
    )
    pipelines = list(pipeline_result.scalars())
    trigger_result = await session.execute(
        select(Trigger).where(Trigger.organisation_id == org_id, Trigger.deleted_at.is_(None))
    )
    triggers = list(trigger_result.scalars())
    connector_result = await session.execute(
        select(ConnectorInstance).where(
            ConnectorInstance.organisation_id == org_id,
            ConnectorInstance.deleted_at.is_(None),
        )
    )
    connectors = list(connector_result.scalars())
    folder_result = await session.execute(select(PipelineFolder).where(PipelineFolder.organisation_id == org_id))
    folders = list(folder_result.scalars())
    sources = [
        *[
            DiscoverySource(
                kind="pipeline",
                identifier=str(row.id),
                name=row.name,
                folder_id=str(row.folder_id) if row.folder_id else None,
                explicit_system_map_tag=bool(row.run_context_defaults.get("system_map_tag")),
            )
            for row in pipelines
        ],
        *[
            DiscoverySource(
                kind="trigger",
                identifier=str(row.id),
                name=row.name,
                pipeline_id=str(row.pipeline_id),
                trigger_type=row.trigger_type,
            )
            for row in triggers
        ],
        *[
            DiscoverySource(
                kind="connector",
                identifier=str(row.id),
                name=row.name,
                connector_type_id=row.connector_type_id,
            )
            for row in connectors
        ],
        *[DiscoverySource(kind="folder", identifier=str(row.id), name=row.name) for row in folders],
    ]
    return discover_draft(sources)


async def _insert_backend(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    fixtures: dict[str, str],
    status: str = "active",
) -> uuid.UUID:
    backend_id = uuid.uuid4()
    ciphertext = Fernet(_FERNET_KEY.encode()).encrypt(b"unused")
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO model_backends (id, organisation_id, name, display_name, provider, model_id, "
                "default_params, visibility, credentials_ciphertext, status, account_id, tier) "
                "VALUES (:id, :oid, :name, :name, 'custom', 'system-map-test', CAST(:params AS jsonb), "
                "'org', :credentials, :status, :aid, 'native')"
            ),
            {
                "id": str(backend_id),
                "oid": str(org_id),
                "name": f"System Map Backend {backend_id.hex[:8]}",
                "params": json.dumps({"fixture_map": fixtures}),
                "credentials": ciphertext,
                "status": status,
                "aid": str(account_id),
            },
        )
    return backend_id


async def _insert_eval(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    eval_type: str = "model_judge",
    config: dict[str, Any] | None = None,
) -> uuid.UUID:
    eval_id = uuid.uuid4()
    effective_config = config or {
        "answer_contract": "typed_questions",
        "questions": [{"id": "grounded", "type": "noul", "weight": 1.0}],
        "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
    }
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, name, eval_type, config_json, account_id) "
                "VALUES (:id, :oid, :pid, :name, :type, CAST(:config AS jsonb), :aid)"
            ),
            {
                "id": str(eval_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "name": f"System Map Judge {eval_id.hex[:8]}",
                "type": eval_type,
                "config": json.dumps(effective_config),
                "aid": str(account_id),
            },
        )
    return eval_id


@pytest_asyncio.fixture
async def discovery_client(
    db_url: str,
    app_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> AsyncGenerator[AsyncClient, None]:
    settings = Settings(
        database_url=db_url,
        modulo_db="postgres",
        secret_key="a" * 32,
        fernet_key=_FERNET_KEY,
        redis_url="",
        modulo_admin_password="",
        modulo_dev_mode=True,
        modulo_csrf_enabled=False,
        modulo_auth_rate_limit_enabled=False,
    )
    principal = TenantPrincipal(
        username="integration-user",
        organisation_id=test_org,
        account_id=test_user,
        org_role="admin",
    )
    authenticated = AuthenticatedPrincipal(
        username="integration-user",
        organisation_id=test_org,
        account_id=test_user,
        org_role="admin",
        via_api_key=False,
        client_kind="browser",
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        factory = async_sessionmaker(app_engine, expire_on_commit=False, autobegin=False)
        async with factory() as session:
            yield session

    async def override_principal() -> TenantPrincipal:
        return principal

    async def override_user() -> AuthenticatedPrincipal:
        return authenticated

    async def override_features() -> _AllFeatures:
        return _AllFeatures()

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[_get_engine] = lambda: app_engine
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_plan_context] = override_features
    app.dependency_overrides[get_current_tenant_user] = override_principal
    app.dependency_overrides[get_current_tenant_user_or_api_key] = override_principal
    app.dependency_overrides[get_current_user] = override_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=30.0) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


async def _assert_persisted_draft(db_engine: AsyncEngine, map_id: str) -> dict[str, Any]:
    async with db_engine.connect() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT sm.published_revision_id, sr.status, sr.content_json "
                        "FROM system_maps sm JOIN system_map_revisions sr ON sr.id = sm.published_revision_id "
                        "WHERE sm.id = :id"
                    ),
                    {"id": map_id},
                )
            )
            .mappings()
            .first()
        )
        if row is not None:
            return dict(row)
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT sm.published_revision_id, sr.status, sr.content_json "
                        "FROM system_maps sm JOIN system_map_revisions sr ON sr.system_map_id = sm.id "
                        "WHERE sm.id = :id ORDER BY sr.revision_number DESC LIMIT 1"
                    ),
                    {"id": map_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


@pytest.mark.asyncio
async def test_deterministic_request_is_unchanged_and_never_published(
    discovery_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
) -> None:
    response = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"Deterministic {uuid.uuid4().hex}"},
    )

    assert response.status_code == 201
    report = response.json()
    assert report["ai_assisted"] is False
    assert report["provenance"]["mode"] == "deterministic"
    assert report["published"] is False
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        expected = await _inventory_draft(session, test_org)
    assert report["inferred_nodes"] == list(expected.nodes)
    assert report["inferred_edges"] == list(expected.relationships)
    persisted = await _assert_persisted_draft(db_engine, report["draft_map_id"])
    assert persisted["published_revision_id"] is None
    assert persisted["status"] == "draft"


@pytest.mark.asyncio
async def test_ai_request_returns_provenance_and_selected_backend_id(
    discovery_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        draft = await _inventory_draft(session, test_org)
    ai_response = json.dumps(
        {
            "additional_nodes": [],
            "additional_edges": [],
            "confidence": 0.99,
            "reasoning": "No safe additions.",
        }
    )
    fixtures = {normalize_input(build_discovery_prompt(draft, safe_additions=())): ai_response}
    backend_id = await _insert_backend(db_engine, org_id=test_org, account_id=test_user, fixtures=fixtures)

    response = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"AI {uuid.uuid4().hex}", "model_backend_id": str(backend_id)},
    )

    assert response.status_code == 201
    report = response.json()
    assert report["ai_assisted"] is True
    assert report["provenance"]["mode"] == "ai_assisted"
    assert report["provenance"]["selected_backend_id"] == str(backend_id)
    assert report["provenance"]["prompt_version"]
    assert report["provenance"]["rubric_version"]
    assert report["published"] is False


@pytest.mark.asyncio
async def test_unavailable_or_unauthorised_backend_fails_closed_without_a_draft(
    discovery_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    unavailable_id = await _insert_backend(
        db_engine,
        org_id=test_org,
        account_id=test_user,
        fixtures={},
        status="disabled",
    )
    unavailable = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"Unavailable {uuid.uuid4().hex}", "model_backend_id": str(unavailable_id)},
    )
    assert unavailable.status_code == 403
    assert unavailable.json()["detail"] == str(
        {
            "code": "model_backend_unauthorised",
            "message": "Selected model backend is not active and authorised for this organisation",
        }
    )

    foreign_org = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(foreign_org), "name": "Foreign AI Org", "slug": f"foreign-{foreign_org.hex[:8]}"},
        )
    foreign_backend = await _insert_backend(db_engine, org_id=foreign_org, account_id=test_user, fixtures={})
    unauthorised = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"Foreign {uuid.uuid4().hex}", "model_backend_id": str(foreign_backend)},
    )
    assert unauthorised.status_code == 403
    assert unauthorised.json()["detail"] == str(
        {
            "code": "model_backend_unauthorised",
            "message": "Selected model backend is not active and authorised for this organisation",
        }
    )


@pytest.mark.asyncio
async def test_eval_definition_must_be_same_org_model_judge(
    discovery_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        draft = await _inventory_draft(session, test_org)
    ai_response = json.dumps(
        {
            "additional_nodes": [],
            "additional_edges": [],
            "confidence": 0.99,
            "reasoning": "No safe additions.",
        }
    )
    fixtures = {normalize_input(build_discovery_prompt(draft, safe_additions=())): ai_response}
    backend_id = await _insert_backend(db_engine, org_id=test_org, account_id=test_user, fixtures=fixtures)
    wrong_type = await _insert_eval(
        db_engine,
        org_id=test_org,
        account_id=test_user,
        pipeline_id=test_pipeline,
        eval_type="regex",
        config={"pattern": "x"},
    )

    response = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={
            "name": f"Wrong eval {uuid.uuid4().hex}",
            "model_backend_id": str(backend_id),
            "eval_definition_id": str(wrong_type),
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == str(
        {
            "code": "invalid_eval_definition",
            "message": "Eval gate must be an active org-owned model_judge definition",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected_status", "expected_verdict"),
    [(True, 201, "pass"), (False, 422, "fail"), (None, 422, "error")],
    ids=["pass-accepts", "fail-blocks", "error-is-distinct"],
)
async def test_eval_gate_controls_ai_persistence_and_does_not_leak_candidates(
    discovery_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
    test_pipeline: uuid.UUID,
    answer: bool | None,
    expected_status: int,
    expected_verdict: str,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        draft = await _inventory_draft(session, test_org)
    ai_response = json.dumps(
        {
            "additional_nodes": [],
            "additional_edges": [],
            "confidence": 0.99,
            "reasoning": "No safe additions.",
        }
    )
    judge_response = (
        "not-json"
        if answer is None
        else json.dumps(
            {
                "answer": answer,
                "probabilities": {"yes": 1.0 if answer else 0.0, "no": 0.0 if answer else 1.0},
                "detail": "integration verdict",
            }
        )
    )
    eval_config = {
        "answer_contract": "typed_questions",
        "field": "candidate",
        "judge_whole_output": True,
        "questions": [{"id": "grounded", "type": "noul", "weight": 1.0}],
        "pass_rule": {"kind": "weighted_score_at_least", "threshold": 0.5},
    }
    candidate = {"candidate": {"nodes": list(draft.nodes), "relationships": list(draft.relationships)}}
    question = typed_config_questions(eval_config)[0]
    safe_eval = EvalDefinition(
        id=uuid.uuid4(),
        org_id=test_org,
        pipeline_id=test_pipeline,
        node_id=None,
        name="fixture",
        eval_type=EvalType.MODEL_JUDGE,
        config=eval_config,
        failure_behaviour="block",
    )
    _, safe_eval_definition = EvalEngine._build_safe_judge_input(candidate, safe_eval)
    judge_content = str(safe_eval_definition.config["_judge_content"])
    fixtures = {
        normalize_input(build_discovery_prompt(draft, safe_additions=())): ai_response,
        normalize_input([HumanMessage(content=build_question_prompt(question, judge_content))]): judge_response,
    }
    backend_id = await _insert_backend(db_engine, org_id=test_org, account_id=test_user, fixtures=fixtures)
    eval_id = await _insert_eval(
        db_engine,
        org_id=test_org,
        account_id=test_user,
        pipeline_id=test_pipeline,
        config=eval_config,
    )
    name = f"Gated {uuid.uuid4().hex}"

    response = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={"name": name, "model_backend_id": str(backend_id), "eval_definition_id": str(eval_id)},
    )

    assert response.status_code == expected_status
    if expected_status == 201:
        report = response.json()
        assert report["eval_gate_passed"] is True
        assert report["eval_gate_verdict"] == expected_verdict
        persisted = await _assert_persisted_draft(db_engine, report["draft_map_id"])
        assert persisted["published_revision_id"] is None
    else:
        assert response.json()["detail"] == str(
            {
                "code": "eval_gate_failed",
                "message": "AI-assisted discovery did not pass its evaluation gate",
                "verdict": expected_verdict,
            }
        )
        async with db_engine.connect() as conn:
            count = (
                await conn.execute(
                    text("SELECT count(*) FROM system_maps WHERE organisation_id = :oid AND name = :name"),
                    {"oid": str(test_org), "name": name},
                )
            ).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_ai_requires_a_valid_backend_response_instead_of_deterministic_fallback(
    discovery_client: AsyncClient,
    db_engine: AsyncEngine,
    test_org: uuid.UUID,
    test_user: uuid.UUID,
) -> None:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        draft = await _inventory_draft(session, test_org)
    fixtures = {normalize_input(build_discovery_prompt(draft, safe_additions=())): json.dumps({"bad": "shape"})}
    backend_id = await _insert_backend(db_engine, org_id=test_org, account_id=test_user, fixtures=fixtures)

    response = await discovery_client.post(
        "/api/v1/system-maps/discover",
        json={"name": f"Invalid AI {uuid.uuid4().hex}", "model_backend_id": str(backend_id)},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == str(
        {
            "code": "model_backend_invalid_response",
            "message": "AI discovery backend returned no accepted enhancement",
        }
    )
