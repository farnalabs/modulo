"""Unit tests for the generic REST connector (FAR-408)."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from modulo.connectors._rate_bucket import TokenBucket
from modulo.connectors.base import ConnectorPayload, ConnectorQuery, ConnectorType
from modulo.connectors.rest import (
    _SENSITIVE_VALUE_MASK,
    RESTCardinalityExceededError,
    RESTConnectError,
    RestConnector,
    RESTFanOutCancelledError,
    RESTFanOutFailureError,
    RESTRateLimitTimeoutError,
    RESTResponseTooLargeError,
    RESTStatusError,
    SecurityGuard,
)
from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK
from tests.connectors._conformance import assert_result_shape, assert_write_result_shape
from tests.connectors._noop_guard import make_noop_security_guard as _noop_guard


def _default_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={})


def _make_connector(
    config: dict[str, Any] | None,
    creds: dict[str, Any] | None,
) -> RestConnector:
    """Build a RestConnector against a stub HTTP transport (no real network)."""
    return RestConnector(
        config,
        creds,
        transport=httpx.MockTransport(_default_handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
    )


# ── Connector type + capabilities ───────────────────────────


def test_connector_type_is_rest() -> None:
    c = RestConnector({"base_url": "https://api.example.com", "path": "/items"}, {"auth_mode": "bearer", "token": "t"})
    assert c.connector_type is ConnectorType.REST
    assert ConnectorType.REST.capabilities == frozenset({"read", "write"})


# ── Query: records transform ─────────────────────────────


def test_query_records_path_extracts_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 3
    assert [r["id"] for r in result.records] == [1, 2, 3]
    assert result.total == 3


def test_query_top_level_array() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"a": 1}, {"a": 2}])

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 2


def test_query_passthrough_raw_body_feeds_shape() -> None:
    """Raw/passthrough content-type still yields a list-of-dicts record shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="a,b\n1,2", headers={"content-type": "text/csv"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 1
    assert result.records[0]["content_type"] == "text/csv"
    assert result.records[0]["body"] == "a,b\n1,2"


def test_query_unknown_resource_raises() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "operations": {"users": {"path": "/items"}}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="Unsupported REST resource"):
        asyncio_run(c.query(ConnectorQuery(resource="nope")))


def test_query_empty_resource_raises() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="requires a resource name"):
        asyncio_run(c.query(ConnectorQuery(resource="")))


# ── Query: templating from filters ───────────────────────────


def test_query_templates_url_and_params_from_filters() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users/{{ user_id }}",
            "params": {"page": "{{ page }}"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default", filters={"user_id": 42, "page": 2})))
    assert str(captured["url"]) == "https://api.example.com/users/42?page=2"
    assert captured["params"] == {"page": "2"}


def test_query_templates_headers_from_filters() -> None:
    """Header values render from runtime variables via the sandboxed Jinja env —
    the headers leg of the templated-fields behaviour (path/params/body/headers)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "headers": {
                "X-Branch": "{{ branch }}",
                "X-Runner": "{{ resource }}",
            },
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default", filters={"branch": "feature/foo"})))
    assert captured.get("x-branch") == "feature/foo"
    assert captured.get("x-runner") == "default"


# ── Auth modes ──────────────────────────────────


def test_auth_bearer() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["authorization"] == "Bearer sec-token"


def test_auth_api_key_header() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k123", "in": "header", "header_name": "X-API-Key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["x-api-key"] == "k123"


def test_auth_api_key_custom_header_name() -> None:
    """api_key header mode honours a custom header_name (not only the default
    X-API-Key), and that custom name becomes a protected header."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k567", "in": "header", "header_name": "X-Tenant-Key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured.get("x-tenant-key") == "k567"
    assert "x-api-key" not in captured

    # The custom header name is protected: a rendered header may not override it.
    c2 = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"X-Tenant-Key": "evil"}},
        {"auth_mode": "api_key", "api_key": "k567", "in": "header", "header_name": "X-Tenant-Key"},
    )
    with pytest.raises(ValueError, match="protected header"):
        asyncio_run(c2.query(ConnectorQuery(resource="default")))


def test_auth_api_key_query() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k456", "in": "query", "query_param_name": "api_key"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"] == {"api_key": "k456"}


def test_auth_api_key_empty_header_name_falls_back_to_default() -> None:
    """An empty-string ``header_name`` is UNSET, not a name: it must fall back to
    the ``X-API-Key`` default instead of passing through (httpx rejects an empty
    header name, which would brick every request the connector issues)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k789", "in": "header", "header_name": ""},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured.get("x-api-key") == "k789"


def test_auth_api_key_whitespace_query_param_name_falls_back_to_default() -> None:
    """A whitespace-only ``query_param_name`` is UNSET: it must fall back to the
    ``api_key`` default instead of being used verbatim as a query key."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "k456", "in": "query", "query_param_name": "   "},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"] == {"api_key": "k456"}


def test_auth_api_key_explicit_non_empty_names_pass_through() -> None:
    """Explicit non-empty ``header_name``/``query_param_name`` values pass
    through verbatim — the empty/whitespace coercion never touches a real name."""
    auth = RestConnector._normalise_auth(
        {"auth_mode": "api_key", "api_key": "k1", "in": "header", "header_name": "X-Custom-Auth"}
    )
    assert auth["header_name"] == "X-Custom-Auth"
    auth = RestConnector._normalise_auth(
        {"auth_mode": "api_key", "api_key": "k1", "in": "query", "query_param_name": "auth_token"}
    )
    assert auth["query_param_name"] == "auth_token"


def test_api_key_query_secret_not_screened() -> None:
    """A query-mode api_key with filter-triggering chars must never hit the injection filter.

    The credential is injected into params AFTER the screening guard (the same
    invariant as header mode, where apply_auth runs after the guard), so a legit
    secret containing characters the output filter would reject still round-trips.
    """
    screened: list[str] = []

    class _TrackingGuard(SecurityGuard):
        def __init__(self) -> None:
            super().__init__(validate_url=self._noop_validate, filter_strings=self._track_filter)

        @staticmethod
        async def _noop_validate(_url: str) -> None:
            return None

        @staticmethod
        def _track_filter(values: list[str], _resource: str) -> None:
            screened.extend(values)

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    secret = "k{{java}}ev"
    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": secret, "in": "query", "query_param_name": "api_key"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_TrackingGuard(),
    )
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"] == {"api_key": secret}
    assert secret not in screened


def test_auth_basic() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "basic", "username": "u", "password": "p"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["authorization"].startswith("Basic ")


# ── Credential auth contract (FAR-504) ────────────────────────────────


def test_validate_credentials_is_authoritative_auth_contract() -> None:
    """``validate_credentials`` is the single source of truth for the REST
    required-secret contract (FAR-504): rejected dicts must also be rejected by
    ``_normalise_auth`` (and vice versa), so the API boundary and the run-time
    connector never drift. bearer->token; basic->username+password;
    api_key->api_key + in header/query."""
    valid = [
        {"auth_mode": "bearer", "token": "t"},
        {"auth_mode": "api_key", "api_key": "k"},
        {"auth_mode": "api_key", "api_key": "k", "in": "header", "header_name": "X-Key"},
        {"auth_mode": "api_key", "api_key": "k", "in": "query", "query_param_name": "token"},
        {"auth_mode": "basic", "username": "u", "password": "p"},
    ]
    invalid = [
        {"auth_mode": "bearer"},  # missing token
        {"auth_mode": "api_key"},  # missing api_key
        {"auth_mode": "basic", "username": "u"},  # missing password
        {"auth_mode": "basic", "password": "p"},  # missing username
        {"auth_mode": "opaque"},  # unsupported mode
        {"auth_mode": "api_key", "api_key": "k", "in": "cookie"},  # in must be header/query
    ]
    for creds in valid:
        RestConnector.validate_credentials(creds)
        RestConnector._normalise_auth(creds)
    for creds in invalid:
        with pytest.raises(ValueError, match="REST "):
            RestConnector.validate_credentials(creds)
        with pytest.raises(ValueError, match="REST "):
            RestConnector._normalise_auth(creds)


def test_validate_credentials_raises_on_missing_required_secret() -> None:
    """Each auth_mode raises a specific ValueError naming the missing secret."""
    with pytest.raises(ValueError, match="requires creds\\['token'\\]"):
        RestConnector.validate_credentials({"auth_mode": "bearer"})
    with pytest.raises(ValueError, match="requires creds\\['username'\\] and creds\\['password'\\]"):
        RestConnector.validate_credentials({"auth_mode": "basic", "username": "u"})
    with pytest.raises(ValueError, match="requires creds\\['api_key'\\]"):
        RestConnector.validate_credentials({"auth_mode": "api_key"})
    with pytest.raises(ValueError, match=r"auth_mode.*one of 'bearer', 'api_key', 'basic'"):
        RestConnector.validate_credentials({"auth_mode": "opaque"})


def test_validate_credentials_rejects_masked_secret() -> None:
    """A masked placeholder (SENSITIVE_VALUE_MASK) is NOT a real secret: CREATE
    must reject it rather than persist the literal mask as the stored credential
    (guaranteed runtime auth failure). All three secret-bearing auth_modes are
    covered (FAR-504)."""
    # The connector's local mirror of the mask sentinel must stay in sync with the
    # canonical value (the connector cannot import ``modulo.core`` per the
    # import-linter contract, so it mirrors the string — a drift here silently
    # defeats the masked-rejection guard).
    assert _SENSITIVE_VALUE_MASK == SENSITIVE_VALUE_MASK
    masked_bearer = {"auth_mode": "bearer", "token": SENSITIVE_VALUE_MASK}
    masked_basic = {"auth_mode": "basic", "username": "u", "password": SENSITIVE_VALUE_MASK}
    masked_api_key = {"auth_mode": "api_key", "api_key": SENSITIVE_VALUE_MASK}
    # A masked value gets the DEDICATED message (FAR-504 review minor): the plain
    # "requires creds['token']" wording would wrongly imply the key was absent,
    # when in fact a (placeholder) value WAS supplied.
    with pytest.raises(ValueError, match=r"requires creds\['token'\].*redaction-mask placeholder"):
        RestConnector.validate_credentials(masked_bearer)
    with pytest.raises(
        ValueError, match=r"requires creds\['username'\] and creds\['password'\].*redaction-mask placeholder"
    ):
        RestConnector.validate_credentials(masked_basic)
    with pytest.raises(ValueError, match=r"requires creds\['api_key'\].*redaction-mask placeholder"):
        RestConnector.validate_credentials(masked_api_key)


def test_validate_credentials_rejects_whitespace_only_secret() -> None:
    """A whitespace-only secret is MISSING — a blank credential string should be
    rejected rather than persisted as a real (broken) secret (FAR-504)."""
    blank_bearer = {"auth_mode": "bearer", "token": "   "}
    blank_basic = {"auth_mode": "basic", "username": "   ", "password": "u"}
    blank_api_key = {"auth_mode": "api_key", "api_key": "\t\n"}
    for creds in (blank_bearer, blank_basic, blank_api_key):
        with pytest.raises(ValueError, match="REST "):
            RestConnector.validate_credentials(creds)


def test_validate_credentials_accepts_real_nonempty_secret() -> None:
    """A legitimate non-empty secret still passes validation for every mode
    (the masked/whitespace rejection must never reject a real secret)."""
    valid = [
        {"auth_mode": "bearer", "token": "sk-live-real-token-12345"},
        {"auth_mode": "api_key", "api_key": "a-real-api-key"},
        {"auth_mode": "api_key", "api_key": "a-real-api-key", "in": "header"},
        {"auth_mode": "basic", "username": "user@example.com", "password": "hunter2!"},
    ]
    for creds in valid:
        RestConnector.validate_credentials(creds)  # must not raise for a real secret
        auth = RestConnector._normalise_auth(creds)
        assert auth["mode"] == str(creds["auth_mode"]).lower()
        if creds["auth_mode"] == "bearer":
            assert auth["token"] == creds["token"]
        elif creds["auth_mode"] == "basic":
            assert auth["username"] == creds["username"]
            assert auth["password"] == creds["password"]
        else:  # api_key
            assert auth["api_key"] == creds["api_key"]
            assert auth["in"] == str(creds.get("in", "header")).lower()


# ── Injection guard ────────────────────────────────


def test_guard_rejects_auth_header_override() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"Authorization": "evil"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="protected header"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_crlf_in_header_value() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"X-Foo": "ok\r\nX-Evil: 1"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="control characters"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_host_not_in_allowlist() -> None:
    c = _make_connector(
        {
            "base_url": "https://evil.example.com",
            "path": "/items",
            "allowed_hosts": ["api.example.com"],
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="not in allowed_hosts"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_guard_rejects_non_http_scheme() -> None:
    c = _make_connector(
        {"base_url": "file:///etc/passwd", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="scheme"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_query_benign_injection_phrase_not_rejected() -> None:
    """A read/query whose term would trip the prompt-injection TEXT classifier must succeed.

    The prompt-injection text classifier is scoped off the READ surface — a
    legitimate agent-supplied search term like ``import os`` or ``eval(`` must not
    throw out of the query path (it would previously raise OutputRejectedError).
    The read surface is protected by the real HTTP controls (control-char
    rejection, protected-header set, SSRF/allowlist) which the dedicated guard
    tests above exercise.
    """

    class _RejectInjectionGuard(SecurityGuard):
        def __init__(self) -> None:
            super().__init__(validate_url=self._noop_validate, filter_strings=self._reject)

        @staticmethod
        async def _noop_validate(_url: str) -> None:
            return None

        @staticmethod
        def _reject(values: list[str], _resource: str) -> None:
            for value in values:
                for trigger in ("import os", "eval(", "ignore previous instructions"):
                    if trigger in value:
                        raise ValueError(f"rejected injection term: {trigger}")

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": {"items": [{"id": 1}, {"id": 2}]}})

    c = RestConnector(
        {
            "base_url": "https://api.example.com",
            "path": "/search",
            "params": {"q": "{{ q }}"},
            "records_path": "data.items",
        },
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_RejectInjectionGuard(),
    )
    result = asyncio_run(c.query(ConnectorQuery(resource="default", filters={"q": "import os"})))
    assert captured["params"].get("q") == "import os"
    assert len(result.records) == 2
    assert [r["id"] for r in result.records] == [1, 2]


# ── Write ───────────────────────────────────


def test_write_posts_json_and_returns_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.read() or request.content
        return httpx.Response(200, json={"method": request.method, "name": json.loads(raw)["name"]})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"name": "{{ name }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert_write_result_shape(result)
    assert result["method"] == "POST"
    assert result["name"] == "Ada"


# ── Health check ─────────────────────────────────


def test_health_check_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True


def test_health_check_api_key_in_query_sends_creds() -> None:
    """health_check must send query-mode api_key creds (not a bare, unauthenticated probe)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "api_key", "api_key": "the-secret", "in": "query", "query_param_name": "api_key"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True
    assert captured["params"] == {"api_key": "the-secret"}


# ── ConnectorHub multi-field creds round-trip ───────────────────────


def test_hub_ciphertext_round_trips_multi_field_json_dict() -> None:
    """A JSON-dict credentials_ciphertext round-trips as multi-field creds (REST)."""
    from cryptography.fernet import Fernet

    from modulo.core.connector_hub import ConnectorHub
    from modulo.core.secrets_backend import create_secrets_backend

    key = Fernet.generate_key().decode()
    creds = {"auth_mode": "bearer", "token": "secret-token"}
    ciphertext = Fernet(key.encode()).encrypt(json.dumps(creds).encode())

    class _CI:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.connector_type_id = "rest"
            self.config_json = {"base_url": "https://api.example.com", "path": "/items"}
            self.credentials_ciphertext = ciphertext
            self.visibility = "org"
            self.allowed_operations = None

    ci = _CI()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = key
        hub = ConnectorHub(secrets_backend=backend)
        asyncio_run(hub.initialise([ci]))

    connector = hub.get(ci.id)
    assert isinstance(connector._inner, RestConnector)
    assert connector.connector_type is ConnectorType.REST
    assert connector._inner._auth["mode"] == "bearer"
    assert connector._inner._auth["token"] == "secret-token"


def test_hub_teardown_closes_connector_client() -> None:
    """ConnectorHub async teardown must close held connectors' pooled clients (FAR-408)."""
    from cryptography.fernet import Fernet

    from modulo.core.connector_hub import ConnectorHub
    from modulo.core.secrets_backend import create_secrets_backend

    key = Fernet.generate_key().decode()
    creds = {"auth_mode": "bearer", "token": "secret-token"}
    ciphertext = Fernet(key.encode()).encrypt(json.dumps(creds).encode())

    class _CI:
        def __init__(self) -> None:
            self.id = uuid.uuid4()
            self.connector_type_id = "rest"
            self.config_json = {"base_url": "https://api.example.com", "path": "/items"}
            self.credentials_ciphertext = ciphertext
            self.visibility = "org"
            self.allowed_operations = None

    ci = _CI()
    backend = create_secrets_backend(fernet_key=key, backend_name="fernet")
    with (
        patch.object(backend, "get_secret", side_effect=KeyError(str(ci.id))),
        patch("modulo.settings.get_settings") as get_settings,
    ):
        get_settings.return_value.fernet_key = key

        async def scenario() -> None:
            hub = ConnectorHub(secrets_backend=backend)
            await hub.initialise([ci])
            connector = hub.get(ci.id)._inner
            assert isinstance(connector, RestConnector)
            client = connector._client()
            assert client.is_closed is False
            await hub.__aexit__(None, None, None)
            assert client.is_closed is True

        asyncio_run(scenario())


def test_health_check_sends_credentials_and_validates() -> None:
    """health_check must send creds (not a bare probe) and validate the target."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "allowed_hosts": ["api.example.com"],
        },
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is True
    assert captured.get("authorization") == "Bearer sec-token"


def test_health_check_reports_bad_status_as_unok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "sec-token"},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is False


def test_api_key_query_rendered_param_collision_rejected() -> None:
    """A rendered/config param must never shadow the api_key credential param."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "params": {"api_key": "{{ v }}"}},
        {"auth_mode": "api_key", "api_key": "k456", "in": "query", "query_param_name": "api_key"},
    )
    with pytest.raises(ValueError, match="collides with the api_key credential"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_api_key_query_always_overrides_rendered_param() -> None:
    """A rendered param of a DIFFERENT name must never shadow the credential param."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "params": {"api_key": "rendered"}},
        {"auth_mode": "api_key", "api_key": "the-secret", "in": "query", "query_param_name": "auth_token"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert captured["params"]["api_key"] == "rendered"
    assert captured["params"]["auth_token"] == "the-secret"


def test_three_xx_is_a_distinct_error() -> None:
    """3xx (redirects not followed) must surface as an error, not a passthrough record."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, text="", headers={"location": "https://api.example.com/new"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="location"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_three_xx_carries_status_location_and_retry_after_metadata() -> None:
    """A 3xx must surface a typed RESTStatusError carrying status_code, location
    and Retry-After metadata (not just a redacted message) so an operator can
    reconcile exactly which redirect led to the failure (FAR-408 product map)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307,
            text="redirecting",
            headers={"location": "https://api.example.com/v2/items", "Retry-After": "5"},
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTStatusError) as exc_info:
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    exc = exc_info.value
    assert exc.status_code == 307
    assert exc.location == "https://api.example.com/v2/items"
    assert exc.retry_after == 5.0


def test_config_rejects_unsupported_http_method() -> None:
    """An operation method outside the GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS
    allowlist must be rejected with an actionable ValueError at config time."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "operations": {"default": {"method": "TRACE", "path": "/items"}},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match=r"TRACE.*not allowed"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_config_rejects_verbatim_unsupported_top_level_method() -> None:
    """The top-level method path (non-operations config) is swept by the same
    allowlist — a CONNECT verb is rejected, never emitted."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "method": "CONNECT"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match=r"CONNECT.*not allowed"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_query_honours_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"items": [{"id": i} for i in range(5)]}})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default", limit=2)))
    assert len(result.records) == 2
    assert result.total == 2


def test_query_rejects_non_none_cursor() -> None:
    """Pagination is response-driven — a direct start cursor is not supported."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="pagination is response-driven"):
        asyncio_run(c.query(ConnectorQuery(resource="default", cursor="abc")))


def test_query_surfaces_next_cursor_from_response() -> None:
    """A next_cursor_path JMESPath expression surfaces the response's next_cursor
    on the result so the caller can page forward response-driven (FAR-408)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"items": [{"id": 1}], "paging": {"next_cursor": "page-2"}}},
        )

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "records_path": "data.items",
            "next_cursor_path": "data.paging.next_cursor",
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert len(result.records) == 1
    assert result.next_cursor == "page-2"


def test_query_next_cursor_none_without_path() -> None:
    """No next_cursor_path configured → next_cursor stays None, not a spurious value."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "records_path": "data.items",
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"items": [{"id": 1}]}}))
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert result.next_cursor is None


def test_query_passthrough_forces_wrap_for_json() -> None:
    """passthrough=True forces a single-record wrap even for a JSON body."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [1, 2, 3]}))
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert len(result.records) == 1
    assert '"data"' in result.records[0]["body"]


def test_query_xml_passthrough_yields_uniform_record() -> None:
    """An XML (non-JSON) body with no records_path yields the uniform
    {body, content_type, status_code, headers} passthrough record — the
    UNTESTED-for-XML leg of the raw/passthrough content-type behaviour."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<root><item>1</item></root>",
            headers={"content-type": "application/xml", "x-request-id": "req-1"},
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 1
    record = result.records[0]
    assert record["body"] == "<root><item>1</item></root>"
    assert record["content_type"] == "application/xml"
    assert record["status_code"] == 200
    assert record["headers"]["x-request-id"] == "req-1"


def test_query_whole_object_single_record_fallback() -> None:
    """A top-level JSON object (no records_path) falls back to a whole-object
    single-record wrap so the result always stays a list-of-dicts (FAR-408)."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/status"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "revision": 3}))
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert_result_shape(result)
    assert len(result.records) == 1
    assert result.records[0] == {"ok": True, "revision": 3}
    assert result.total == 1


def test_retry_get_retries_on_429_then_succeeds() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert result.metadata["status_code"] == 200
    assert len(attempts) == 3


def test_write_does_not_retry_non_idempotent() -> None:
    """A POST without an idempotency header is a single attempt, never retried."""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"name": "{{ name }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="429"):
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert len(attempts) == 1


def test_retry_delay_honours_retry_after() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    exc = RESTStatusError("boom", status_code=429, retry_after=2.5)
    assert c._retry_delay(exc, 0) == 2.5
    assert c._retry_delay(RESTStatusError("boom", status_code=429, retry_after=None), 1) >= 1.0


def test_retry_delay_caps_huge_retry_after() -> None:
    """An untrusted Retry-After header must be capped so a server cannot make us sleep ~1h."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    exc = RESTStatusError("boom", status_code=429, retry_after=3600)
    assert c._retry_delay(exc, 0) == 30.0
    assert c._retry_delay(exc, 3) == 30.0


def test_dot_index_records_path_rejected() -> None:
    """Legacy dot-index records_path must fail loud instead of being silently coerced."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "records_path": "data.items.0"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"items": [{"id": 1}]}}))
    with pytest.raises(ValueError, match=r"\[0\]"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_response_size_cap_aborts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 500, headers={"content-type": "text/plain"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "max_response_size": 50},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTResponseTooLargeError, match="too large"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_transport_errors_are_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTConnectError, match="transport error"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


# ── Transport config wiring (timeout_seconds / verify_tls) ────────────────


def test_timeout_seconds_in_config_overrides_default() -> None:
    """config_json timeout_seconds must override the constructor default (FAR-412)."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "timeout_seconds": 12.0},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c._client().timeout == httpx.Timeout(12.0)
    assert c._timeout == 12.0


def test_default_timeout_is_30_seconds() -> None:
    """Absent timeout_seconds keeps the default (30.0), not 0 or None (FAR-412)."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c._client().timeout == httpx.Timeout(30.0)
    assert c._timeout == 30.0


@pytest.mark.parametrize(
    ("verify_tls_config", "expected_verify"),
    [
        ({"verify_tls": False}, False),
        ({}, True),
    ],
)
def test_verify_tls_passes_through_to_client(
    monkeypatch: pytest.MonkeyPatch,
    verify_tls_config: dict[str, Any],
    expected_verify: bool,
) -> None:
    """config_json verify_tls must reach the AsyncClient as verify=... (FAR-412)."""
    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("modulo.connectors.rest.httpx.AsyncClient", factory)
    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items", **verify_tls_config},
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(_default_handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
    )
    c._client()
    assert captured.get("verify") is expected_verify


# ── Fan-out / iterator (FAR-411) ───────────────────────────────────────────


def test_write_fanout_sequentially_emits_each_item() -> None:
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}", "idx": "{{ item_index }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(
        c.write(ConnectorPayload(resource="default", data={"items": [{"name": "Ada"}, {"name": "Grace"}]}))
    )

    assert result["fanout"] is True
    assert result["total"] == 2
    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert result["cardinality_over_cap"] is False
    assert len(captured) == 2
    assert captured[0] == {"name": "Ada", "idx": "0"}
    assert captured[1] == {"name": "Grace", "idx": "1"}
    assert [o["status"] for o in result["outcomes"]] == ["success", "success"]
    assert result["outcomes"][0]["item_summary"] == "{'name': 'Ada'}"
    assert result["outcomes"][1]["item_summary"] == "{'name': 'Grace'}"


def test_write_fanout_sized_cardinality_exceeded_fails_closed() -> None:
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"n": "{{ n }}"},
            "fan_out": {"enabled": True, "items_path": "items", "max_cardinality": 2},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTCardinalityExceededError, match="exceeding max_cardinality") as exc:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": [{"n": 1}, {"n": 2}, {"n": 3}]})))
    # Fail-closed: zero partial emit — nothing hit the wire.
    assert len(requests) == 0
    assert exc.value.source_cardinality == 3
    assert exc.value.fanout_capacity == 2
    assert exc.value.lazy is False
    assert exc.value.cardinality_over_cap is True


def test_write_fanout_lazy_generator_cardinality_exceeded_fails_closed() -> None:
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"n": "{{ n }}"},
            "fan_out": {"enabled": True, "items_path": "items", "max_cardinality": 3},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)

    def gen() -> Any:
        for i in range(10):
            yield {"n": i}

    with pytest.raises(RESTCardinalityExceededError, match="lazy source") as exc:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": gen()})))
    assert len(requests) == 0
    assert exc.value.lazy is True
    assert exc.value.source_cardinality is None
    assert exc.value.cardinality_over_cap is True


def test_write_fanout_empty_iterator_vacuous_success() -> None:
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"n": "{{ n }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": []})))
    assert result["fanout"] is True
    assert result["total"] == 0
    assert result["success_count"] == 0
    assert result["failure_count"] == 0
    assert not result["outcomes"]
    assert len(requests) == 0


def test_write_fanout_items_path_unresolved_is_vacuous_success() -> None:
    requests: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(1)
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"n": "{{ n }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"other": 1})))
    assert result["total"] == 0
    assert len(requests) == 0


def test_write_fanout_per_item_failure_fails_node_with_outcomes() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("name") == "fail":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTFanOutFailureError, match="fan-out failed at item 1") as exc:
        asyncio_run(
            c.write(
                ConnectorPayload(
                    resource="default",
                    data={"items": [{"name": "ok"}, {"name": "fail"}, {"name": "never"}]},
                )
            )
        )
    # Sequential abort: the 3rd item was never attempted.
    assert len(calls) == 2
    assert exc.value.failed_index == 1
    assert exc.value.failed_item == "{'name': 'fail'}"
    assert exc.value.success_count == 1
    assert exc.value.failure_count == 1
    assert exc.value.outcomes[0]["status"] == "success"
    assert exc.value.outcomes[1]["status"] == "failure"
    assert "boom" in exc.value.failed_error or "500" in exc.value.failed_error


def test_write_fanout_applies_per_destination_rate_limit() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
            "rate_limit": {"requests_per_second": 1000.0, "burst": 10},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(
        c.write(ConnectorPayload(resource="default", data={"items": [{"name": f"n{i}"} for i in range(5)]}))
    )
    assert result["total"] == 5
    assert result["success_count"] == 5
    assert len(calls) == 5
    # One bucket per destination (host), lazily created on first send.
    assert len(c._rate_buckets) == 1


# ── Fan-out: configured retry budget (FAR-411) ───────────────────────


def test_write_fanout_honors_configured_max_retries():
    """fan_out.max_retries governs retry attempts on the fan-out path.

    With max_retries=0 a retryable GET must be a SINGLE attempt (no retry) even
    though the default connector loop would otherwise run 3 attempts.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "operations": {"default": {"method": "GET", "path": "/users"}},
            "fan_out": {"enabled": True, "items_path": "items", "max_retries": 0},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTFanOutFailureError) as exc:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": [{"name": "a"}]})))
    assert "429" in exc.value.failed_error
    assert len(calls) == 1


def test_write_fanout_default_retries_still_three_attempts():
    """Absent max_retries keeps the default connector budget (2 retries = 3 attempts)."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "operations": {"default": {"method": "GET", "path": "/users"}},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": [{"name": "a"}]})))
    assert result["success_count"] == 1
    assert len(calls) == 3


# ── Fan-out: token-per-attempt metering (FAR-411) ──────────────────────


def test_send_acquires_token_per_retry_attempt(monkeypatch):
    """Each wire attempt (including retries) consumes a fresh rate-limit token."""
    counts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "rate_limit": {"requests_per_second": 1000.0, "burst": 10},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    original = RestConnector._acquire_rate_token

    async def counting(self, destination: str, *, deadline_seconds: float | None = None) -> None:
        counts.append(1)
        await original(self, destination, deadline_seconds=deadline_seconds)

    monkeypatch.setattr(RestConnector, "_acquire_rate_token", counting)
    with pytest.raises(RESTStatusError, match="429"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    # GET is retryable: 3 attempts (default 2 retries + 1), each metered.
    assert len(counts) == 3


# ── Fan-out: bounded rate-limit wait (FAR-411) ───────────────────────


def test_write_fanout_rate_limit_wait_is_bounded():
    """A saturated per-destination bucket must time out rather than spin forever."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items", "per_item_timeout": 0.001},
            "rate_limit": {"requests_per_second": 1.0, "burst": 1},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with pytest.raises(RESTFanOutFailureError, match="rate-limit wait exceeded") as exc:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": [{"name": "a"}, {"name": "b"}]})))
    assert "rate-limit wait exceeded" in exc.value.failed_error


def test_rate_limit_timeout_is_typed_error():
    """The bounded wait raises a typed RESTRateLimitTimeoutError on a saturated bucket."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "rate_limit": {"requests_per_second": 1.0, "burst": 1},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    # Drain the only token so a follow-up acquire must wait past the deadline.
    bucket = TokenBucket(rate=1.0, burst=1)
    c._rate_buckets["https://api.example.com"] = bucket
    asyncio.run(bucket.consume())

    with pytest.raises(RESTRateLimitTimeoutError, match="rate-limit wait exceeded"):
        asyncio.run(c._acquire_rate_token("https://api.example.com", deadline_seconds=0.001))


# ── Fan-out: cancellation preserves partial outcomes (FAR-411) ───────────────


def test_write_fanout_cancellation_preserves_partial_outcomes(monkeypatch):
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    original = RestConnector._execute
    call_count = 0

    async def cancelling(self, request, *, surface, request_timeout=None, max_retries=None):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise asyncio.CancelledError
        return await original(self, request, surface=surface, request_timeout=request_timeout, max_retries=max_retries)

    monkeypatch.setattr(RestConnector, "_execute", cancelling)
    with pytest.raises(RESTFanOutCancelledError) as exc:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": [{"name": "a"}, {"name": "b"}]})))
    assert exc.value.success_count == 1
    assert len(exc.value.outcomes) == 1
    assert exc.value.outcomes[0]["status"] == "success"


# ── Fan-out: max_cardinality validation (FAR-411) ──────────────────────


@pytest.mark.parametrize("bad", [0, -1, 100_001])
def test_fanout_max_cardinality_rejects_invalid(bad):
    """max_cardinality must be >= 1 and capped — reject zero/negative/huge."""
    with pytest.raises(ValueError, match="max_cardinality"):
        RestConnector(
            {
                "base_url": "https://api.example.com",
                "path": "/x",
                "fan_out": {"items_path": "items", "max_cardinality": bad},
            },
            {"auth_mode": "bearer", "token": "t"},
        )


def test_fanout_max_cardinality_cap_boundary_accepted():
    """The cap boundary (100_000) is accepted; it is not silently clamped by an off-by-one."""
    c = RestConnector(
        {
            "base_url": "https://api.example.com",
            "path": "/x",
            "fan_out": {"items_path": "items", "max_cardinality": 100_000},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c._max_fanout_cardinality == 100_000


# ── Fan-out: outcome redaction (FAR-411) ─────────────────────────


def test_fanout_outcome_redacts_credential_bearing_item():
    """Outcome item_summary must redact a credential-bearing item payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "super-secret-token"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(
        c.write(ConnectorPayload(resource="default", data={"items": [{"name": "super-secret-token"}]}))
    )
    summary = result["outcomes"][0]["item_summary"]
    assert "super-secret-token" not in summary
    assert "***" in summary


def test_fanout_failure_redacts_failed_item():
    """RESTFanOutFailureError.failed_item must be a redacted summary, not the raw item."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "fan_out": {"enabled": True, "items_path": "items"},
        },
        {"auth_mode": "bearer", "token": "super-secret-token"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTFanOutFailureError) as exc:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"items": [{"name": "super-secret-token"}]})))
    assert "super-secret-token" not in exc.value.failed_item
    assert "***" in exc.value.failed_item


# ── FAR-413: SSRF adversarial via the connector (real guard) ────────────────


def _real_ssrf_guard() -> SecurityGuard:
    """A guard bound to the real ``modulo.core.ssrf`` validator (as the hub does)."""
    from modulo.core.ssrf import validate_outbound_url_async

    async def validate_url(url: str) -> None:
        await validate_outbound_url_async(url)

    def filter_strings(values: list[str], resource: str) -> None:
        return None

    return SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)


def test_runtime_loopback_blocked_by_real_guard() -> None:
    """Sibling regression: loopback/metadata MUST stay blocked at runtime even when
    ``verify_tls`` is disabled (the SSRF guard is independent of TLS verification)."""
    c = RestConnector(
        {"base_url": "http://127.0.0.1", "path": "/admin", "verify_tls": False},
        {"auth_mode": "bearer", "token": "t"},
        security_guard=_real_ssrf_guard(),
    )
    with pytest.raises(ValueError, match="private/internal"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_connector_blocks_decimal_ipv4_loopback_via_ssrf() -> None:
    """Decimal/hex integer loopback encodings must be rejected at request build."""
    for base_url in ("http://2130706433/", "http://0x7f000001/"):
        c = RestConnector(
            {"base_url": base_url, "path": "/admin"},
            {"auth_mode": "bearer", "token": "t"},
            security_guard=_real_ssrf_guard(),
        )
        with pytest.raises(ValueError, match=r"decimal/octal integer IP literal|hex-encoded IP literal"):
            asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_redirect_to_internal_is_not_followed() -> None:
    """A 3xx with a Location header pointing at an internal host must surface as
    an error — never be followed (follow_redirects=False in the client)."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, text="", headers={"location": "http://127.0.0.1/evil"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(ValueError, match="location"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    # Only the external target was ever touched — the redirect was not followed.
    assert len(requests) == 1
    assert requests[0].startswith("https://api.example.com")


def test_dns_rebind_revalidates_per_request() -> None:
    """Each query() re-validates the target — no cached resolution is carried
    between the validation and the transport, so a hostname that rebinds to an
    internal address is caught on the second query."""
    validated: list[str] = []

    async def validate_url(url: str) -> None:
        validated.append(url)

    def filter_strings(values: list[str], resource: str) -> None:
        return None

    guard = SecurityGuard(validate_url=validate_url, filter_strings=filter_strings)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        security_guard=guard,
    )
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert len(validated) == 2  # one rebuild per request — revalidated, not cached


# ── FAR-413: injected clock / timing (FAR-320 flake lesson) ─────────────────


def test_retry_uses_injected_sleep_not_wall_clock() -> None:
    """The retry backoff must sleep on the injected ``sleep`` seam — never the
    real ``asyncio.sleep``. A frozen clock + fake sleep makes the timing
    deterministic, so this test cannot flake on wall-clock jitter (FAR-320)."""
    sleeps: list[float] = []
    attempts: list[int] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
        sleep=fake_sleep,
        clock=lambda: 0.0,
    )
    with patch("asyncio.sleep", side_effect=AssertionError("must not use wall clock")):
        result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert result.metadata["status_code"] == 200
    assert len(attempts) == 3
    # Two retries, each with Retry-After:0 -> zero delay, in deterministic order.
    assert sleeps == [0.0, 0.0]


def test_retry_uses_injected_random_for_deterministic_backoff() -> None:
    """The backoff jitter must come from the injected ``random_uniform`` seam so
    the exact backoff schedule is assertable — previously the module-level
    ``random.uniform`` jittered the delay even though the wait was otherwise
    deterministic, defeating the injected ``sleep`` seam."""
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        ssrf_validator=lambda url: None,
        security_guard=_noop_guard(),
        sleep=fake_sleep,
        random_uniform=lambda a, b: 0.1,
    )
    with pytest.raises(RESTConnectError):
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    # attempt 0 fails -> sleep _backoff(0)=0.5*1+0.1=0.6;
    # attempt 1 fails -> sleep _backoff(1)=0.5*2+0.1=1.1.
    assert sleeps == [0.6, 1.1]


# ── FAR-413: idempotency-header behaviour ───────────────────────────────────


def test_idempotency_header_is_valid_uuid() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.headers))
        return httpx.Response(200, json={})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "idempotency_header": "X-Idempotency-Key",
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    key = captured["x-idempotency-key"]
    parsed = uuid.UUID(key)
    # A bare UUID v4 — uniquely identifies this attempt; never carries a run_id
    # fragment or any path segment (it is a single 36-char canonical UUID).
    assert parsed.version == 4
    assert len(key) == 36
    assert key.count("-") == 4


def test_mutating_verb_with_idempotency_header_retries() -> None:
    """A POST with a declared idempotency header is safe to retry on 429."""
    attempts: list[int] = []
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        keys.append(dict(request.headers).get("x-idempotency-key", ""))
        if len(attempts) < 2:
            return httpx.Response(429, text="throttled", headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async def fake_sleep(delay: float) -> None:
        return None

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "idempotency_header": "X-Idempotency-Key",
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    c._sleep = fake_sleep
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert result["ok"] is True
    assert len(attempts) == 2
    # The SAME idempotency key is reused across the retry — the retry-dedup
    # guarantee (a server can suppress the repeated delivery). Deterministic
    # run+node+index persistence is a pipeline-level concern (FAR-410), not the
    # connector, so a fresh uuid is minted per logical request instead.
    assert keys[0] == keys[1]
    assert keys[0]


def test_idempotency_header_is_distinct_across_writes() -> None:
    """Two distinct write() calls mint distinct idempotency keys."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.headers).get("x-idempotency-key", ""))
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/users",
            "body": {"name": "{{ name }}"},
            "idempotency_header": "X-Idempotency-Key",
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Grace"})))
    assert len(set(captured)) == 2


# ── FAR-413: UNKNOWN / terminal outcomes ─────────────────────────


def test_write_timeout_is_typed_rest_connect_error_not_crash() -> None:
    """A write-timeout surfaces as a typed RESTConnectError (a ValueError), never
    a raw ``httpx`` exception escaping to the caller — the "no hard-fail / no
    crash on an indeterminate write" invariant."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.WriteTimeout("write timed out")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"name": "{{ name }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTConnectError) as exc_info:
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"name": "Ada"})))
    assert isinstance(exc_info.value, ValueError)
    assert "transport error" in str(exc_info.value)


def test_retry_exhaust_on_5xx_surfaces_status_error() -> None:
    """A GET that keeps 5xx-ing exhausts the retry budget and surfaces a typed
    status error (the terminal FAILED-like outcome), never silently passing."""
    attempts: list[int] = []

    async def fake_sleep(delay: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500, text="boom")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    c._sleep = fake_sleep
    with pytest.raises(RESTStatusError) as exc_info:
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert exc_info.value.status_code == 500
    assert len(attempts) == 3  # 1 initial + 2 retries


# ── FAR-413: response-size abort releases the stream ─────────────────────


def test_response_size_abort_connector_still_usable() -> None:
    """After an abort the connector object remains usable — a follow-up request
    on the SAME instance succeeds. (This narrows the previous stream-release
    claim: MockTransport has no connection pool, so asserting 'the stream was
    released' would be trivially true and unproveable in this harness.)"""
    big = [False]

    def handler(request: httpx.Request) -> httpx.Response:
        if big[0]:
            return httpx.Response(200, text="x" * 2000, headers={"content-type": "text/plain"})
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "max_response_size": 50},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)

    big[0] = True
    with pytest.raises(RESTResponseTooLargeError, match="too large"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))

    big[0] = False
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert result.metadata["status_code"] == 200  # the same connector still works


def test_response_size_abort_on_chunked_response_without_content_length() -> None:
    """A chunked (unknown-length) response with NO Content-Length must be capped
    in the ``aiter_bytes()`` accumulation loop — the overflow path that the
    Content-Length pre-check can never reach. We strip the auto-inserted
    Content-Length so the streaming loop is genuinely exercised."""

    def handler(request: httpx.Request) -> httpx.Response:
        resp = httpx.Response(200, content=b"x" * 2000, headers={"transfer-encoding": "chunked"})
        resp.headers.pop("content-length", None)
        return resp

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "max_response_size": 50},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTResponseTooLargeError, match="too large"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


# ── FAR-413: redaction negative tests ───────────────────────────────────────


def test_no_secret_in_transport_error_detail() -> None:
    secret = "super-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"connect failed for {secret}")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTConnectError) as exc_info:
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert secret not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_no_secret_in_status_error_detail() -> None:
    secret = "hunter2-did-not-forget"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"invalid token {secret}")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    with pytest.raises(RESTStatusError) as exc_info:
        asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert secret not in str(exc_info.value)


def test_no_secret_in_health_check_detail() -> None:
    secret = "api-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad key: {secret}")

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    health = asyncio_run(c.health_check())
    assert health.ok is False
    assert secret not in health.detail


def test_recorded_request_headers_do_not_leak_auth() -> None:
    """A passthrough record captures RESPONSE headers (display-only data), never
    the request Authorization — so a secret can't be replayed or parsed back out."""
    secret = "bearer-token-xyz"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="raw", headers={"content-type": "text/plain", "server": "echo"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    record = result.records[0]
    assert record["content_type"] == "text/plain"
    assert secret not in json.dumps(record)
    # The record is plain data (a display-only snapshot), never an executable replay.
    assert isinstance(record["headers"], dict)
    assert record["headers"].get("server") == "echo"


# ── FAR-518: success-path response redaction (server-reflected credential) ───


def test_write_result_redacts_reflected_credential_in_body() -> None:
    """A write response whose JSON body echoes the bearer token is redacted from
    the returned result — a server-reflected credential must never persist into the
    run result / node output."""
    secret = "echo-me-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "echo": secret, "nested": {"auth": f"Bearer {secret}"}},
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={})))
    assert_write_result_shape(result)
    assert secret not in json.dumps(result)
    assert "***" in json.dumps(result)


def test_write_result_redacts_credential_in_plain_body() -> None:
    """A write response whose non-JSON raw body echoes the token is redacted (the
    non-JSON ``body``/``content_type`` branch of ``_write_result``)."""
    secret = "plain-body-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"ack {secret}", headers={"content-type": "text/plain"})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={})))
    assert_write_result_shape(result)
    assert secret not in json.dumps(result)
    assert "***" in json.dumps(result)


def test_passthrough_record_redacts_reflected_credential_in_body_and_headers() -> None:
    """A passthrough read whose body AND a response header both echo the bearer
    token are redacted from the returned record (header + body reflection)."""
    secret = "echo-header-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f"body {secret}",
            headers={"content-type": "text/plain", "x-echo": secret},
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    record = result.records[0]
    assert secret not in json.dumps(record)
    assert "***" in record["body"]
    assert "***" in record["headers"]["x-echo"]


def test_read_result_redacts_query_secret_url_echoed_in_body() -> None:
    """A read response that echoes the request URL (which carries the api_key as
    a query-param credential) is redacted from the returned record — the URL's
    query secret is a credential value and must be stripped wherever it appears."""
    secret = "query-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": [{"request_url": str(request.url)}]})

    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "path": "/items",
            "records_path": "records",
        },
        {"auth_mode": "api_key", "api_key": secret, "in": "query", "query_param_name": "api_key"},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    assert secret not in json.dumps(result.records)
    assert "***" in json.dumps(result.records)


def test_read_metadata_url_redacts_credential_rendered_into_path() -> None:
    """``_transform`` metadata's ``url`` (built from ``request.url``) is redacted
    when the credential value is rendered into the request URL — a query/path
    secret carried in the URL must never be persisted into the run result."""
    secret = "url-embedded-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/res/{{ q }}"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default", filters={"q": secret})))
    assert secret not in json.dumps(result.metadata)
    assert "***" in json.dumps(result.metadata)


def test_write_result_leaves_legit_response_without_reflected_credential_unchanged() -> None:
    """A legit response that never reflects a credential is returned unchanged —
    value-based redaction only strips actual credential values, never legitimate
    response content."""
    secret = "unused-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "data": {"name": "demo", "count": 3}})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={})))
    assert_write_result_shape(result)
    assert result == {"ok": True, "data": {"name": "demo", "count": 3}}


# ── FAR-518: basic-auth base64 blob redaction (server-reflected Authorization) ─


def test_basic_auth_reflected_authorization_body_is_redacted() -> None:
    """A write response whose body reflects the request's basic-auth Authorization
    (the ``Basic <b64>`` header value and the decodable base64 blob) is redacted —
    the SQL-less, decodable user:pass must never persist into the run result."""
    username = "svc-user"
    password = "svc-pass"
    b64 = base64.b64encode(f"{username}:{password}".encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "echo_auth": f"Basic {b64}", "echo_blob": b64},
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users"},
        {"auth_mode": "basic", "username": username, "password": password},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={})))
    assert_write_result_shape(result)
    assert result["ok"] is True
    assert b64 not in json.dumps(result)
    assert password not in json.dumps(result)
    assert "***" in json.dumps(result)


def test_basic_auth_reflected_authorization_header_is_redacted() -> None:
    """A passthrough read whose RESPONSE header echoes the request's basic-auth
    ``Authorization: Basic <b64>`` value is redacted from the returned record —
    the base64 blob (decodable user:pass) must never persist into node output."""
    username = "svc-user"
    password = "svc-pass"
    b64 = base64.b64encode(f"{username}:{password}".encode()).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="ack",
            headers={"content-type": "text/plain", "x-echo-auth": f"Basic {b64}"},
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "passthrough": True},
        {"auth_mode": "basic", "username": username, "password": password},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.query(ConnectorQuery(resource="default")))
    record = result.records[0]
    assert b64 not in json.dumps(record)
    assert password not in json.dumps(record)
    assert "***" in record["headers"]["x-echo-auth"]


# ── FAR-518: no over-redaction for short, word-like credentials ──────────────


def test_short_credential_does_not_mangle_legit_words() -> None:
    """A short, word-like credential (password ``data``) is redacted ONLY as a
    standalone credential-like value, never as a substring of a normal word —
    legit response content that merely contains the substring is untouched
    (FAR-518 no over-redaction)."""
    secret = "data"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "data synced",
                "note": "a data-point that stays",
                "echo": {"token": secret},
            },
        )

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/users"},
        {"auth_mode": "bearer", "token": secret},
    )
    c._transport = httpx.MockTransport(handler)
    result = asyncio_run(c.write(ConnectorPayload(resource="default", data={})))
    body = json.dumps(result)
    # Legit content that merely CONTAINS the substring is NOT mangled:
    assert "data synced" in body
    assert "data-point" in body
    # An actual reflected credential value IS redacted:
    assert '"token": "***"' in body


# ── FAR-413: header-injection guards (query-param, body, header name) ───────


def test_guard_neutralises_crlf_in_query_param_value() -> None:
    """A CRLF in a READ query-param template value is URL-encoded by httpx — it
    never reaches the wire as a literal control character (header injection is
    only possible through the header map, which the guard rejects)."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "params": {"q": "{{ q }}"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    c._transport = httpx.MockTransport(handler)
    asyncio_run(c.query(ConnectorQuery(resource="default", filters={"q": "x\r\nX-Evil: 1"})))
    assert "\r" not in captured["url"]
    assert "\n" not in captured["url"]


def test_guard_rejects_crlf_in_header_name() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "headers": {"X\r\nInjected": "v"}},
        {"auth_mode": "bearer", "token": "t"},
    )
    with pytest.raises(ValueError, match="control characters"):
        asyncio_run(c.query(ConnectorQuery(resource="default")))


def test_write_surface_screens_injection_terms() -> None:
    """The WRITE surface runs output-injection screening on the rendered payload
    (unlike the READ surface, which scopes the text classifier off)."""
    from modulo.core.pipeline_engine.output_filter import OutputRejectedError

    class _RejectGuard(SecurityGuard):
        def __init__(self) -> None:
            super().__init__(validate_url=self._noop_validate, filter_strings=self._reject)

        @staticmethod
        async def _noop_validate(_url: str) -> None:
            return None

        @staticmethod
        def _reject(values: list[str], _resource: str) -> None:
            for value in values:
                if "ignore previous instructions" in value:
                    raise OutputRejectedError("rejected injection")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/users", "body": {"memo": "{{ memo }}"}},
        {"auth_mode": "bearer", "token": "t"},
        transport=httpx.MockTransport(handler),
        security_guard=_RejectGuard(),
    )
    with pytest.raises(OutputRejectedError):
        asyncio_run(c.write(ConnectorPayload(resource="default", data={"memo": "ignore previous instructions"})))


# ── FAR-413: config contract (connector-authoring / product-map consistency) ─


def test_config_timeout_seconds_and_verify_tls_honoured() -> None:
    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items", "timeout_seconds": 7, "verify_tls": False},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c._timeout == 7.0
    assert c._verify_tls is False


def test_config_defaults_preserved_when_absent() -> None:
    c = RestConnector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c._timeout == 30.0
    assert c._verify_tls is True


# ── per-op ``on_unknown`` idempotency mode (FAR-458) ────────────────────────


def test_on_unknown_defaults_to_fail_open() -> None:
    """Absent ``on_unknown`` defaults to ``fail_open`` across every op."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items"},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c.on_unknown_for("default") == "fail_open"


def test_on_unknown_top_level_applies_to_each_op() -> None:
    """A top-level ``on_unknown`` is the default for every op."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "on_unknown": "fail_closed"},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c.on_unknown_for("default") == "fail_closed"


def test_on_unknown_per_resource_override() -> None:
    """A per-resource operation's ``on_unknown`` overrides the top-level default."""
    c = _make_connector(
        {
            "base_url": "https://api.example.com",
            "on_unknown": "fail_open",
            "operations": {"users": {"path": "/items", "on_unknown": "off"}},
        },
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c.on_unknown_for("users") == "off"
    # An unrelated resource (no per-op override, but the top-level applies) is fail_open.
    assert c.on_unknown_for("default") == "fail_open"


def test_on_unknown_case_and_whitespace_normalised() -> None:
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "on_unknown": "  Fail_Closed  "},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c.on_unknown_for("default") == "fail_closed"


def test_on_unknown_invalid_top_level_rejected_at_config_parse() -> None:
    """An invalid top-level ``on_unknown`` is a loud config error at construction
    time (config-parse), never silently adopted."""
    with pytest.raises(ValueError, match="on_unknown"):
        _make_connector(
            {"base_url": "https://api.example.com", "path": "/items", "on_unknown": "bogus"},
            {"auth_mode": "bearer", "token": "t"},
        )


def test_on_unknown_invalid_per_resource_rejected_at_config_parse() -> None:
    """An invalid per-resource operation ``on_unknown`` is also rejected at
    config-parse time (fail fast on a config error)."""
    with pytest.raises(ValueError, match="on_unknown"):
        _make_connector(
            {
                "base_url": "https://api.example.com",
                "operations": {"users": {"path": "/items", "on_unknown": "always"}},
            },
            {"auth_mode": "bearer", "token": "t"},
        )


def test_off_bypasses_marker_never_dedupes() -> None:
    """``off`` is the default-free bypass: the write is never deduped. The
    RestConnector surfaces it so the gate can short-circuit before any read."""
    c = _make_connector(
        {"base_url": "https://api.example.com", "path": "/items", "on_unknown": "off"},
        {"auth_mode": "bearer", "token": "t"},
    )
    assert c.on_unknown_for("default") == "off"


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
