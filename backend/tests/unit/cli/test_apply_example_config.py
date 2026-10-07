"""FAR-1531: the canonical ``configs/apply/example.yaml`` must apply cleanly.

One artefact, three assertions:

1. STRUCTURAL — the example loads, declares every apply kind (schemas,
   model_backends, pipelines with an agent NAME-ref, triggers), keeps every
   pipeline Paused (``run_enabled: false``, false-only FAR-1530) and
   references secrets ONLY via ``${env:...}`` refs (inline literals are
   rejected at load).
2. CONVERGENCE — a full ``apply`` against a STATEFUL fake org creates every
   entity and pauses every pipeline, then a second ``--diff`` (drift) run
   reports all-unchanged with ZERO write requests. That is the same property
   the ``staging-apply`` deploy gate asserts end-to-end against staging.
3. WIRING — ``.github/workflows/deploy.yml`` still carries the
   ``staging-apply`` job as a sibling of ``staging-e2e``
   (``needs: [deploy-staging]``) and ``deploy-production`` still gates on it,
   so the gate cannot be silently un-wired.

The fake org echoes what apply POSTs/PATCHes (the way a converged server
would), so a create-then-read-back round trip through the real managed-view
hashes is what proves convergence — not a hand-built fixture.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import yaml

from modulo.cli.apply.drift import has_drift
from modulo.cli.apply.executor import ApplyExecutor, resolve_secret_refs
from modulo.cli.apply.loader import load_apply_file
from modulo.cli.apply.models import ApplyConfig
from modulo.cli.apply.plan import KIND_BACKEND, KIND_PIPELINE, KIND_SCHEMA, KIND_TRIGGER

REPO_ROOT = Path(__file__).resolve().parents[4]
EXAMPLE_PATH = REPO_ROOT / "configs" / "apply" / "example.yaml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

_API = "https://staging.test"
_BASE = f"{_API}/api/v1"
_ORG = "00000000-0000-4000-8000-0000000000ff"
_TIMESTAMP = "2026-01-01T00:00:00Z"

MODEL_KEY_VAR = "APPLY_EXAMPLE_MODEL_API_KEY"
MOCK_HMAC_VAR = "MOCK_APPLY_EXAMPLE_WEBHOOK_HMAC"

EXPECTED_CREATED = {
    (KIND_SCHEMA, "apply-example-ticket"),
    (KIND_SCHEMA, "apply-example-summary"),
    (KIND_BACKEND, "apply-example-chat"),
    (KIND_PIPELINE, "apply-example-graphed"),
    (KIND_PIPELINE, "apply-example-graphless"),
    (KIND_TRIGGER, "apply-example-graphed/nightly"),
    (KIND_TRIGGER, "apply-example-graphed/ingest"),
}


def _load_example() -> ApplyConfig:
    return load_apply_file(EXAMPLE_PATH)


def _body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content.decode())


def _page(items: list[dict[str, Any]]) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "total": len(items), "page": 1, "page_size": 100})


class _FakeOrg:
    """Stateful fake of the apply-facing API surface.

    Stores whatever apply writes and serves it back on the list/read
    endpoints, so a second run sees exactly the org the first run produced.
    """

    def __init__(self) -> None:
        self.schemas: dict[str, dict[str, Any]] = {}
        self.backends: dict[str, dict[str, Any]] = {}
        self.pipelines: dict[str, dict[str, Any]] = {}
        self.triggers: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, respx.Route] = {}

    # -- helpers ---------------------------------------------------------
    def _pipeline_by_id(self, pipeline_id: str) -> dict[str, Any]:
        for row in self.pipelines.values():
            if row["id"] == pipeline_id:
                return row
        raise KeyError(pipeline_id)

    def _pipeline_id_by_name(self, pipeline_name: str) -> str:
        return str(self.pipelines[pipeline_name]["id"])

    def install(self) -> dict[str, respx.Route]:
        routes = {
            "schemas_get": respx.get(f"{_BASE}/schemas").mock(side_effect=self.list_schemas),
            "schemas_post": respx.post(f"{_BASE}/schemas").mock(side_effect=self.create_schema),
            "versions_get": respx.get(re.compile(rf"{re.escape(_BASE)}/schemas/[^/]+/versions")).mock(
                side_effect=self.list_versions
            ),
            "versions_post": respx.post(re.compile(rf"{re.escape(_BASE)}/schemas/[^/]+/versions")).mock(
                side_effect=self.create_version
            ),
            "backends_get": respx.get(f"{_BASE}/model-backends").mock(side_effect=self.list_backends),
            "backends_post": respx.post(f"{_BASE}/model-backends").mock(side_effect=self.create_backend),
            "health_post": respx.post(re.compile(rf"{re.escape(_BASE)}/model-backends/[^/]+/health-check")).mock(
                side_effect=self.health_check
            ),
            "pipelines_get": respx.get(f"{_BASE}/pipelines").mock(side_effect=self.list_pipelines),
            "pipelines_post": respx.post(f"{_BASE}/pipelines").mock(side_effect=self.create_pipeline),
            "pipeline_patch": respx.patch(re.compile(rf"{re.escape(_BASE)}/pipelines/[^/]+")).mock(
                side_effect=self.patch_pipeline
            ),
            "pause_post": respx.post(re.compile(rf"{re.escape(_BASE)}/pipelines/[^/]+/pause")).mock(
                side_effect=self.pause_pipeline
            ),
            "graph_get": respx.get(re.compile(rf"{re.escape(_BASE)}/pipelines/[^/]+/graph")).mock(
                side_effect=self.get_graph
            ),
            "trigger_post": respx.post(re.compile(rf"{re.escape(_BASE)}/pipelines/[^/]+/triggers")).mock(
                side_effect=self.create_trigger
            ),
            "triggers_get": respx.get(f"{_BASE}/triggers").mock(side_effect=self.list_triggers),
            "agents_get": respx.get(f"{_BASE}/agents").mock(side_effect=self.list_agents),
        }
        self.routes = routes
        return routes

    # -- schemas ---------------------------------------------------------
    def list_schemas(self, request: httpx.Request) -> httpx.Response:
        return _page([dict(row) for row in self.schemas.values()])

    def create_schema(self, request: httpx.Request) -> httpx.Response:
        payload = _body(request)
        row = {
            "id": str(uuid.uuid4()),
            "organisation_id": _ORG,
            "name": payload["name"],
            "description": payload.get("description"),
            "abstract_name": payload.get("abstract_name"),
            "versions": [],
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
        }
        self.schemas[row["name"]] = row
        return httpx.Response(201, json=row)

    def _schema_by_id(self, schema_id: str) -> dict[str, Any]:
        for row in self.schemas.values():
            if row["id"] == schema_id:
                return row
        raise KeyError(schema_id)

    def list_versions(self, request: httpx.Request) -> httpx.Response:
        schema_id = request.url.path.rstrip("/").split("/")[-2]
        versions = self._schema_by_id(schema_id)["versions"]
        return _page([dict(v) for v in versions])

    def create_version(self, request: httpx.Request) -> httpx.Response:
        schema_id = request.url.path.rstrip("/").split("/")[-2]
        payload = _body(request)
        self._schema_by_id(schema_id)["versions"].append(payload)
        return httpx.Response(201, json={"id": str(uuid.uuid4()), **payload})

    # -- model backends --------------------------------------------------
    def list_backends(self, request: httpx.Request) -> httpx.Response:
        return _page([dict(row) for row in self.backends.values()])

    def create_backend(self, request: httpx.Request) -> httpx.Response:
        payload = _body(request)
        # The API never returns the stored api_key — only a has_credentials flag.
        row = {
            "id": str(uuid.uuid4()),
            "organisation_id": _ORG,
            "name": payload["name"],
            "display_name": payload["display_name"],
            "provider": payload["provider"],
            "model_id": payload["model_id"],
            "has_credentials": True,
            "default_params": payload.get("default_params") or {},
            "visibility": payload.get("visibility", "org"),
            "tier": payload.get("tier", "native"),
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
        }
        self.backends[row["name"]] = row
        return httpx.Response(201, json=row)

    def health_check(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "detail": None, "checked_at": _TIMESTAMP})

    # -- pipelines -------------------------------------------------------
    def list_pipelines(self, request: httpx.Request) -> httpx.Response:
        return _page([dict(row) for row in self.pipelines.values()])

    def create_pipeline(self, request: httpx.Request) -> httpx.Response:
        payload = _body(request)
        row: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "organisation_id": _ORG,
            "name": payload["name"],
            "description": payload.get("description"),
            "max_concurrent_runs": payload.get("max_concurrent_runs", 5),
            "stdout_retention_config": payload.get("stdout_retention_config"),
            "hitl_review_window_seconds": payload.get("hitl_review_window_seconds"),
            "circuit_breaker_threshold": payload.get("circuit_breaker_threshold"),
            "business_owner_id": payload.get("business_owner_id"),
            "reliability_owner_id": payload.get("reliability_owner_id"),
            # A freshly created pipeline runs until someone pauses it; the
            # corpus declares run_enabled: false, so apply must POST /pause.
            "run_enabled": True,
            "run_disabled_reason": None,
            "graph": None,
            "created_at": _TIMESTAMP,
            "updated_at": _TIMESTAMP,
        }
        self.pipelines[row["name"]] = row
        return httpx.Response(201, json=row)

    def patch_pipeline(self, request: httpx.Request) -> httpx.Response:
        pipeline_id = request.url.path.rstrip("/").split("/")[-1]
        row = self._pipeline_by_id(pipeline_id)
        payload = _body(request)
        for key, value in payload.items():
            if key == "graph_json":
                row["graph"] = value
            else:
                row[key] = value
        row["updated_at"] = _TIMESTAMP
        return httpx.Response(200, json=row)

    def pause_pipeline(self, request: httpx.Request) -> httpx.Response:
        pipeline_id = request.url.path.rstrip("/").split("/")[-2]
        row = self._pipeline_by_id(pipeline_id)
        row["run_enabled"] = False
        row["run_disabled_reason"] = "operator"
        return httpx.Response(200, json=row)

    def get_graph(self, request: httpx.Request) -> httpx.Response:
        pipeline_id = request.url.path.rstrip("/").split("/")[-2]
        graph = self._pipeline_by_id(pipeline_id)["graph"] or {"nodes": [], "edges": []}
        return httpx.Response(200, json={**graph, "validation_issues": []})

    # -- agents / triggers -----------------------------------------------
    def list_agents(self, request: httpx.Request) -> httpx.Response:
        # apply never creates agents: the example's agent NAME-refs must
        # resolve against pre-provisioned agents (PREREQUISITES preamble).
        return _page(
            [
                {"id": "00000000-0000-4000-8000-0000000000a1", "organisation_id": _ORG, "name": "apply-example-worker"},
                {
                    "id": "00000000-0000-4000-8000-0000000000a2",
                    "organisation_id": _ORG,
                    "name": "apply-example-reviewer",
                },
            ]
        )

    def create_trigger(self, request: httpx.Request) -> httpx.Response:
        pipeline_id = request.url.path.rstrip("/").split("/")[-2]
        pipeline_name = next(name for name, row in self.pipelines.items() if row["id"] == pipeline_id)
        payload = _body(request)
        row = {"id": str(uuid.uuid4()), "pipeline_id": pipeline_id, **payload}
        self.triggers[f"{pipeline_name}/{payload['name']}"] = row
        return httpx.Response(201, json=row)

    def list_triggers(self, request: httpx.Request) -> httpx.Response:
        return _page([dict(row) for row in self.triggers.values()])


def _run(config: ApplyConfig, *, drift: bool = False) -> dict[str, Any]:
    with httpx.Client() as client:
        executor = ApplyExecutor(_API, "mk_test_key", client=client)
        try:
            return executor.run(config, dry_run=False, drift=drift)
        finally:
            executor.close()


# ---------------------------------------------------------------------------
# 1. Structural contract
# ---------------------------------------------------------------------------


class TestExampleStructure:
    def test_example_file_exists(self) -> None:
        assert EXAMPLE_PATH.is_file(), f"missing canonical apply example at {EXAMPLE_PATH}"

    def test_example_loads_with_every_apply_kind(self) -> None:
        config = _load_example()
        assert config.api_version == "modulo.dev/v1"
        assert [s.name for s in config.entities.schemas] == ["apply-example-ticket", "apply-example-summary"]
        assert [b.name for b in config.entities.model_backends] == ["apply-example-chat"]
        assert [p.name for p in config.entities.pipelines] == ["apply-example-graphed", "apply-example-graphless"]
        assert [t.display_key() for t in config.entities.triggers] == [
            "apply-example-graphed/nightly",
            "apply-example-graphed/ingest",
        ]

    def test_every_pipeline_is_declared_paused(self) -> None:
        """The corpus must never express a running pipeline: run_enabled is
        false-only (FAR-1530) and the staging gate relies on the pause to
        guarantee nothing executes on the target org."""
        config = _load_example()
        assert config.entities.pipelines
        for pipeline in config.entities.pipelines:
            assert pipeline.manages_run_enabled is True, pipeline.name
            assert pipeline.run_enabled is False, pipeline.name

    def test_graph_references_an_agent_by_name(self) -> None:
        """The example must exercise the agent NAME-ref surface (apply never
        creates agents — see the PREREQUISITES preamble in the file)."""
        config = _load_example()
        graphed = next(p for p in config.entities.pipelines if p.name == "apply-example-graphed")
        assert graphed.graph is not None
        agent_refs = [node.agent for node in graphed.graph.nodes if node.agent is not None]
        assert agent_refs == ["apply-example-worker", "apply-example-reviewer"]

    def test_secrets_are_env_refs_that_block_when_unset(self) -> None:
        config = _load_example()
        backend = config.entities.model_backends[0]
        assert backend.api_key == "${env:APPLY_EXAMPLE_MODEL_API_KEY}"
        # Unset env -> the referencing entity is BLOCKED, never written raw.
        _keys, blocked = resolve_secret_refs(config, environ={})
        assert blocked
        assert blocked[0][1] == "apply-example-chat"
        assert "is not set" in blocked[0][2]
        # Set env -> resolved, nothing blocked.
        keys, blocked = resolve_secret_refs(
            config,
            environ={MODEL_KEY_VAR: "sk-test", MOCK_HMAC_VAR: "mock-hmac"},
        )
        assert not blocked
        assert keys["apply-example-chat"] == "sk-test"


# ---------------------------------------------------------------------------
# 2. Apply + converge against a stateful fake org
# ---------------------------------------------------------------------------


@respx.mock
def test_example_applies_cleanly_then_converges(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the example, then prove a second (drift) run is a no-op.

    FAILS without the example being valid: a blocked entity (missing agent,
    bad graph, unresolvable env ref) lands in ``blocked`` and the first run
    reports it. FAILS on non-convergence: any managed field the server
    normalises differently from the way the config writes it reappears as
    ``created``/``updated`` on the second run and ``has_drift`` is True.
    """
    monkeypatch.setenv(MODEL_KEY_VAR, "sk-test-low-cost-key")
    monkeypatch.setenv(MOCK_HMAC_VAR, "mock-hmac-value")
    config = _load_example()
    org = _FakeOrg()
    routes = org.install()

    first = _run(config)

    assert not first["blocked"], first["blocked"]
    assert not first["failed"], first["failed"]
    created = {(entry["kind"], entry["name"]) for entry in first["created"]}
    assert created == EXPECTED_CREATED
    # The Paused write path fired for every pipeline (POST /pause, not a
    # PATCHed run_enabled and not an inactive trigger).
    assert routes["pause_post"].call_count == len(config.entities.pipelines)
    for row in org.pipelines.values():
        assert row["run_enabled"] is False, row["name"]
        assert row["run_disabled_reason"] == "operator"
    # The agent NAME-ref resolved (provisioned agent), and the graph landed.
    assert org.pipelines["apply-example-graphed"]["graph"] is not None
    # Schema version + webhook secret landed server-side.
    assert [v["version"] for v in org.schemas["apply-example-ticket"]["versions"]] == ["v1"]
    assert org.triggers["apply-example-graphed/ingest"]["config_json"]["hmac_secret"] == "mock-hmac-value"

    mutating = (
        "schemas_post",
        "versions_post",
        "backends_post",
        "pipelines_post",
        "pipeline_patch",
        "pause_post",
        "trigger_post",
    )
    before = {name: routes[name].call_count for name in mutating}

    second = _run(config, drift=True)

    assert second.get("mode") == "drift"
    assert not has_drift(second), second
    unchanged = {(entry["kind"], entry["name"]) for entry in second["unchanged"]}
    assert unchanged == EXPECTED_CREATED
    after = {name: routes[name].call_count for name in mutating}
    assert after == before, "drift mode must never write"


# ---------------------------------------------------------------------------
# 3. Deploy-gate wiring
# ---------------------------------------------------------------------------


def _deploy_workflow() -> dict[str, Any]:
    assert DEPLOY_WORKFLOW.is_file(), f"missing {DEPLOY_WORKFLOW}"
    loaded = yaml.safe_load(DEPLOY_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    jobs = loaded.get("jobs")
    assert isinstance(jobs, dict), "deploy.yml has no jobs mapping"
    return loaded


class TestDeployGateWiring:
    def test_gate_job_is_a_sibling_of_staging_e2e(self) -> None:
        jobs = _deploy_workflow()["jobs"]
        assert "staging-apply" in jobs, "the staging apply gate job is missing from deploy.yml"
        needs = jobs["staging-apply"]["needs"]
        assert "deploy-staging" in needs, f"staging-apply must need deploy-staging, got {needs}"

    def test_production_gates_on_the_apply_job(self) -> None:
        needs = _deploy_workflow()["jobs"]["deploy-production"]["needs"]
        assert "staging-apply" in needs, f"deploy-production must gate on staging-apply, got {needs}"
        assert "staging-e2e" in needs

    def test_gate_runs_the_canonical_example_twice(self) -> None:
        """`apply` then `apply --diff` (the idempotency assertion) against
        this exact file — one artefact, CI-proven."""
        text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
        assert "configs/apply/example.yaml" in text
        assert re.search(r"modulo apply[^\n]*--diff", text), "the gate must assert convergence with --diff"
