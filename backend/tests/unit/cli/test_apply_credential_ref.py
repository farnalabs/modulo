"""Tests for ``credential_ref`` org-vault references in ``modulo apply`` (FAR-1216).

Covers the declarative field (model validation + wire normalisation to
``secretref://<key>``), plan-time vault-key validation (missing / unverifiable
keys block the plan, never a silent pass), and the verbatim surfacing of the
server's typed ``credential_reference_error`` (422) at apply time.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
import respx

from modulo.api.routes.model_backends import ModelBackendCreate
from modulo.cli.apply import executor as executor_module
from modulo.cli.apply.executor import (
    PAGE_SIZE,
    ApplyExecutor,
    VaultListingError,
    resolve_secret_refs,
)
from modulo.cli.apply.loader import parse_apply_documents
from modulo.cli.apply.models import ModelBackendEntity

_CREDENTIAL_REF_CONFIG = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: openai
      display_name: OpenAI
      provider: openai
      model_id: gpt-x
      credential_ref: vault/openai-key
"""

_BACKENDS_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE), "include_in_dev": "true"}


def _backend_item(name: str, provider: str) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "organisation_id": str(uuid.uuid4()),
        "name": name,
        "display_name": name,
        "provider": provider,
        "model_id": "some-model",
        "has_credentials": True,
        "default_params": {},
        "visibility": "org",
        "tier": "native",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _mock_backends(backends: list[dict]) -> dict:
    routes: dict = {}
    routes["schemas_get"] = respx.get(
        "https://api.test/api/v1/schemas", params={"page": "1", "page_size": str(PAGE_SIZE)}
    ).respond(json={"items": [], "total": 0, "page": 1, "page_size": PAGE_SIZE})
    routes["backends_get"] = respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
        json={"items": backends, "total": len(backends), "page": 1, "page_size": PAGE_SIZE}
    )
    routes["backends_post"] = respx.post("https://api.test/api/v1/model-backends").respond(
        json=_backend_item("openai", "openai")
    )
    routes["health"] = respx.post(re.compile(r"https://api\.test/api/v1/model-backends/[^/]+/health-check")).respond(
        json={"status": "ok"}
    )
    return routes


def _mcp_envelope(secrets: list[dict], total: int | None = None) -> dict:
    """A faithful ``list_secrets`` StreamableHTTP JSON-RPC response envelope."""
    payload = {"secrets": secrets, "total": total if total is not None else len(secrets)}
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False,
        },
    }


def _mock_vault(route_body: dict):
    return respx.post("https://api.test/mcp/").respond(json=route_body)


class TestCredentialRefModel:
    def test_credential_ref_normalised_to_secretref_token(self) -> None:
        entity = ModelBackendEntity(
            name="openai",
            display_name="OpenAI",
            provider="openai",
            model_id="gpt-x",
            credential_ref="vault/openai-key",
        )
        assert entity.credential_reference() == "secretref://vault/openai-key"
        assert entity.api_key is None
        assert entity.env_ref_var() is None

    def test_managed_view_never_hashes_credential(self) -> None:
        entity = ModelBackendEntity(
            name="openai",
            display_name="OpenAI",
            provider="openai",
            model_id="gpt-x",
            credential_ref="vault/openai-key",
        )
        assert "api_key" not in entity.managed_view()
        assert entity.managed_view(include_api_key=True)["api_key"] == "secretref://vault/openai-key"

    def test_both_api_key_and_credential_ref_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one of api_key or credential_ref"):
            ModelBackendEntity(
                name="openai",
                display_name="OpenAI",
                provider="openai",
                model_id="gpt-x",
                api_key="${env:SK}",
                credential_ref="vault/openai-key",
            )

    def test_neither_api_key_nor_credential_ref_rejected(self) -> None:
        with pytest.raises(ValueError, match="exactly one of api_key or credential_ref"):
            ModelBackendEntity(
                name="openai",
                display_name="OpenAI",
                provider="openai",
                model_id="gpt-x",
            )

    def test_credential_ref_with_scheme_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="bare vault key"):
            ModelBackendEntity(
                name="openai",
                display_name="OpenAI",
                provider="openai",
                model_id="gpt-x",
                credential_ref="secretref://vault/openai-key",
            )

    def test_credential_ref_with_whitespace_rejected(self) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            ModelBackendEntity(
                name="openai",
                display_name="OpenAI",
                provider="openai",
                model_id="gpt-x",
                credential_ref="vault/openai key",
            )


class TestResolveSecretRefs:
    def test_credential_ref_forwarded_as_secretref_token(self) -> None:
        config = parse_apply_documents(_CREDENTIAL_REF_CONFIG)
        resolved, blocked = resolve_secret_refs(config, environ={})
        assert resolved == {"openai": "secretref://vault/openai-key"}
        assert not blocked


class TestVaultListingParser:
    def test_parses_keys(self) -> None:
        keys = executor_module._parse_vault_key_listing(_mcp_envelope([{"key": "a"}, {"key": "b"}]))
        assert keys == {"a", "b"}

    def test_jsonrpc_error_envelope_raises(self) -> None:
        envelope = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        with pytest.raises(VaultListingError, match="boom"):
            executor_module._parse_vault_key_listing(envelope)

    def test_tool_iserror_raises(self) -> None:
        envelope = {"result": {"isError": True, "content": []}}
        with pytest.raises(VaultListingError, match="tool error"):
            executor_module._parse_vault_key_listing(envelope)

    def test_truncated_listing_raises(self) -> None:
        with pytest.raises(VaultListingError, match="only 1 were listed"):
            executor_module._parse_vault_key_listing(_mcp_envelope([{"key": "a"}], total=5))

    def test_missing_content_raises(self) -> None:
        with pytest.raises(VaultListingError, match="no content"):
            executor_module._parse_vault_key_listing({"result": {"isError": False}})


class TestListVaultKeys:
    @respx.mock
    def test_sends_jsonrpc_tools_call(self) -> None:
        route = _mock_vault(_mcp_envelope([{"key": "vault/openai-key"}]))
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            keys = executor.list_vault_keys()
        assert keys == {"vault/openai-key"}
        sent = json.loads(route.calls[0].request.content)
        assert sent["method"] == "tools/call"
        assert sent["params"]["name"] == "list_secrets"

    @respx.mock
    def test_non_2xx_raises(self) -> None:
        respx.post("https://api.test/mcp/").respond(status_code=402, json={"detail": "feature required"})
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            with pytest.raises(VaultListingError, match="status 402"):
                executor.list_vault_keys()


class TestPlanTimeValidation:
    @respx.mock
    def test_present_key_plans_normally(self) -> None:
        routes = _mock_backends([])
        _mock_vault(_mcp_envelope([{"key": "vault/openai-key"}]))
        config = parse_apply_documents(_CREDENTIAL_REF_CONFIG)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        assert not [e for e in report["blocked"] if e["name"] == "openai"]
        assert any(entry["name"] == "openai" for entry in report["created"])
        assert routes["backends_post"].call_count == 0

    @respx.mock
    def test_missing_key_blocks_the_plan(self) -> None:
        _mock_backends([])
        _mock_vault(_mcp_envelope([{"key": "some/other-key"}]))
        config = parse_apply_documents(_CREDENTIAL_REF_CONFIG)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "openai"]
        assert len(blocked) == 1
        assert "vault/openai-key" in blocked[0]["reason"]
        assert "not found in the org vault" in blocked[0]["reason"]
        assert not report["created"]

    @respx.mock
    def test_unverifiable_listing_blocks_fail_closed(self) -> None:
        _mock_backends([])
        respx.post("https://api.test/mcp/").respond(status_code=500, json={"detail": "oops"})
        config = parse_apply_documents(_CREDENTIAL_REF_CONFIG)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "openai"]
        assert len(blocked) == 1
        assert "cannot verify credential_ref" in blocked[0]["reason"]
        assert not report["created"]

    @respx.mock
    def test_config_without_credential_ref_never_lists_vault(self) -> None:
        """A literal / env-ref / secretref api_key config makes no MCP call."""
        _mock_backends([])
        mcp = respx.post("https://api.test/mcp/").respond(json=_mcp_envelope([]))
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
            executor.run(config, dry_run=True)
        assert mcp.call_count == 0


class TestApplyTimeTypedError:
    @respx.mock
    def test_credential_ref_sent_as_secretref_in_create(self) -> None:
        routes = _mock_backends([])
        _mock_vault(_mcp_envelope([{"key": "vault/openai-key"}]))
        config = parse_apply_documents(_CREDENTIAL_REF_CONFIG)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        assert routes["backends_post"].call_count == 1
        sent = json.loads(routes["backends_post"].calls[0].request.content)
        assert sent["api_key"] == "secretref://vault/openai-key"
        # The request body must validate against the REAL API model.
        ModelBackendCreate.model_validate(sent)
        assert any(entry["name"] == "openai" for entry in report["created"])

    @respx.mock
    def test_server_typed_422_surfaced_verbatim(self) -> None:
        routes = _mock_backends([])
        _mock_vault(_mcp_envelope([{"key": "vault/openai-key"}]))
        routes["backends_post"].mock(
            return_value=httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "type": "credential_reference_error",
                            "loc": ["body", "api_key"],
                            "msg": ("vault key 'vault/openai-key': vault key not found in this organisation"),
                        }
                    ]
                },
            )
        )
        config = parse_apply_documents(_CREDENTIAL_REF_CONFIG)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, dry_run=False)
        failed = [entry for entry in report["failed"] if entry["name"] == "openai"]
        assert len(failed) == 1
        assert "credential_reference_error" in failed[0]["error"]
        assert "vault/openai-key" in failed[0]["error"]
        assert not report["created"]
