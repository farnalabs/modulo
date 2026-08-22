"""Unit tests for community-library sync orchestration (FAR-363).

The sync layer is tested with a fake AsyncSession (no real DB) and a fake
LibraryClient. The contract under test: store the verified manifest + apply
revocations, and fail open (never raise, preserve the last-good cache).
"""

from __future__ import annotations

import types
from typing import Self

import pytest

from modulo.core.library_sync import get_cached_manifest, is_revoked, sync_library
from modulo.core.library_sync import sync as sync_module
from modulo.core.library_sync.manifest import canonical_manifest_bytes
from modulo.core.library_sync.models import SINGLETON_ID, LibrarySyncState
from modulo.registry.crypto import generate_keypair, sign

ENDPOINT = "https://library.modulo.run"


def _signed_manifest(
    private_key_pem: str, *, entries: list[dict] | None = None, revoked: list[dict] | None = None
) -> dict:
    body: dict[str, object] = {
        "schema_version": "1",
        "generated_at": "2026-08-22T00:00:00Z",
        "entries": entries if entries is not None else [],
        "revoked": revoked if revoked is not None else [],
    }
    canonical = canonical_manifest_bytes(body)
    return {**body, "signature": {"algorithm": "ed25519", "value": sign(private_key_pem, canonical)}}


def _settings(*, endpoint: str = ENDPOINT, root_key: str = "", timeout: float = 15.0) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        modulo_library_endpoint=endpoint,
        modulo_library_root_public_key=root_key,
        modulo_library_sync_timeout_seconds=timeout,
    )


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    """Minimal AsyncSession stand-in covering the sync paths (no real DB)."""

    def __init__(self, existing: LibrarySyncState | None = None) -> None:
        self.state = existing
        self.added: list[object] = []
        self._active_tx = False

    def in_transaction(self) -> bool:
        return self._active_tx

    async def execute(self, _stmt: object) -> _FakeResult:
        return _FakeResult(self.state)

    def add(self, obj: object) -> None:
        self.added.append(obj)
        if self.state is None:
            self.state = obj  # type: ignore[assignment]

    def begin(self) -> Self:
        self._active_tx = True
        return self

    async def __aenter__(self) -> Self:
        self._active_tx = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._active_tx = False


class _FakeClient:
    def __init__(self, manifest: dict | None, catalog: list[dict] | None, error: Exception | None = None) -> None:
        self.manifest = manifest
        self.catalog = catalog
        self.error = error
        self.closed = False

    async def fetch_manifest(self) -> dict | None:
        if self.error is not None:
            raise self.error
        return self.manifest

    async def fetch_catalog(self) -> list[dict] | None:
        return self.catalog

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def keys() -> tuple[str, str]:
    return generate_keypair()


class TestSyncLibrary:
    async def test_disabled_when_endpoint_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sync_module, "get_settings", lambda: _settings(endpoint=""))
        monkeypatch.setattr(sync_module, "LibraryClient", lambda **_: pytest.fail("client must not be built"))

        session = _FakeSession()
        result = await sync_library(session)

        assert result.success is False
        assert "disabled" in (result.error or "")

    async def test_stores_manifest_and_applies_revocations(
        self, keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_key, public_key = keys
        entries = [
            {"id": "agent-1", "type": "agent", "slug": "revoked-agent"},
            {"id": "agent-2", "type": "agent", "slug": "healthy-agent"},
        ]
        manifest = _signed_manifest(private_key, entries=entries, revoked=[{"id": "agent-1"}])
        client = _FakeClient(manifest=manifest, catalog=entries)

        monkeypatch.setattr(sync_module, "get_settings", lambda: _settings(endpoint=ENDPOINT, root_key=public_key))
        monkeypatch.setattr(sync_module, "LibraryClient", lambda **_: client)

        session = _FakeSession()
        result = await sync_library(session)

        assert result.success is True
        assert result.entries_count == 2
        assert result.revoked_count == 1
        assert client.closed is True

        assert session.state is not None
        assert session.state.manifest_json == manifest
        by_id = {entry["id"]: entry for entry in session.state.catalog_json}
        assert by_id["agent-1"]["status"] == "revoked"
        assert "status" not in by_id["agent-2"]

    async def test_updates_existing_state(self, keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
        private_key, public_key = keys
        old_manifest = _signed_manifest(private_key, entries=[{"id": "agent-1"}])
        new_manifest = _signed_manifest(private_key, entries=[{"id": "agent-1"}, {"id": "agent-2"}])
        client = _FakeClient(manifest=new_manifest, catalog=[{"id": "agent-1"}, {"id": "agent-2"}])

        monkeypatch.setattr(sync_module, "get_settings", lambda: _settings(endpoint=ENDPOINT, root_key=public_key))
        monkeypatch.setattr(sync_module, "LibraryClient", lambda **_: client)

        existing = LibrarySyncState(id=SINGLETON_ID, manifest_json=old_manifest, catalog_json=[{"id": "agent-1"}])
        session = _FakeSession(existing=existing)
        result = await sync_library(session)

        assert result.success is True
        assert existing.manifest_json == new_manifest
        assert len(existing.catalog_json) == 2
        assert existing.last_error is None

    async def test_fail_open_on_manifest_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(manifest=None, catalog=None)
        monkeypatch.setattr(sync_module, "get_settings", lambda: _settings(endpoint=ENDPOINT))
        monkeypatch.setattr(sync_module, "LibraryClient", lambda **_: client)

        session = _FakeSession()
        result = await sync_library(session)

        assert result.success is False
        assert "manifest" in (result.error or "")
        assert session.state is not None
        assert "manifest" in (session.state.last_error or "")

    async def test_fail_open_on_unexpected_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(manifest=None, catalog=None, error=RuntimeError("boom"))
        monkeypatch.setattr(sync_module, "get_settings", lambda: _settings(endpoint=ENDPOINT))
        monkeypatch.setattr(sync_module, "LibraryClient", lambda **_: client)

        session = _FakeSession()
        result = await sync_library(session)

        assert result.success is False
        assert result.error == "unexpected sync failure"
        assert session.state is not None
        assert session.state.last_error == "unexpected sync failure"


class TestCachedManifest:
    async def test_returns_none_when_empty(self) -> None:
        session = _FakeSession()
        assert await get_cached_manifest(session) is None

    async def test_returns_stored_manifest(self) -> None:
        manifest = {"schema_version": "1", "entries": [{"id": "agent-1"}]}
        state = LibrarySyncState(id=SINGLETON_ID, manifest_json=manifest, catalog_json=[])
        assert await get_cached_manifest(_FakeSession(existing=state)) == manifest

    async def test_fail_open_on_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _explode(_session: object) -> LibrarySyncState:
            raise RuntimeError("db down")

        monkeypatch.setattr(sync_module, "_read_state", _explode)
        assert await get_cached_manifest(_FakeSession()) is None


class TestIsRevoked:
    def _state_with_manifest(self, manifest: dict) -> _FakeSession:
        state = LibrarySyncState(id=SINGLETON_ID, manifest_json=manifest, catalog_json=[])
        return _FakeSession(existing=state)

    async def test_true_for_revoked_id(self) -> None:
        manifest = {
            "schema_version": "1",
            "entries": [{"id": "agent-1"}],
            "revoked": [{"id": "agent-1", "reason": "malicious"}],
        }
        assert await is_revoked(self._state_with_manifest(manifest), "agent-1") is True

    async def test_false_for_healthy_id(self) -> None:
        manifest = {"schema_version": "1", "entries": [{"id": "agent-2"}], "revoked": [{"id": "agent-1"}]}
        assert await is_revoked(self._state_with_manifest(manifest), "agent-2") is False

    async def test_false_when_no_manifest(self) -> None:
        assert await is_revoked(_FakeSession(), "agent-1") is False
