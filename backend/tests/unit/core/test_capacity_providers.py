"""Unit tests for the provider/platform capacity probe (FAR-1992).

Covers the provider-agnostic factory and the Fly volumes probe. Provider HTTP is
faked through the injectable client factory, so no real network is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Self

import pytest

from modulo.core.capacity_providers import build_platform_capacity_probe
from modulo.core.capacity_providers.fly import (
    FlyVolumeCapacityProbe,
    capacity_bytes_from_volumes,
)

_GIB = 1024**3


def _settings(
    *,
    provider: str = "auto",
    app: str = "modulo-app-db",
    volume: str = "",
    token: str = "fly-token",
) -> SimpleNamespace:
    return SimpleNamespace(
        db_capacity_platform_provider=provider,
        db_capacity_fly_app=app,
        db_capacity_fly_volume=volume,
        db_capacity_fly_api_token=token,
    )


# --- capacity_bytes_from_volumes (payload parsing) --------------------------


def test_single_volume_resolves_its_capacity() -> None:
    payload = [{"id": "vol_1", "name": "pg_data", "size_gb": 80}]
    assert capacity_bytes_from_volumes(payload, "") == 80 * _GIB


def test_empty_payload_resolves_nothing() -> None:
    assert capacity_bytes_from_volumes([], "") is None


def test_non_list_payload_resolves_nothing() -> None:
    assert capacity_bytes_from_volumes({"error": "nope"}, "") is None


def test_multiple_volumes_without_a_selector_resolve_nothing() -> None:
    # Ambiguous: never guess which volume holds the database.
    payload = [
        {"id": "vol_1", "name": "pg_data", "size_gb": 80},
        {"id": "vol_2", "name": "other", "size_gb": 10},
    ]
    assert capacity_bytes_from_volumes(payload, "") is None


def test_selector_by_name_picks_the_named_volume() -> None:
    payload = [
        {"id": "vol_1", "name": "other", "size_gb": 10},
        {"id": "vol_2", "name": "pg_data", "size_gb": 80},
    ]
    assert capacity_bytes_from_volumes(payload, "pg_data") == 80 * _GIB


def test_selector_by_id_picks_the_volume() -> None:
    payload = [
        {"id": "vol_1", "name": "other", "size_gb": 10},
        {"id": "vol_2", "name": "pg_data", "size_gb": 80},
    ]
    assert capacity_bytes_from_volumes(payload, "vol_2") == 80 * _GIB


def test_selector_matching_nothing_resolves_nothing() -> None:
    payload = [{"id": "vol_1", "name": "pg_data", "size_gb": 80}]
    assert capacity_bytes_from_volumes(payload, "absent") is None


def test_duplicate_selector_matches_resolve_nothing() -> None:
    # A selector that is not unique is ambiguous: refuse rather than guess.
    payload = [
        {"id": "vol_1", "name": "pg_data", "size_gb": 80},
        {"id": "vol_2", "name": "pg_data", "size_gb": 10},
    ]
    assert capacity_bytes_from_volumes(payload, "pg_data") is None


@pytest.mark.parametrize(
    "size_gb",
    [
        pytest.param(None, id="missing"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param("80", id="string"),
        pytest.param(True, id="bool"),
    ],
)
def test_unusable_size_gb_resolves_nothing(size_gb: Any) -> None:
    payload = [{"id": "vol_1", "name": "pg_data", "size_gb": size_gb}]
    assert capacity_bytes_from_volumes(payload, "") is None


# --- build_platform_capacity_probe (factory) --------------------------------


def test_factory_disabled_provider_returns_none() -> None:
    assert build_platform_capacity_probe(_settings(provider="")) is None


@pytest.mark.parametrize("provider", ["none", "disabled", "off"])
def test_factory_explicit_off_returns_none(provider: str) -> None:
    assert build_platform_capacity_probe(_settings(provider=provider)) is None


def test_factory_unknown_provider_returns_none() -> None:
    assert build_platform_capacity_probe(_settings(provider="aws")) is None


def test_factory_fly_without_token_returns_none() -> None:
    assert build_platform_capacity_probe(_settings(token="")) is None


def test_factory_fly_without_app_returns_none() -> None:
    assert build_platform_capacity_probe(_settings(app="")) is None


def test_factory_auto_builds_the_fly_probe() -> None:
    probe = build_platform_capacity_probe(_settings(provider="auto"))
    assert isinstance(probe, FlyVolumeCapacityProbe)
    assert probe.name == "fly"


def test_factory_explicit_fly_builds_the_probe() -> None:
    assert isinstance(build_platform_capacity_probe(_settings(provider="fly")), FlyVolumeCapacityProbe)


def test_factory_trims_whitespace_from_config() -> None:
    probe = build_platform_capacity_probe(_settings(provider="  FLY  ", app="  app ", token=" tok "))
    assert isinstance(probe, FlyVolumeCapacityProbe)
    assert probe._app == "app"
    assert probe._token == "tok"


# --- FlyVolumeCapacityProbe.probe_capacity_bytes (injected fake client) -----


class _FakeResponse:
    def __init__(self, payload: Any, *, status_error: bool = False, json_error: bool = False) -> None:
        self._payload = payload
        self._status_error = status_error
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self._status_error:
            raise RuntimeError("HTTP 500")

    def json(self) -> Any:
        if self._json_error:
            raise ValueError("not JSON")
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.requested_url: str | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str) -> Any:
        self.requested_url = url
        return self._response


def _client_factory_capturing(response: _FakeResponse) -> tuple[Any, dict[str, Any]]:
    client = _FakeClient(response)
    captured: dict[str, Any] = {}

    async def factory(url: str, **kwargs: Any) -> _FakeClient:
        captured["url"] = url
        captured.update(kwargs)
        return client

    return factory, captured


async def test_probe_returns_capacity_from_a_mocked_api() -> None:
    factory, captured = _client_factory_capturing(_FakeResponse([{"id": "v", "name": "pg_data", "size_gb": 80}]))
    probe = FlyVolumeCapacityProbe(app="modulo-app-db", token="tok", client_factory=factory)
    assert await probe.probe_capacity_bytes() == 80 * _GIB
    assert captured["url"] == "https://api.machines.dev/v1/apps/modulo-app-db/volumes"
    assert captured["headers"]["Authorization"] == "Bearer tok"


async def test_probe_returns_none_on_http_error() -> None:
    factory, _ = _client_factory_capturing(_FakeResponse([], status_error=True))
    probe = FlyVolumeCapacityProbe(app="app", token="tok", client_factory=factory)
    assert await probe.probe_capacity_bytes() is None


async def test_probe_returns_none_on_malformed_body() -> None:
    factory, _ = _client_factory_capturing(_FakeResponse(None, json_error=True))
    probe = FlyVolumeCapacityProbe(app="app", token="tok", client_factory=factory)
    assert await probe.probe_capacity_bytes() is None


async def test_probe_returns_none_when_the_client_factory_raises() -> None:
    async def factory(url: str, **kwargs: Any) -> Any:
        raise RuntimeError("connect refused")

    probe = FlyVolumeCapacityProbe(app="app", token="tok", client_factory=factory)
    assert await probe.probe_capacity_bytes() is None


async def test_probe_returns_none_when_the_request_raises() -> None:
    class _BoomClient(_FakeClient):
        async def get(self, url: str) -> Any:
            raise RuntimeError("connection reset")

    async def factory(url: str, **kwargs: Any) -> _BoomClient:
        return _BoomClient(_FakeResponse([]))

    probe = FlyVolumeCapacityProbe(app="app", token="tok", client_factory=factory)
    assert await probe.probe_capacity_bytes() is None


async def test_probe_returns_none_when_no_volume_matches() -> None:
    factory, _ = _client_factory_capturing(_FakeResponse([{"id": "v", "name": "other", "size_gb": 80}]))
    probe = FlyVolumeCapacityProbe(app="app", token="tok", volume="pg_data", client_factory=factory)
    assert await probe.probe_capacity_bytes() is None
