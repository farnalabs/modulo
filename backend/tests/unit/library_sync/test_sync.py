"""Unit tests for community-library sync orchestration (FAR-363).

The sync layer is tested with a fake AsyncSession (no real DB) and a fake
LibraryClient. The contract under test: store the verified manifest and apply
revocations, and fail open — every failure path records the error and returns a
SyncResult without raising, leaving the last-good cached manifest intact.
"""

from __future__ import annotations

import logging
import types
from typing import Any, Self

import pytest
from sqlalchemy.exc import InvalidRequestError
from tests.unit.library_sync.conftest import signed_manifest as _signed_manifest

from modulo.core.library_sync import get_cached_manifest, is_revoked, sync_library
from modulo.core.library_sync import sync as sync_module
from modulo.core.library_sync.models import SINGLETON_ID, LibrarySyncState

ENDPOINT = "https://library.modulo.run"


def _settings(*, endpoint: str = ENDPOINT, root_key: str = "", timeout: float = 15.0) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        modulo_library_endpoint=endpoint,
        modulo_library_root_public_key=root_key,
        modulo_library_sync_timeout_seconds=timeout,
    )


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    *,
    endpoint: str = ENDPOINT,
    root_key: str = "",
) -> None:
    """Point the sync module's settings + client factory at *client* (no real HTTP)."""
    monkeypatch.setattr(sync_module, "get_settings", lambda: _settings(endpoint=endpoint, root_key=root_key))
    monkeypatch.setattr(sync_module, "LibraryClient", lambda **_: client)


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeSession:
    """Minimal AsyncSession stand-in covering the sync paths (no real DB).

    Models the real session's ``autobegin=False`` shape: ``execute`` outside an
    active transaction, and a re-entrant ``begin``, both raise
    ``InvalidRequestError``.
    """

    def __init__(self, existing: LibrarySyncState | None = None) -> None:
        self.state = existing
        self._active_tx = False

    def in_transaction(self) -> bool:
        return self._active_tx

    async def execute(self, _stmt: object) -> _FakeResult:
        if not self._active_tx:
            raise InvalidRequestError("execute() called outside a transaction")
        return _FakeResult(self.state)

    def add(self, obj: object) -> None:
        if self.state is None:
            self.state = obj  # type: ignore[assignment]

    def begin(self) -> Self:
        if self._active_tx:
            raise InvalidRequestError("a transaction is already begun on this session")
        self._active_tx = True
        return self

    async def __aenter__(self) -> Self:
        self._active_tx = True
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._active_tx = False


class _FakeClient:
    def __init__(
        self, manifest: dict | None, error: Exception | None = None, close_error: Exception | None = None
    ) -> None:
        self.manifest = manifest
        self.error = error
        self.close_error = close_error
        self.closed = False

    async def fetch_manifest(self) -> dict | None:
        if self.error is not None:
            raise self.error
        return self.manifest

    async def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _AwaitableInTransactionSession(_FakeSession):
    """Session whose ``in_transaction()`` returns an AWAITABLE (sync.py:55).

    Some session/driver combinations return a coroutine here. Every coroutine
    is truthy, so the value must be AWAITED — a truthiness check alone would
    always take the "txn active" branch and call ``execute()`` outside one.
    """

    def __init__(self, existing: LibrarySyncState | None = None, *, active_tx: bool = False) -> None:
        super().__init__(existing=existing)
        self._active_tx = active_tx

    async def _resolve(self) -> bool:
        return self._active_tx

    def in_transaction(self) -> Any:
        return self._resolve()


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
        client = _FakeClient(manifest=manifest)
        _install_client(monkeypatch, client, root_key=public_key)

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
        client = _FakeClient(manifest=new_manifest)
        _install_client(monkeypatch, client, root_key=public_key)

        existing = LibrarySyncState(id=SINGLETON_ID, manifest_json=old_manifest, catalog_json=[{"id": "agent-1"}])
        session = _FakeSession(existing=existing)
        result = await sync_library(session)

        assert result.success is True
        assert existing.manifest_json == new_manifest
        assert len(existing.catalog_json) == 2
        assert existing.last_error is None

    async def test_manifest_failure_records_error_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(manifest=None)
        _install_client(monkeypatch, client)

        session = _FakeSession()
        result = await sync_library(session)

        assert result.success is False
        assert "manifest" in (result.error or "")
        assert session.state is not None
        assert "manifest" in (session.state.last_error or "")

    async def test_fail_open_preserves_last_good_manifest(
        self, keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_key, public_key = keys
        good_manifest = _signed_manifest(private_key, entries=[{"id": "agent-1"}])
        good_catalog = [{"id": "agent-1", "type": "agent", "slug": "keeper"}]
        client = _FakeClient(manifest=None)
        _install_client(monkeypatch, client, root_key=public_key)

        existing = LibrarySyncState(id=SINGLETON_ID, manifest_json=good_manifest, catalog_json=good_catalog)
        session = _FakeSession(existing=existing)
        result = await sync_library(session)

        assert result.success is False
        assert existing.manifest_json == good_manifest
        assert existing.catalog_json == good_catalog
        assert existing.last_error == "manifest fetch or verification failed"
        assert existing.last_synced_at is not None

    async def test_record_failure_never_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(manifest=None)
        _install_client(monkeypatch, client)

        async def _boom(_session: object) -> None:
            raise RuntimeError("state load failed")

        monkeypatch.setattr(sync_module, "_load_state", _boom)

        session = _FakeSession()
        result = await sync_library(session)

        assert isinstance(result, sync_module.SyncResult)
        assert result.success is False

    async def test_fail_open_on_unexpected_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _FakeClient(manifest=None, error=RuntimeError("boom"))
        _install_client(monkeypatch, client)

        session = _FakeSession()
        result = await sync_library(session)

        assert result.success is False
        assert result.error == "unexpected sync failure"
        assert session.state is not None
        assert session.state.last_error == "unexpected sync failure"

    async def test_raising_close_never_propagates(
        self, keys: tuple[str, str], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``close()`` that raises is logged and swallowed on every path.

        The teardown sits in ``finally`` OUTSIDE the ``except Exception``
        block, so without its own guard a raising ``close()`` would replace the
        body's exception (or discard its ``return``) and escape — breaking the
        documented never-raises contract. Proves both the success and the
        failure path return a ``SyncResult`` and that the failure is logged.
        """
        private_key, public_key = keys
        good_manifest = _signed_manifest(private_key, entries=[{"id": "agent-1"}])
        good_client = _FakeClient(manifest=good_manifest, close_error=RuntimeError("teardown failed"))
        _install_client(monkeypatch, good_client, root_key=public_key)

        with caplog.at_level(logging.ERROR, logger=sync_module._log.name):
            ok = await sync_library(_FakeSession())

        assert isinstance(ok, sync_module.SyncResult)
        assert ok.success is True
        assert ok.entries_count == 1
        assert good_client.closed is True
        assert "library_sync.client_close_failed" in caplog.messages

        # Failure path: the body failed AND teardown failed — still no escape.
        failing_client = _FakeClient(manifest=None, close_error=RuntimeError("teardown failed"))
        _install_client(monkeypatch, failing_client)

        failed = await sync_library(_FakeSession())

        assert isinstance(failed, sync_module.SyncResult)
        assert failed.success is False
        assert failing_client.closed is True


class TestCachedManifest:
    async def test_returns_none_when_no_state_stored(self) -> None:
        session = _FakeSession()
        assert await get_cached_manifest(session) is None

    async def test_returns_stored_manifest(self) -> None:
        manifest = {"schema_version": "1", "entries": [{"id": "agent-1"}]}
        state = LibrarySyncState(id=SINGLETON_ID, manifest_json=manifest, catalog_json=[])
        assert await get_cached_manifest(_FakeSession(existing=state)) == manifest

    async def test_reuses_active_transaction(self) -> None:
        """``_read_state`` must not re-enter ``begin`` when a txn is already open."""
        manifest = {"schema_version": "1", "entries": [{"id": "agent-1"}]}
        state = LibrarySyncState(id=SINGLETON_ID, manifest_json=manifest, catalog_json=[])
        session = _FakeSession(existing=state)
        session._active_tx = True

        assert await get_cached_manifest(session) == manifest
        # Still the same caller-owned transaction (no re-begin, no close).
        assert session._active_tx is True

    async def test_in_transaction_result_may_be_awaitable(self) -> None:
        """sync.py:55 — a coroutine from ``in_transaction()`` is awaited.

        Every coroutine is truthy, so without the ``await`` the truthiness
        check would always take the "txn active" branch: with no transaction
        really open, ``_load_state``'s ``execute()`` then raises
        ``InvalidRequestError`` (sessions are ``autobegin=False``) and the
        manifest degrades to ``None`` instead of being returned.
        """
        manifest = {"schema_version": "1", "entries": [{"id": "agent-1"}]}
        state = LibrarySyncState(id=SINGLETON_ID, manifest_json=manifest, catalog_json=[])

        # Awaitable resolves False: no transaction open — must begin() its own.
        outside = _AwaitableInTransactionSession(existing=state)
        assert await get_cached_manifest(outside) == manifest

        # Awaitable resolves True: reuse the caller's open transaction as-is.
        inside = _AwaitableInTransactionSession(existing=state, active_tx=True)
        assert await get_cached_manifest(inside) == manifest
        assert inside._active_tx is True

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

    async def test_false_when_manifest_read_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _explode(_session: object) -> dict | None:
            raise RuntimeError("db down")

        monkeypatch.setattr(sync_module, "get_cached_manifest", _explode)
        assert await is_revoked(_FakeSession(), "agent-1") is False
