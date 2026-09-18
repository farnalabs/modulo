"""BDD step definitions: SSRF-safe outbound URL validation (feat-core-ssrf).

Drives the REAL ``modulo.core.ssrf`` guard seams — sync and async URL
validation, the tenant-scoped egress allowlist layering, DNS fail-closed
semantics, the pinned-target resolution and the unpinned-host refusal of the
pinned transport — without any real DNS or network traffic: hostname
resolutions are injected by patching the module's ``_resolve_all_sync`` /
``_resolve_all_async`` seams (the same network-free approach the 73-function
unit suite in ``backend/tests/unit/core/test_ssrf.py`` uses), so the product-map
claim that the guard blocks private/link-local/metadata targets end to end is
pinned by executing BDD rather than by a mocked assertion.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core import ssrf

scenarios("../features/security/ssrf_guard.feature")

_SSRF_ALLOW_ENV = "SSRF_ALLOW_PRIVATE_RANGES"


def _parse_ips(csv_ips: str) -> list[str]:
    """Split a comma-separated address list, treating an empty string as none."""
    if not csv_ips.strip():
        return []
    return [item.strip() for item in csv_ips.split(",") if item.strip()]


def _make_sync_resolver(expected_host: str, ips: list[str]):
    """A fake ``_resolve_all_sync`` that resolves one host to a fixed set."""

    def _resolve(host: str) -> list[str]:
        assert host == expected_host, f"resolver called with {host!r}, expected {expected_host!r}"
        return ips

    return _resolve


def _make_async_resolver(expected_host: str, ips: list[str]):
    """A fake ``_resolve_all_async`` (coroutine) that resolves one host."""

    async def _resolve(host: str) -> list[str]:
        assert host == expected_host, f"async resolver called with {host!r}, expected {expected_host!r}"
        return ips

    return _resolve


def _capture(request, call) -> None:
    """Run ``call`` and record the raised ``ValueError`` (or ``None`` on success)."""
    try:
        call()
    except ValueError as exc:
        request.node._ssrf_error = exc
    else:
        request.node._ssrf_error = None


# -- Given: egress allowlist environment ---------------------------------------


@given("the global egress allowlist is empty")
def step_allowlist_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_SSRF_ALLOW_ENV, raising=False)


@given(parsers.parse('the global egress allowlist is "{cidr}"'))
def step_allowlist_set(cidr: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SSRF_ALLOW_ENV, cidr)


# -- Given: injected DNS resolution ---------------------------------------------


@given(parsers.parse('the hostname "{host}" resolves to "{ips_csv}"'))
def step_resolver_sync(host: str, ips_csv: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "_resolve_all_sync", _make_sync_resolver(host, _parse_ips(ips_csv)))


@given(parsers.parse('the hostname "{host}" resolves to no addresses'))
def step_resolver_sync_empty(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "_resolve_all_sync", _make_sync_resolver(host, []))


@given(parsers.parse('the hostname "{host}" resolves asynchronously to "{ips_csv}"'))
def step_resolver_async(host: str, ips_csv: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssrf, "_resolve_all_async", _make_async_resolver(host, _parse_ips(ips_csv)))


# -- When: validation surfaces ---------------------------------------------------


@when(parsers.parse('I validate the outbound URL "{url}"'))
def step_validate_sync(url: str, request) -> None:
    _capture(request, lambda: ssrf.validate_outbound_url(url))


@when(parsers.parse('I validate the outbound URL "{url}" allowlisting "{networks}"'))
def step_validate_sync_allowlist(url: str, networks: str, request) -> None:
    allow_networks = _parse_ips(networks)
    _capture(request, lambda: ssrf.validate_outbound_url(url, allow_networks=allow_networks))


@when(parsers.parse('I validate the outbound URL "{url}" asynchronously'))
def step_validate_async(url: str, request) -> None:
    _capture(request, lambda: asyncio.run(ssrf.validate_outbound_url_async(url)))


@when(parsers.parse('I resolve the pinned target for "{url}"'))
def step_resolve_pinned(url: str, request) -> None:
    request.node._ssrf_target = asyncio.run(ssrf.resolve_pinned_ip(url))


@when(parsers.parse('I build a pinned async transport for "{url}"'))
def step_build_pinned_transport(url: str, request) -> None:
    request.node._ssrf_transport = ssrf.pinned_async_transport_sync(url)


@when(parsers.parse('I build a pinned async client for "{url}" with a caller-supplied transport'))
def step_build_pinned_client_bad_transport(url: str, request) -> None:
    _capture(request, lambda: ssrf.pinned_async_client_sync(url, transport=httpx.AsyncHTTPTransport()))


# -- Then: verdicts ---------------------------------------------------------------


@then("the URL is accepted")
def step_url_accepted(request) -> None:
    error = request.node._ssrf_error
    assert error is None, f"expected the URL to be accepted, got a rejection: {error!r}"


@then(parsers.parse('the URL is rejected with "{reason}"'))
def step_url_rejected(reason: str, request) -> None:
    error = request.node._ssrf_error
    assert isinstance(error, ValueError), f"expected a ValueError rejection, got {error!r}"
    assert reason.lower() in str(error).lower(), f"expected {reason!r} in rejection, got {error!r}"


@then(parsers.parse('the URL build fails with "{reason}"'))
def step_url_build_fails(reason: str, request) -> None:
    step_url_rejected(reason, request)


@then(parsers.parse('the pinned target keeps hostname "{host}" and addresses "{ips_csv}"'))
def step_pinned_target_shape(host: str, ips_csv: str, request) -> None:
    target = request.node._ssrf_target
    assert target is not None, "no pinned target was resolved"
    assert target.host == host, f"expected host {host!r}, got {target.host!r}"
    expected_ips = tuple(_parse_ips(ips_csv))
    assert target.ips == expected_ips, f"expected addresses {expected_ips!r}, got {target.ips!r}"
    assert target.scheme == "https"


@then(parsers.parse('the pinned transport refuses the unpinned host "{host}"'))
def step_pinned_transport_refuses(host: str, request) -> None:
    transport = request.node._ssrf_transport
    assert transport is not None, "no pinned transport was built"
    backend = transport._pool._network_backend

    async def _probe():
        try:
            await backend.connect_tcp(host, 443)
        except ssrf.UnpinnedHostError as exc:
            return exc
        return None

    exc = asyncio.run(_probe())
    assert isinstance(exc, ssrf.UnpinnedHostError), f"expected UnpinnedHostError, got {exc!r}"
    assert "refusing to connect to unpinned host" in str(exc), f"unexpected error message: {exc!r}"
