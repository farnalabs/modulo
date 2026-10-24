"""``modulo apply`` support for the model-backend custom endpoint (FAR-2105).

FAR-2046 moved the operator-supplied endpoint into the credential-adjacent
``base_url`` field (plus the ``allow_insecure_http`` opt-in) and made it
credential-bound. These tests pin the declarative engine's contract for it:

* declare-to-manage semantics — omitted is unmanaged, explicit ``null`` clears;
* ``base_url`` / ``allow_insecure_http`` in the canonical drift hash, so
  ``--diff`` sees a changed endpoint instead of reporting a no-op;
* both fields sent on create and (only when changed) on update;
* the shared ``validate_base_url`` floor applied at PLAN time;
* the credential-binding rule surfaced at PLAN time (a vault reference cannot
  create or move an endpoint).

HTTP is a recording stub (respx): rather than asserting against a mock that
would pass either way, every write payload is validated against the REAL
``ModelBackendCreate`` / ``ModelBackendUpdate`` API models, so a field the
server would reject fails here.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx
import pytest
import respx

from modulo.api.routes.model_backends import ModelBackendCreate, ModelBackendUpdate
from modulo.cli.apply.executor import PAGE_SIZE, ApplyExecutor
from modulo.cli.apply.loader import ApplyLoadError, parse_apply_documents
from modulo.cli.apply.models import ModelBackendEntity

_CONFIG = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: local
      display_name: Local gateway
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
      base_url: {base_url}
"""

_CONFIG_OMITTED = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: local
      display_name: Local gateway
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
"""

_CONFIG_CREDENTIAL_REF = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: local
      display_name: Local gateway
      provider: openai
      model_id: gpt-x
      credential_ref: local-key
      base_url: {base_url}
"""


def _config_with(*, base_url: str) -> str:
    """Render the literal-key config with the given endpoint declaration."""
    return _CONFIG.replace("{base_url}", base_url)


def _credential_ref_config_with(*, base_url: str) -> str:
    """Render the vault-reference config with the given endpoint declaration."""
    return _CONFIG_CREDENTIAL_REF.replace("{base_url}", base_url)


_BACKENDS_LIST_PARAMS = {"page": "1", "page_size": str(PAGE_SIZE), "include_in_dev": "true"}
_HEALTH_PATH = re.compile(r"https://api\.test/api/v1/model-backends/[^/]+/health-check")
_BACKEND_ID = "00000000-0000-0000-0000-00000000b001"


def _backend_item(
    *,
    base_url: str | None = None,
    has_credentials: bool = True,
    display_name: str = "Local gateway",
    model_id: str = "gpt-x",
) -> dict:
    return {
        "id": _BACKEND_ID,
        "organisation_id": str(uuid.uuid4()),
        "name": "local",
        "display_name": display_name,
        "provider": "openai",
        "model_id": model_id,
        "has_credentials": has_credentials,
        "base_url": base_url,
        "default_params": {},
        "visibility": "org",
        "tier": "native",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def _mock_state(backends: list[dict]) -> dict[str, respx.Route]:
    """Mock the list/versions/create/patch/health surfaces used by apply."""
    return {
        "schemas_get": respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        ),
        "backends_get": respx.get("https://api.test/api/v1/model-backends", params=_BACKENDS_LIST_PARAMS).respond(
            json={"items": backends, "total": len(backends), "page": 1, "page_size": 100}
        ),
        "backends_post": respx.post("https://api.test/api/v1/model-backends").respond(
            201, json=_backend_item(base_url="https://gateway.internal/v1")
        ),
        "backends_patch": respx.patch(f"https://api.test/api/v1/model-backends/{_BACKEND_ID}").respond(
            200, json=_backend_item()
        ),
        "mcp_post": respx.post("https://api.test/mcp/").respond(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"text": json.dumps({"secrets": [{"key": "local-key"}], "total": 1})}]},
            },
        ),
        "health_post": respx.post(_HEALTH_PATH).respond(
            200, json={"status": "ok", "detail": None, "checked_at": "2026-01-01T00:00:00Z"}
        ),
    }


def _run(config_text: str, *, current: list[dict], monkeypatch: pytest.MonkeyPatch, **kwargs) -> tuple[dict, dict]:
    monkeypatch.setenv("SK", "literal-key")
    with respx.mock:
        routes = _mock_state(current)
        config = parse_apply_documents(config_text)
        with httpx.Client() as client:
            executor = ApplyExecutor("https://api.test", "key", client=client)
            report = executor.run(config, **kwargs)
    return report, routes


def _payload(route: respx.Route) -> dict:
    return json.loads(route.calls.last.request.content)


class TestCreate:
    def test_create_sends_the_declared_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _config_with(base_url="https://gateway.internal/v1"),
            current=[],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["created"]] == ["local"]
        assert not report["blocked"]
        payload = _payload(routes["backends_post"])
        # Contract round-trip: the real API model must accept the wire shape.
        created = ModelBackendCreate.model_validate(payload)
        assert created.base_url == "https://gateway.internal/v1"
        assert created.api_key == "literal-key"

    def test_rerun_with_the_same_endpoint_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _config_with(base_url="https://gateway.internal/v1"),
            current=[_backend_item(base_url="https://gateway.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["unchanged"]] == ["local"]
        assert not report["updated"]
        assert routes["backends_patch"].call_count == 0

    def test_plain_http_endpoint_sends_the_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_text = _config_with(base_url="http://gateway.internal/v1").replace(
            "      base_url: http://gateway.internal/v1\n",
            "      base_url: http://gateway.internal/v1\n      allow_insecure_http: true\n",
        )
        report, routes = _run(config_text, current=[], monkeypatch=monkeypatch, dry_run=False)
        assert [e["name"] for e in report["created"]] == ["local"]
        created = ModelBackendCreate.model_validate(_payload(routes["backends_post"]))
        assert created.base_url == "http://gateway.internal/v1"
        assert created.allow_insecure_http is True

    def test_plain_http_no_drift_when_the_flat_opt_in_is_echoed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The HTTP opt-in is not persisted separately — the scheme is the record."""
        config_text = _config_with(base_url="http://gateway.internal/v1").replace(
            "      base_url: http://gateway.internal/v1\n",
            "      base_url: http://gateway.internal/v1\n      allow_insecure_http: true\n",
        )
        report, routes = _run(
            config_text,
            current=[_backend_item(base_url="http://gateway.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["unchanged"]] == ["local"]
        assert routes["backends_patch"].call_count == 0

    def test_credential_ref_cannot_create_a_custom_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _credential_ref_config_with(base_url="https://gateway.internal/v1"),
            current=[],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert routes["backends_post"].call_count == 0
        blocked = [e for e in report["blocked"] if e["name"] == "local"]
        assert blocked
        assert "literal api_key" in blocked[0]["reason"]


class TestUpdate:
    def test_changed_endpoint_is_drift_and_patches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _config_with(base_url="https://new.internal/v1"),
            current=[_backend_item(base_url="https://old.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
            drift=True,
        )
        assert [e["name"] for e in report["updated"]] == ["local"]
        fields = report["drift_detail"]["model_backend:local"]["fields"]
        assert "base_url" in fields["modified"]
        # Drift mode never writes.
        assert routes["backends_patch"].call_count == 0

    def test_changed_endpoint_patches_with_the_endpoint_and_literal_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _config_with(base_url="https://new.internal/v1"),
            current=[_backend_item(base_url="https://old.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["updated"]] == ["local"]
        payload = _payload(routes["backends_patch"])
        update = ModelBackendUpdate.model_validate(payload)
        assert update.base_url == "https://new.internal/v1"
        assert update.api_key == "literal-key"

    def test_explicit_null_clears_the_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _config_with(base_url="null"),
            current=[_backend_item(base_url="https://old.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["updated"]] == ["local"]
        payload = _payload(routes["backends_patch"])
        assert "base_url" in payload
        assert payload["base_url"] is None
        assert payload["api_key"] == "literal-key"
        assert "allow_insecure_http" not in payload

    def test_omitted_endpoint_leaves_a_live_endpoint_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _CONFIG_OMITTED,
            current=[_backend_item(base_url="https://old.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["unchanged"]] == ["local"]
        assert routes["backends_patch"].call_count == 0

    def test_omitted_endpoint_patches_without_the_endpoint_or_credential(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An omitted endpoint is NOT written, even when another field changes.

        This is the case where the unmanaged-omission gate can actually be
        exercised: the entity is ``updated`` (display_name differs), so a PATCH
        IS sent — and it must carry neither ``base_url`` nor ``api_key``. If the
        ``manages_base_url`` gate is dropped, this PATCH sends a credential (and
        can clear a live endpoint), which no call-count assertion would catch.
        """
        report, routes = _run(
            _CONFIG_OMITTED.replace("display_name: Local gateway", "display_name: Renamed gateway"),
            current=[_backend_item(base_url="https://old.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["updated"]] == ["local"]
        payload = _payload(routes["backends_patch"])
        assert payload["display_name"] == "Renamed gateway"
        assert "base_url" not in payload
        assert "api_key" not in payload

    def test_https_with_the_flag_declared_has_no_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flag is not stored: hashing it raw would report permanent false drift.

        The server accepts ``https`` + ``allow_insecure_http: true`` but cannot
        echo the flag back (it stores only the endpoint), so the hash compares
        the EFFECTIVE, scheme-derived value on both sides. Raw-flag hashing
        would make this configuration drift forever.
        """
        config_text = _config_with(base_url="https://gateway.internal/v1").replace(
            "      base_url: https://gateway.internal/v1\n",
            "      base_url: https://gateway.internal/v1\n      allow_insecure_http: true\n",
        )
        report, routes = _run(
            config_text,
            current=[_backend_item(base_url="https://gateway.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["unchanged"]] == ["local"]
        assert not report["updated"]
        assert routes["backends_patch"].call_count == 0

    def test_http_endpoint_against_a_live_https_drifts_on_both_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        config_text = _config_with(base_url="http://gateway.internal/v1").replace(
            "      base_url: http://gateway.internal/v1\n",
            "      base_url: http://gateway.internal/v1\n      allow_insecure_http: true\n",
        )
        report, _routes = _run(
            config_text,
            current=[_backend_item(base_url="https://gateway.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
            drift=True,
        )
        assert [e["name"] for e in report["updated"]] == ["local"]
        fields = report["drift_detail"]["model_backend:local"]["fields"]
        assert set(fields["modified"]) == {"base_url", "allow_insecure_http"}

    def test_omitted_endpoint_does_not_hash_against_a_live_endpoint(self) -> None:
        entity = ModelBackendEntity.model_validate(
            {
                "name": "local",
                "display_name": "Local",
                "provider": "openai",
                "model_id": "gpt",
                "api_key": "${env:SK}",
            }
        )
        assert "base_url" not in entity.managed_view()

    def test_vault_reference_cannot_change_an_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        report, routes = _run(
            _credential_ref_config_with(base_url="https://new.internal/v1"),
            current=[_backend_item(base_url="https://old.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert routes["backends_patch"].call_count == 0
        blocked = [e for e in report["blocked"] if e["name"] == "local"]
        assert blocked
        assert "literal api_key" in blocked[0]["reason"]

    def test_unchanged_endpoint_allows_a_vault_ref_to_update_other_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, routes = _run(
            _credential_ref_config_with(base_url="https://same.internal/v1").replace(
                "display_name: Local gateway", "display_name: Renamed gateway"
            ),
            current=[_backend_item(base_url="https://same.internal/v1")],
            monkeypatch=monkeypatch,
            dry_run=False,
        )
        assert [e["name"] for e in report["updated"]] == ["local"]
        payload = _payload(routes["backends_patch"])
        assert "base_url" not in payload
        assert "api_key" not in payload
        assert payload["display_name"] == "Renamed gateway"


class TestPlanTimeValidation:
    @pytest.mark.parametrize(
        ("base_url", "opt_in", "expected"),
        [
            pytest.param("http://169.254.169.254/v1", False, "cloud metadata", id="metadata_ipv4"),
            pytest.param("https://metadata.google.internal/v1", False, "cloud metadata", id="metadata_hostname"),
            pytest.param("http://0x7f000001/v1", False, "hex-encoded IP literal", id="non_canonical_ip"),
            pytest.param("http://gateway.internal/v1", False, "https://", id="plain_http_without_opt_in"),
            pytest.param("https://gateway.internal/v1?key=1", False, "query string", id="query_string"),
        ],
    )
    def test_invalid_endpoint_blocks_at_plan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        base_url: str,
        opt_in: bool,
        expected: str,
    ) -> None:
        config_text = _config_with(base_url=base_url)
        if opt_in:
            config_text = config_text.replace(
                f"      base_url: {base_url}\n",
                f"      base_url: {base_url}\n      allow_insecure_http: true\n",
            )
        report, routes = _run(config_text, current=[], monkeypatch=monkeypatch, dry_run=True)
        blocked = [e for e in report["blocked"] if e["name"] == "local"]
        assert blocked
        assert expected in blocked[0]["reason"]
        assert routes["backends_post"].call_count == 0

    def test_server_without_endpoint_support_blocks_instead_of_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response shape predating FAR-2046 must fail loudly, never silently converge."""
        current = _backend_item(base_url="https://old.internal/v1")
        current.pop("base_url")
        report, routes = _run(
            _config_with(base_url="https://new.internal/v1"),
            current=[current],
            monkeypatch=monkeypatch,
            dry_run=True,
        )
        blocked = [e for e in report["blocked"] if e["name"] == "local"]
        assert blocked
        assert "did not report an endpoint" in blocked[0]["reason"]
        assert routes["backends_patch"].call_count == 0

    def test_non_string_base_url_is_rejected_at_load(self) -> None:
        """A non-string (e.g. YAML int) must fail at load, not reach the wire."""
        with pytest.raises(ApplyLoadError, match="base_url must be a string"):
            parse_apply_documents(_CONFIG.replace("{base_url}", "123"))

    def test_empty_base_url_is_rejected_at_load(self) -> None:
        with pytest.raises(ApplyLoadError, match="non-empty string"):
            parse_apply_documents(_config_with(base_url='""'))

    def test_allow_insecure_http_requires_a_base_url(self) -> None:
        config_text = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: local
      display_name: Local
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
      allow_insecure_http: true
"""
        with pytest.raises(ApplyLoadError, match="requires base_url"):
            parse_apply_documents(config_text)

    def test_allow_insecure_http_cannot_accompany_an_explicit_null(self) -> None:
        config_text = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: local
      display_name: Local
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
      base_url: null
      allow_insecure_http: true
"""
        with pytest.raises(ApplyLoadError, match="requires base_url"):
            parse_apply_documents(config_text)
