"""Unit tests for modulo.cli.apply.executor (FAR-681 slice 1).

HTTP mocked via respx. Round-trip payloads are validated against the REAL
API pydantic models (contract round-trip), not hand-built response fixtures.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from modulo.api.routes.model_backends import ModelBackendCreate
from modulo.api.routes.schemas import SchemaCreate, SchemaVersionCreate
from modulo.cli.apply.executor import (
    AUTH_HEADER,
    PAGE_SIZE,
    ApplyExecutor,
    ApplyHttpError,
    resolve_secret_refs,
)
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

_BACKENDS_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE), "include_in_dev": "true"}


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
    versions_json: list[dict] | None = None,
) -> dict:
    """Mock the list/create endpoints; returns {name: route} for assertions."""
    routes = {}
    routes["schemas_get"] = respx.get(
        "https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}
    ).respond(json={"items": schemas, "total": len(schemas), "page": 1, "page_size": 100})
    routes["backends_get"] = respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
        json={"items": backends, "total": len(backends), "page": 1, "page_size": 100}
    )
    routes["versions_get"] = respx.get(
        re.compile(r"https://api\.test/api/v1/schemas/[^/]+/versions"),
        params={"page": "1", "page_size": str(PAGE_SIZE)},
    ).mock(
        return_value=httpx.Response(
            200,
            json={"items": list(versions_json or []), "total": len(versions_json or [])},
        )
    )
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
    routes["health_post"] = respx.post(re.compile(r"https://api\.test/api/v1/model-backends/[^/]+/health-check")).mock(
        return_value=httpx.Response(200, json={"status": "ok", "detail": None, "checked_at": "2026-01-01T00:00:00Z"})
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
        _mock_current([existing], [], versions_json=existing.get("versions"))
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
        # default_params must survive the round-trip (the former ClassVar
        # annotation made pydantic v2 drop the field entirely).
        assert created.default_params == {"temperature": 0.5}


class TestHeaders:
    @respx.mock
    def test_bearer_authorization_header_sent(self) -> None:
        """The server (HTTPBearer) reads ONLY Authorization: Bearer — never X-Api-Key."""
        schemas_get = respx.get(
            "https://api.test/api/v1/schemas", params={"page": "1", "page_size": str(PAGE_SIZE)}
        ).respond(json={"items": [], "total": 0})
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0}
        )
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "secret-key", client=client)
            executor.fetch_current(parse_apply_documents(CONFIG_TEXT))
        request = schemas_get.calls.last.request
        assert request.headers[AUTH_HEADER.lower()] == "Bearer secret-key"
        assert "x-api-key" not in request.headers


GREEN_CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: first
      description: First
    - name: second
      description: Second
"""


class TestPagination:
    @respx.mock
    def test_fetch_current_collects_all_pages(self) -> None:
        """Two schemas that do not fit one page are both collected (no false 'created')."""
        page1 = [_schema_item("alpha", "First page"), _schema_item("beta", "First page")]
        page2 = [_schema_item("gamma", "Second page")]
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": page1, "total": 3, "page": 1, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/schemas", params={"page": "2", "page_size": "100"}).respond(
            json={"items": page2, "total": 3, "page": 2, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0}
        )
        config = parse_apply_documents("api_version: modulo.dev/v1\nentities: {}")
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            current = executor.fetch_current(config)
        assert sorted(current["schema"]) == ["alpha", "beta", "gamma"]

    @respx.mock
    def test_versions_fetch_passes_page_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "v")
        config_text = (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: alpha\n"
            "      description: Alpha schema\n"
            "      versions:\n"
            "        - version: v1\n"
            "          version_number: 1\n"
            "          definition_json:\n"
            "            type: object\n"
        )
        schema_id = str(uuid.uuid4())
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [_schema_item("alpha", "Alpha schema", schema_id=schema_id)], "total": 1}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0}
        )
        versions_route = respx.get(
            f"https://api.test/api/v1/schemas/{schema_id}/versions",
            params={"page": "1", "page_size": str(PAGE_SIZE)},
        ).respond(json={"items": [], "total": 0})
        config = parse_apply_documents(config_text)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            executor.fetch_current(config)
        assert versions_route.called

    @respx.mock
    def test_backend_list_403_falls_back_to_default_listing(self) -> None:
        """Operator-key 403 on include_in_dev degrades to the default listing."""
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        with_dev = respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            status_code=403, json={"detail": "operator role required"}
        )
        without_dev = respx.get(
            "https://api.test/api/v1/model-backends", params={"page": "1", "page_size": str(PAGE_SIZE)}
        ).respond(json={"items": [_backend_item("openai", "openai")], "total": 1})
        config = parse_apply_documents("api_version: modulo.dev/v1\nentities: {}")
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            current = executor.fetch_current(config)
        assert with_dev.called
        assert without_dev.called
        assert sorted(current["model_backend"]) == ["openai"]

    @respx.mock
    def test_backend_list_non_403_error_propagates(self) -> None:
        """Only 403 (in_dev disclosure) falls back; other errors propagate."""
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            status_code=500, json={"detail": "boom"}
        )
        config = parse_apply_documents("api_version: modulo.dev/v1\nentities: {}")
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            with pytest.raises(ApplyHttpError) as exc_info:
                executor.fetch_current(config)
        assert exc_info.value.status_code == 500


class TestRefBlocking:
    @respx.mock
    def test_secretref_blocked_at_plan_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        routes = _mock_current([], [])
        config_text = (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  model_backends:\n"
            "    - name: openai\n"
            "      display_name: OpenAI\n"
            "      provider: openai\n"
            "      model_id: gpt-x\n"
            "      api_key: secretref://vault/openai-key\n"
        )
        config = parse_apply_documents(config_text)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        blocked = [e for e in report["blocked"] if e["name"] == "openai"]
        assert len(blocked) == 1
        assert blocked[0]["reason"] == (
            "secretref resolution not supported yet (server-side resolution lands in a later slice)"
        )
        assert not report["created"]
        assert not report["failed"]
        assert routes["backends_post"].call_count == 0

    def test_empty_env_value_distinct_reason(self) -> None:
        config = parse_apply_documents(CONFIG_TEXT)
        resolved, blocked = resolve_secret_refs(config, environ={"SK": "   "})
        assert "openai" not in resolved
        assert len(blocked) == 1
        assert "empty" in blocked[0][2]
        assert "not set" not in blocked[0][2]


class TestVersionImmutability:
    @respx.mock
    def test_conflicting_version_string_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "v")
        config_text = (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: alpha\n"
            "      description: Alpha schema\n"
            "      versions:\n"
            "        - version: v1\n"
            "          version_number: 1\n"
            "          definition_json:\n"
            "            type: object\n"
            "            properties:\n"
            "              changed: {}\n"
        )
        existing = _schema_item("alpha", "Alpha schema")
        existing["versions"] = [
            {
                "version": "v1",
                "version_number": 1,
                "definition_json": {"type": "object"},
                "published": False,
            }
        ]
        _mock_current([existing], [], versions_json=existing.get("versions"))
        config = parse_apply_documents(config_text)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        blocked = [e for e in report["blocked"] if e["name"] == "alpha"]
        assert len(blocked) == 1
        assert "version v1 exists with different content" in blocked[0]["reason"]
        assert "immutable via apply" in blocked[0]["reason"]
        assert not report["updated"]
        assert not report["failed"]

    @respx.mock
    def test_identical_version_is_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "v")
        config_text = (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: alpha\n"
            "      description: Alpha schema\n"
            "      versions:\n"
            "        - version: v1\n"
            "          version_number: 1\n"
            "          definition_json:\n"
            "            type: object\n"
        )
        existing = _schema_item("alpha", "Alpha schema")
        existing["versions"] = [
            {
                "version": "v1",
                "version_number": 1,
                "definition_json": {"type": "object"},
                "published": False,
            }
        ]
        _mock_current([existing], [], versions_json=existing.get("versions"))
        config = parse_apply_documents(config_text)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        unchanged = [e for e in report["unchanged"] if e["name"] == "alpha"]
        assert len(unchanged) == 1
        assert not report["blocked"]


class TestFailureIsolation:
    """Failure-injection: one entity failing must not abort the others."""

    @respx.mock
    def test_schema_400_isolates_entity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = _run_isolated_http_case(monkeypatch, status_code=400)
        _assert_single_entity_failed(report, "first", kinds={"schema"})

    @respx.mock
    def test_schema_409_isolates_entity_with_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = _run_isolated_http_case(monkeypatch, status_code=409)
        entry = [e for e in report["failed"] if e["name"] == "first"]
        assert "name already exists; rerun to apply as update" in entry[0]["error"]

    @respx.mock
    def test_schema_500_isolates_entity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report = _run_isolated_http_case(monkeypatch, status_code=500)
        _assert_single_entity_failed(report, "first", kinds={"schema"})

    @respx.mock
    def test_connect_error_isolates_entity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """httpx.ConnectError moves exactly that entity to failed."""
        monkeypatch.setenv("SK", "v")
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0}
        )

        def _side_effect_factory() -> list:
            return [
                httpx.ConnectError("connection refused"),
                httpx.Response(201, json=_schema_item("second", None)),
            ]

        respx.post("https://api.test/api/v1/schemas").mock(side_effect=_side_effect_factory())
        config = parse_apply_documents(GREEN_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        _assert_single_entity_failed(report, "first")

    @respx.mock
    def test_later_entities_still_apply_after_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "v")
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0}
        )
        schema_post = respx.post("https://api.test/api/v1/schemas").mock(
            side_effect=[
                httpx.Response(500, json={"detail": "boom"}),
                httpx.Response(201, json=_schema_item("second", None)),
            ]
        )
        config = parse_apply_documents(GREEN_CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed_names = [e["name"] for e in report["failed"]]
        assert failed_names == ["first"]
        created_names = [e["name"] for e in report["created"]]
        assert created_names == ["second"]
        assert schema_post.call_count == 2

    @respx.mock
    def test_created_schema_version_post_failure_keeps_created_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "v")
        schema_id = str(uuid.uuid4())
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0}
        )
        respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": [], "total": 0}
        )
        respx.post("https://api.test/api/v1/schemas").mock(
            return_value=httpx.Response(201, json=_schema_item("alpha", None, schema_id=schema_id))
        )
        respx.post(f"https://api.test/api/v1/schemas/{schema_id}/versions").mock(
            return_value=httpx.Response(500, json={"detail": "version boom"})
        )
        config_text = (
            "api_version: modulo.dev/v1\n"
            "entities:\n"
            "  schemas:\n"
            "    - name: alpha\n"
            "      description: Alpha schema\n"
            "      versions:\n"
            "        - version: v1\n"
            "          version_number: 1\n"
            "          definition_json:\n"
            "            type: object\n"
        )
        config = parse_apply_documents(config_text)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        created = [e for e in report["created"] if e["kind"] == "schema" and e["name"] == "alpha"]
        assert len(created) == 1
        version_failures = [e for e in report["failed"] if e["kind"] == "schema_version" and e["name"] == "alpha/v1"]
        assert len(version_failures) == 1


def _run_isolated_http_case(monkeypatch: pytest.MonkeyPatch, status_code: int) -> dict:
    monkeypatch.setenv("SK", "v")
    respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
        json={"items": [], "total": 0}
    )
    respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
        json={"items": [], "total": 0}
    )
    respx.post("https://api.test/api/v1/schemas").mock(
        side_effect=[
            httpx.Response(status_code, json={"detail": "injected failure"}),
            httpx.Response(201, json=_schema_item("second", None)),
        ]
    )
    config = parse_apply_documents(GREEN_CONFIG_TEXT)
    with httpx.Client() as client:
        executor = ApplyExecutor("https://api.test", "key", client=client)
        report = executor.run(config, dry_run=False)
    _assert_single_entity_failed(report, "first", kinds={"schema"})
    return report


def _assert_single_entity_failed(
    report: dict,
    name: str,
    *,
    kinds: set[str] | None = None,
) -> None:
    assert [e["name"] for e in report["created"]] == ["second"]
    failed = report["failed"]
    assert len(failed) == 1
    assert failed[0]["name"] == name
    if kinds is not None:
        assert failed[0]["kind"] in kinds
    assert not report["updated"]


class TestHealthCheckSurfaced:
    @respx.mock
    def test_unhealthy_health_check_moves_created_backend_to_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SK", "resolved-secret")
        routes = _mock_current([], [])
        routes["health_post"].mock(
            return_value=httpx.Response(
                200,
                json={"status": "unhealthy", "detail": "401 from provider", "checked_at": "2026-01-01T00:00:00Z"},
            )
        )
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        created_names = [e["name"] for e in report["created"]]
        assert "openai" not in created_names
        assert "fresh" in created_names
        failed = [e for e in report["failed"] if e["name"] == "openai"]
        assert len(failed) == 1
        assert "failed health check" in failed[0]["error"]
        assert "401 from provider" in failed[0]["error"]
        # The backend was still created (POST sent the payload).
        assert routes["backends_post"].call_count == 1

    @respx.mock
    def test_health_check_403_keeps_finding_out_of_failed_when_parent_write_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A health-check failure is contained per-entity, not fatal."""
        monkeypatch.setenv("SK", "resolved-secret")
        routes = _mock_current([], [])
        routes["health_post"].mock(return_value=httpx.Response(500, json={"detail": "down"}))
        config = parse_apply_documents(CONFIG_TEXT)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        # Health-check endpoint 500 -> entity moves to failed with the message.
        failed_names = [e["name"] for e in report["failed"]]
        assert "openai" in failed_names


class TestHttpErrorSemantics:
    @respx.mock
    def test_3xx_treated_as_error(self) -> None:
        """Redirect responses (follow_redirects=False) are errors, not successes."""
        route = respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": str(PAGE_SIZE)}).respond(
            status_code=302, headers={"Location": "https://api.test/elsewhere"}
        )
        config = parse_apply_documents("api_version: modulo.dev/v1\nentities: {}")
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            with pytest.raises(ApplyHttpError) as exc_info:
                executor.fetch_current(config)
        assert route.call_count == 1
        assert exc_info.value.status_code == 302

    @respx.mock
    def test_get_404_carries_status_and_path(self) -> None:
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": str(PAGE_SIZE)}).respond(
            status_code=404, json={"detail": "not found"}
        )
        config = parse_apply_documents("api_version: modulo.dev/v1\nentities: {}")
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            with pytest.raises(ApplyHttpError) as exc_info:
                executor.fetch_current(config)
        assert exc_info.value.status_code == 404
        assert exc_info.value.path == "/schemas"

    @respx.mock
    def test_invalid_json_response_raises_apply_http_error(self) -> None:
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": str(PAGE_SIZE)}).respond(
            status_code=200, content=b"not-json{"
        )
        config = parse_apply_documents("api_version: modulo.dev/v1\nentities: {}")
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            with pytest.raises(ApplyHttpError) as exc_info:
                executor.fetch_current(config)
        assert "invalid JSON" in str(exc_info.value)


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
