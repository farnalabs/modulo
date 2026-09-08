"""Unit tests for modulo.cli.apply.executor (FAR-681 slice 1).

HTTP mocked via respx. Round-trip payloads are validated against the REAL
API pydantic models (contract round-trip), not hand-built response fixtures.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from modulo.api.routes.model_backends import ModelBackendCreate
from modulo.api.routes.schemas import SchemaCreate, SchemaVersionCreate
from modulo.cli.apply.executor import API_KEY_HEADER, ApplyExecutor, resolve_secret_refs
from modulo.cli.apply.loader import parse_apply_documents

CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: alpha
      description: Alpha schema
    - name: fresh
      description: Brand new
  model_backends:
    - name: openai
      display_name: OpenAI
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
      default_params:
        temperature: 0.5
"""


def _schema_item(name: str, description: str | None, schema_id: str | None = None) -> dict:
    return {
        "id": schema_id or str(uuid.uuid4()),
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "description": description,
        "abstract_name": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _backend_item(
    name: str,
    provider: str,
    *,
    display_name: str | None = None,
    model_id: str = "some-model",
    default_params: dict | None = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "display_name": display_name or name,
        "provider": provider,
        "model_id": model_id,
        "has_credentials": True,
        "default_params": default_params or {},
        "visibility": "org",
        "tier": "native",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _mock_current(
    schemas: list[dict],
    backends: list[dict],
    *,
    schema_post_json: dict | None = None,
    backend_post_json: dict | None = None,
) -> dict:
    """Mock the list/create endpoints; returns {name: route} for assertions."""
    routes = {}
    routes["schemas_get"] = respx.get(
        "https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}
    ).respond(json={"items": schemas, "total": len(schemas), "page": 1, "page_size": 100})
    routes["backends_get"] = respx.get(
        "https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}
    ).respond(json={"items": backends, "total": len(backends), "page": 1, "page_size": 100})
    routes["schemas_post"] = respx.post("https://api.test/api/v1/schemas").mock(
        return_value=httpx.Response(
            201,
            json=schema_post_json if schema_post_json is not None else _schema_item("applied", None),
        )
    )
    routes["backends_post"] = respx.post("https://api.test/api/v1/model-backends").mock(
        return_value=httpx.Response(
            201,
            json=backend_post_json if backend_post_json is not None else _backend_item("applied", "applied-provider"),
        )
    )
    return routes


class TestDryRunPlan:
    @respx.mock
    def test_dry_run_reports_all_outcomes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "secret-value")
        schemas = [_schema_item("alpha", "Alpha schema")]
        _mock_current(schemas, [])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        assert report["dry_run"] is True
        created = sorted(e["name"] for e in report["created"])
        unchanged = sorted(e["name"] for e in report["unchanged"])
        assert created == ["fresh", "openai"]
        assert unchanged == ["alpha"]
        assert not report["updated"]
        assert not report["blocked"]
        assert not report["failed"]

    @respx.mock
    def test_dry_run_makes_no_mutations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "secret-value")
        routes = _mock_current([], [])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            executor.run(config, dry_run=True)
        assert routes["schemas_post"].call_count == 0


class TestRealApply:
    @respx.mock
    def test_creates_schema_and_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        routes = _mock_current([], [])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        created_kinds = {e["kind"] for e in report["created"]}
        assert created_kinds == {"schema", "model_backend"}
        schema_payload = json.loads(routes["schemas_post"].calls.last.request.content)
        SchemaCreate.model_validate(schema_payload)
        backend_payload = json.loads(routes["backends_post"].calls.last.request.content)
        created = ModelBackendCreate.model_validate(backend_payload)
        assert created.api_key == "resolved-secret"
        assert not report["failed"]

    @respx.mock
    def test_created_then_unchanged_on_rerun(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        created_schema_fresh = _schema_item("fresh", "Brand new")
        created_schema_alpha = _schema_item("alpha", "Alpha schema")
        created_backend = _backend_item(
            "openai",
            "openai",
            display_name="OpenAI",
            model_id="gpt-x",
            default_params={"temperature": 0.5},
        )
        _mock_current([created_schema_alpha, created_schema_fresh], [created_backend])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        unchanged = sorted(e["name"] for e in report["unchanged"])
        assert unchanged == ["alpha", "fresh", "openai"]
        assert not report["created"]
        assert not report["updated"]
        assert not report["failed"]

    @respx.mock
    def test_patch_sent_when_fields_differ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        existing = _schema_item("alpha", "Old description")
        patch_route = respx.patch(f"https://api.test/api/v1/schemas/{existing['id']}").mock(
            return_value=httpx.Response(200, json={**existing, "description": "Alpha schema"})
        )
        _mock_current([existing], [])
        shortened = CONFIG_TEXT.replace("    - name: fresh\n      description: Brand new\n", "")
        config = parse_apply_documents(shortened)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert patch_route.call_count == 1
        patched_names = [e["name"] for e in report["updated"]]
        assert patched_names == ["alpha"]

    @respx.mock
    def test_provider_mismatch_blocks_apply(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        existing_backend = _backend_item("openai", "mistral")
        routes = _mock_current([], [existing_backend])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert routes["backends_post"].call_count == 0
        blocked_entries = [e for e in report["blocked"] if e["name"] == "openai"]
        assert blocked_entries[0]["reason"] is not None
        assert not report["failed"]


class TestRefResolution:
    @respx.mock
    def test_unresolved_env_ref_blocks_entity(self) -> None:
        _mock_current([], [])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked_entries = [e for e in report["blocked"] if e["name"] == "openai"]
        assert blocked_entries[0]["reason"] is not None
        assert "unresolved" in blocked_entries[0]["reason"]

    def test_resolve_secret_refs_env_passthrough(self) -> None:
        config = parse_apply_documents(CONFIG_TEXT)
        resolved, blocked = resolve_secret_refs(config, environ={"SK": "abc"})
        assert resolved == {"openai": "abc"}
        assert not blocked

    def test_resolve_secret_refs_missing_env(self) -> None:
        config = parse_apply_documents(CONFIG_TEXT)
        resolved, blocked = resolve_secret_refs(config, environ={})
        assert "openai" not in resolved
        assert blocked == [("model_backend", "openai", blocked[0][2])]


class TestVersionsCreate:
    @respx.mock
    def test_version_posted_on_schema_create(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "v")
        config_text = CONFIG_TEXT.replace("    - name: fresh\n      description: Brand new\n", "")
        config_text = config_text.replace(
            "    - name: alpha\n      description: Alpha schema\n",
            (
                "    - name: alpha\n"
                "      description: Alpha schema\n"
                "      versions:\n"
                "        - version: v1\n"
                "          version_number: 1\n"
                "          definition_json:\n"
                "            type: object\n"
            ),
        )
        schema_id = str(uuid.uuid4())
        config = parse_apply_documents(config_text)
        routes = _mock_current(
            [],
            [],
            schema_post_json=_schema_item("alpha", None, schema_id=schema_id),
        )
        version_post = respx.post(f"https://api.test/api/v1/schemas/{schema_id}/versions").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": str(uuid.uuid4()),
                    "organisation_id": str(uuid.uuid4()),
                    "schema_id": schema_id,
                    "version": "v1",
                    "version_number": 1,
                    "definition_json": {"type": "object"},
                    "published": False,
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            )
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert version_post.call_count == 1
        payload = json.loads(version_post.calls.last.request.content)
        SchemaVersionCreate.model_validate(payload)
        created_entries = [e["name"] for e in report["created"] if e["kind"] == "schema"]
        assert created_entries == ["alpha"]
        assert routes["backends_post"].call_count == 1


class TestContractRoundTrip:
    @respx.mock
    def test_backend_payload_validates_against_api_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        routes = _mock_current([], [])
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            executor.run(config, dry_run=False)
        request_body = routes["backends_post"].calls.last.request.content
        payload = json.loads(request_body)
        created = ModelBackendCreate.model_validate(payload)
        assert created.name == "openai"
        assert created.api_key == "resolved-secret"
        assert created.provider == "openai"
        assert created.tier == "native"


class TestHeaders:
    @respx.mock
    def test_api_key_header_sent(self) -> None:
        schemas_get = respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "secret-key", client=client)
            executor.fetch_current(parse_apply_documents(CONFIG_TEXT))
        assert schemas_get.calls.last.request.headers[API_KEY_HEADER.lower()] == "secret-key"


GOLDEN_CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: stable
      description: Stable schema
    - name: fresh
      description: To be created
  model_backends:
    - name: openai
      display_name: OpenAI
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
      default_params:
        temperature: 0.5
"""

_STABLE_CURRENT = {
    "name": "stable",
    "description": "Stable schema",
    "abstract_name": None,
    "versions": [],
}


def _golden_plan_report(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Deterministic plan report for the golden fixture config.

    'stable' exists with identical managed fields -> unchanged; 'fresh' and
    the backend are absent -> created.
    """
    monkeypatch.setenv("SK", "golden-secret")
    with respx.mock:
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [_STABLE_CURRENT], "total": 1, "page": 1, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        config = parse_apply_documents(GOLDEN_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            return executor.run(config, dry_run=True)


class TestGoldenSnapshot:
    def test_plan_snapshot_matches_golden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = _golden_plan_report(monkeypatch)
        golden_path = Path(__file__).resolve().parent / "golden" / "apply_plan_snapshot.json"
        golden = json.loads(golden_path.read_text(encoding="utf-8"))
        assert report == golden

    def test_rerun_reports_all_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "golden-secret")
        # Current state as it would look after a previous successful apply.
        fresh_current = {
            "name": "fresh",
            "description": "To be created",
            "abstract_name": None,
            "versions": [],
        }
        created_state = _backend_item(
            "openai",
            "openai",
            display_name="OpenAI",
            model_id="gpt-x",
            default_params={"temperature": 0.5},
        )
        with respx.mock:
            respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
                json={"items": [_STABLE_CURRENT, fresh_current], "total": 2, "page": 1, "page_size": 100}
            )
            respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
                json={"items": [created_state], "total": 1, "page": 1, "page_size": 100}
            )
            config = parse_apply_documents(GOLDEN_CONFIG_TEXT)
            with httpx.Client() as client:
                executor = ApplyExecutor("https://api.test", "key", client=client)
                report = executor.run(config, dry_run=True)
        unchanged_names = sorted(e["name"] for e in report["unchanged"])
        assert unchanged_names == ["fresh", "openai", "stable"]
        assert not report["created"]
        assert not report["updated"]
        assert not report["blocked"]
        assert not report["failed"]
