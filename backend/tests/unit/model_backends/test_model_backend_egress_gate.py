"""Prove the outbound SSRF gate on the OpenAI-compatible model backends.

Covers both gate call sites added alongside the connector gates:

* ``openai_compatible_health_check`` validates ``base_url`` before probing
  ``/models`` and must return ``HealthResult(ok=False)`` for a blocked target.
* ``OpenAICompatibleBackend.__init__`` validates ``base_url`` and must raise, so
  a backend aimed at an internal address can never be constructed and used.

``pytestmark = pytest.mark.real_ssrf_dns`` opts out of the autouse DNS shim in
``tests/conftest.py``; without it every hostname resolves to a public address and
these assertions would pass for the wrong reason. Blocked targets are literal IPs
(no DNS, hermetic offline) except where the resolver is patched deliberately to
cover the hostname path.

Remove either ``validate_outbound_url*`` call and the matching case here fails.
"""

import pytest

from modulo.core import ssrf
from modulo.model_backends.base import openai_compatible_health_check
from modulo.model_backends.lm_studio import LmStudioBackend
from modulo.model_backends.module import OpenAICompatibleBackend
from modulo.model_backends.ollama import OllamaBackend
from modulo.model_backends.vllm import VllmBackend

pytestmark = pytest.mark.real_ssrf_dns

API_KEY = "test-key"  # nosec B105 - test fixture value, not a credential

BLOCKED_URLS = [
    "http://127.0.0.1:11434/v1",
    "http://10.1.2.3:8000/v1",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]:1234/v1",
]


@pytest.mark.parametrize("blocked_url", BLOCKED_URLS)
async def test_health_check_returns_not_ok_for_blocked_base_url(blocked_url: str) -> None:
    """The health check reports a blocked target instead of probing it."""
    result = await openai_compatible_health_check(base_url=blocked_url, api_key=API_KEY)
    assert result.ok is False
    assert "private/internal" in result.detail


@pytest.mark.parametrize("blocked_url", BLOCKED_URLS)
def test_backend_construction_raises_for_blocked_base_url(blocked_url: str) -> None:
    """A backend pointed at an internal address must not be constructible."""
    with pytest.raises(ValueError, match="private/internal"):
        OpenAICompatibleBackend(api_key=API_KEY, model_id="m", base_url=blocked_url, provider="custom")


@pytest.mark.parametrize("backend_cls", [OllamaBackend, VllmBackend, LmStudioBackend])
def test_localhost_default_backends_blocked_until_allowlisted(backend_cls, monkeypatch) -> None:
    """Localhost-default local backends need the documented dual-stack opt-in.

    ``localhost`` resolves to ``127.0.0.1`` AND ``::1`` on a dual-stack host and
    validation fails closed if any resolved address is blocked, so an IPv4-only
    allowlist is not enough — ``127.0.0.0/8,::1/128`` is what actually works.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["127.0.0.1", "::1"])

    monkeypatch.delenv("SSRF_ALLOW_PRIVATE_RANGES", raising=False)
    with pytest.raises(ValueError, match="private/internal") as exc_info:
        backend_cls(model_id="m")
    message = str(exc_info.value)
    assert "SSRF_ALLOW_PRIVATE_RANGES=127.0.0.0/8,::1/128" in message
    assert "BOTH" in message

    # IPv4-only allowlist still fails closed because ::1 remains blocked.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8")
    with pytest.raises(ValueError, match="private/internal"):
        backend_cls(model_id="m")

    # Documented remediation: both loopback families allowlisted.
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_RANGES", "127.0.0.0/8,::1/128")
    backend = backend_cls(model_id="m")
    assert backend.base_url is not None


async def test_hostname_resolving_to_private_address_is_blocked(monkeypatch) -> None:
    """The DNS path is gated for model backends too, not just literal IPs."""

    async def _private_async(_host: str) -> list[str]:
        return ["192.168.5.5"]

    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["192.168.5.5"])
    monkeypatch.setattr(ssrf, "_resolve_all_async", _private_async)

    result = await openai_compatible_health_check(base_url="http://models.internal.example/v1", api_key=API_KEY)
    assert result.ok is False
    assert "192.168.5.5" in result.detail

    with pytest.raises(ValueError, match="resolves to a private/internal address"):
        OpenAICompatibleBackend(api_key=API_KEY, model_id="m", base_url="http://models.internal.example/v1")


def test_backend_without_base_url_is_unaffected() -> None:
    """Control case: the hosted-provider path has no base_url to validate."""
    backend = OpenAICompatibleBackend(api_key=API_KEY, model_id="gpt-4o-mini", provider="openai")
    assert backend.base_url is None


def test_public_base_url_still_constructs(monkeypatch) -> None:
    """Control case: the gate must not block a legitimate public target."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["93.184.216.34"])
    backend = OpenAICompatibleBackend(
        api_key=API_KEY,
        model_id="m",
        base_url="https://models.example.com/v1",
        provider="custom",
    )
    assert backend.base_url == "https://models.example.com/v1"


def test_hub_custom_base_url_still_goes_through_the_ssrf_gate() -> None:
    """FAR-2046: honouring a custom endpoint does not bypass the pinned SSRF gate."""
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="private/internal"):
        _build_backend(
            "openai",
            "m",
            {"api_key": API_KEY, "base_url": "http://10.1.2.3:8000/v1"},
            {},
        )


# FAR-2046: the non-OpenAI-compatible adapters take their endpoint through a
# provider-specific constructor argument (``anthropic_api_url`` / ``base_url`` /
# ``endpoint``). Each must clear the same construction-time SSRF gate as
# ``OpenAICompatibleBackend`` — otherwise a stored custom endpoint is handed
# straight to the vendor SDK and FAR-512's "an internal-target backend can never
# be constructed and used" invariant only holds on the OpenAI-compatible path.
_ADAPTERS = [
    ("anthropic", "AnthropicBackend", "ChatAnthropic", "anthropic_api_url"),
    ("cohere", "CohereBackend", "ChatCohere", "base_url"),
    ("gemini", "GeminiBackend", "ChatGoogleGenerativeAI", "base_url"),
    ("mistral", "MistralBackend", "ChatMistralAI", "endpoint"),
]


def _adapter(provider: str, class_name: str) -> type:
    module = __import__(f"modulo.model_backends.{provider}", fromlist=[class_name])
    return getattr(module, class_name)  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("provider", "class_name", "_chat_cls", "endpoint_kwarg"), _ADAPTERS, ids=[a[0] for a in _ADAPTERS]
)
@pytest.mark.parametrize("blocked_url", BLOCKED_URLS)
def test_adapter_construction_raises_for_blocked_custom_endpoint(
    provider: str, class_name: str, _chat_cls: str, endpoint_kwarg: str, blocked_url: str
) -> None:
    """A custom internal endpoint must not be constructible on ANY adapter."""
    with pytest.raises(ValueError, match="private/internal"):
        _adapter(provider, class_name)(api_key=API_KEY, model_id="m", **{endpoint_kwarg: blocked_url})


@pytest.mark.parametrize(
    ("provider", "class_name", "_chat_cls", "endpoint_kwarg"), _ADAPTERS, ids=[a[0] for a in _ADAPTERS]
)
def test_adapter_construction_accepts_public_custom_endpoint(
    provider: str, class_name: str, _chat_cls: str, endpoint_kwarg: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control case: the gate must not block a legitimate public custom endpoint."""
    from unittest.mock import patch

    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["93.184.216.34"])
    module = __import__(f"modulo.model_backends.{provider}", fromlist=[class_name])
    with patch.object(module, _chat_cls) as chat_cls:
        backend_cls = getattr(module, class_name)
        backend_cls(api_key=API_KEY, model_id="m", **{endpoint_kwarg: "https://models.example.com/v1"})
    chat_cls.assert_called_once()


@pytest.mark.parametrize(
    ("provider", "_class_name", "_chat_cls", "_endpoint_kwarg"), _ADAPTERS, ids=[a[0] for a in _ADAPTERS]
)
def test_hub_adapter_custom_endpoint_still_goes_through_the_ssrf_gate(
    provider: str, _class_name: str, _chat_cls: str, _endpoint_kwarg: str
) -> None:
    """The hub feeds the credential endpoint into the adapter gate, not around it."""
    from modulo.core.model_backend_hub import _build_backend

    with pytest.raises(ValueError, match="private/internal"):
        _build_backend(
            provider,
            "m",
            {"api_key": API_KEY, "base_url": "http://10.1.2.3:8000/v1"},
            {},
        )
