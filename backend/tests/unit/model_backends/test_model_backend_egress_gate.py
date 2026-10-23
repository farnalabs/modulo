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
from modulo.core.model_backend_hub import _build_backend
from modulo.core.ssrf import PinnedAsyncHTTPTransport
from modulo.model_backends.anthropic import AnthropicBackend
from modulo.model_backends.base import openai_compatible_health_check
from modulo.model_backends.cohere import CohereBackend
from modulo.model_backends.gemini import GeminiBackend
from modulo.model_backends.lm_studio import LmStudioBackend
from modulo.model_backends.mistral import MistralBackend
from modulo.model_backends.module import OpenAICompatibleBackend
from modulo.model_backends.ollama import OllamaBackend
from modulo.model_backends.vllm import VllmBackend

pytestmark = pytest.mark.real_ssrf_dns

API_KEY = "test-key"  # nosec B105 - test fixture value, not a credential
PUBLIC_URL = "https://models.example.com/v1"

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
    with pytest.raises(ValueError, match="private/internal"):
        _build_backend(
            "openai",
            "m",
            {"api_key": API_KEY, "base_url": "http://10.1.2.3:8000/v1"},
            {},
        )


# ---------------------------------------------------------------------------
# FAR-2046: the legacy provider adapters (anthropic / cohere / gemini /
# mistral) take an operator-supplied endpoint too. They must go through the
# same SSRF gate as the OpenAI-compatible path — a target that is internal,
# link-local (IMDS) or loopback-without-opt-in must fail construction.
#
# These drive the REAL adapter constructors (no patched LangChain class). The
# gate fires before the provider client is built, so a blocked target never
# reaches the network.
# ---------------------------------------------------------------------------

_ENDPOINT_KWARG: dict[type, str] = {
    AnthropicBackend: "anthropic_api_url",
    CohereBackend: "base_url",
    GeminiBackend: "base_url",
    MistralBackend: "endpoint",
}


@pytest.mark.parametrize("backend_cls", list(_ENDPOINT_KWARG))
@pytest.mark.parametrize("blocked_url", BLOCKED_URLS)
def test_legacy_adapter_refuses_blocked_custom_endpoint(backend_cls: type, blocked_url: str) -> None:
    """Each adapter fails closed on a private/internal custom endpoint."""
    with pytest.raises(ValueError, match="private/internal"):
        backend_cls(api_key=API_KEY, model_id="m", **{_ENDPOINT_KWARG[backend_cls]: blocked_url})


@pytest.mark.parametrize("provider", ["anthropic", "cohere", "gemini", "mistral"])
def test_hub_custom_endpoint_for_legacy_adapters_goes_through_the_gate(provider: str) -> None:
    """The hub's provider-specific endpoint kwarg cannot bypass the gate."""
    with pytest.raises(ValueError, match="private/internal"):
        _build_backend(provider, "m", {"api_key": API_KEY, "base_url": "http://10.1.2.3:8000/v1"}, {})


@pytest.mark.parametrize("backend_cls", list(_ENDPOINT_KWARG))
async def test_legacy_adapter_accepts_public_custom_endpoint(
    backend_cls: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control case: a legitimate public endpoint still constructs."""
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["93.184.216.34"])
    backend = backend_cls(api_key=API_KEY, model_id="m", **{_ENDPOINT_KWARG[backend_cls]: PUBLIC_URL})
    aclose = getattr(backend, "aclose", None)
    if aclose is not None:
        await aclose()
    assert backend is not None


@pytest.mark.parametrize("backend_cls", [GeminiBackend, MistralBackend])
def test_pinned_adapters_carry_the_pinned_transport(
    backend_cls: type,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adapters that CAN accept a client/transport must actually pin it.

    This is the rebinding guarantee: a bare URL pass-through would leave the
    provider SDK free to re-resolve the host at connect time. The assertion
    reads the transport the provider client would use, so an upgrade that
    silently drops the injected transport fails here.
    """
    monkeypatch.setattr(ssrf, "_resolve_all_sync", lambda _host: ["93.184.216.34"])
    backend = backend_cls(api_key=API_KEY, model_id="m", **{_ENDPOINT_KWARG[backend_cls]: PUBLIC_URL})
    if backend_cls is MistralBackend:
        transport = backend._model.async_client._transport  # type: ignore[attr-defined]
        # The adapter issues requests on relative paths, so the injected client
        # must also carry the endpoint as its base_url.
        assert str(backend._model.async_client.base_url).rstrip("/") == PUBLIC_URL  # type: ignore[attr-defined]
    else:
        genai_client = backend._model.client._api_client  # type: ignore[attr-defined]
        transport = genai_client._async_httpx_client._transport
    assert isinstance(transport, PinnedAsyncHTTPTransport)
