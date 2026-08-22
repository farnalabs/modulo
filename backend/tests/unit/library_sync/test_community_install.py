"""Unit tests for community library browse + install helpers (FAR-363).

The helpers read the cached manifest (via ``library_sync``) and install
``source="registry"`` primitives. We monkeypatch the manifest reads and the
blob fetch, and use a fake session for the DB queries.
"""

from __future__ import annotations

import types
import uuid
from typing import Any

import pytest

from modulo.core.library_service import community as community_module
from modulo.core.library_service.community import (
    get_community_entry,
    install_community_entry,
    list_community_entries,
)

ORG_ID = uuid.uuid4()

ENTRY_1 = {
    "id": "entry-1",
    "type": "agent",
    "slug": "code-reviewer",
    "author": "acme",
    "version": "1.0.0",
    "content_sha256": "a" * 64,
    "license": "MIT",
    "status": "published",
    "published_at": "2026-08-22T00:00:00Z",
}

ENTRY_2 = {
    "id": "entry-2",
    "type": "schema",
    "slug": "invoice",
    "author": "acme",
    "version": "2.1.0",
    "content_sha256": "b" * 64,
    "license": "MIT",
    "status": "published",
    "published_at": "2026-08-22T00:00:00Z",
}

MANIFEST = {
    "schema_version": "1",
    "generated_at": "2026-08-22T00:00:00Z",
    "entries": [ENTRY_1, ENTRY_2],
    "revoked": [],
    "signature": {"algorithm": "ed25519", "value": "deadbeef"},
}


class _FakeResult:
    def __init__(self, rows: list[Any] | None = None, scalar: Any = None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one_or_none(self) -> Any:
        return self._scalar


class _FakeSession:
    """Minimal AsyncSession stand-in for the community helper queries."""

    def __init__(self, installed_rows: list[tuple[str, str]] | None = None, existing_id: Any = None) -> None:
        self.installed_rows = installed_rows or []
        self.existing_id = existing_id
        self.added: list[Any] = []
        self._in_tx = False
        self.rolled_back = False

    def in_transaction(self) -> bool:
        return self._in_tx

    async def begin(self) -> None:
        self._in_tx = True

    async def rollback(self) -> None:
        self._in_tx = False
        self.rolled_back = True

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        if self._in_tx:
            self._in_tx = False

    async def execute(self, stmt: Any) -> _FakeResult:
        text = str(stmt)
        if "LibraryPrimitive.id" in text or "library_primitives.id" in text:
            return _FakeResult(scalar=self.existing_id)
        if "LibraryPrimitive.slug" in text or "library_primitives.slug" in text:
            return _FakeResult(rows=[(s, v) for s, v in self.installed_rows])
        return _FakeResult()

    def add(self, obj: Any) -> None:
        self.added.append(obj)


@pytest.fixture
def fake_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _manifest(_session: Any) -> dict | None:
        return MANIFEST

    async def _revoked(_session: Any, entry_id: str) -> bool:
        return False

    async def _set_rls(_session: Any, _org_id: Any) -> None:
        return None

    monkeypatch.setattr(community_module, "get_cached_manifest", _manifest)
    monkeypatch.setattr(community_module, "is_revoked", _revoked)
    monkeypatch.setattr(community_module, "set_rls_org", _set_rls)


@pytest.fixture
def fake_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blob(_sha256: str) -> dict[str, Any] | None:
        return {"description": "A test agent", "name": "code-reviewer"}

    monkeypatch.setattr(community_module, "_fetch_blob", _blob)


@pytest.fixture
def fake_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        community_module,
        "get_settings",
        lambda: types.SimpleNamespace(
            modulo_library_endpoint="https://library.modulo.run",
            modulo_library_root_public_key="",
            modulo_library_sync_timeout_seconds=15,
        ),
    )


class TestListCommunityEntries:
    async def test_returns_entries_with_installed_flags(self, fake_manifest: None, fake_settings: None) -> None:
        session = _FakeSession(installed_rows=[("code-reviewer", "1.0.0")])
        entries = await list_community_entries(session, ORG_ID)
        assert len(entries) == 2
        by_id = {e["id"]: e for e in entries}
        assert by_id["entry-1"]["installed"] is True
        assert by_id["entry-2"]["installed"] is False

    async def test_returns_empty_when_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _none(_session: Any) -> None:
            return None

        monkeypatch.setattr(community_module, "get_cached_manifest", _none)
        assert not await list_community_entries(_FakeSession(), ORG_ID)

    async def test_skips_revoked_entries(
        self, fake_manifest: None, fake_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manifest = dict(MANIFEST)
        manifest["revoked"] = [
            {
                "id": "entry-2",
                "type": "schema",
                "slug": "invoice",
                "version": "2.1.0",
                "reason": "deprecated",
                "revoked_at": "2026-08-22T00:00:00Z",
            }
        ]

        async def _manifest(_session: Any) -> dict | None:
            return manifest

        monkeypatch.setattr(community_module, "get_cached_manifest", _manifest)
        entries = await list_community_entries(_FakeSession(), ORG_ID)
        assert [e["id"] for e in entries] == ["entry-1"]


class TestGetCommunityEntry:
    async def test_returns_entry(self, fake_manifest: None, fake_settings: None) -> None:
        entry = await get_community_entry(_FakeSession(), "entry-1")
        assert entry is not None
        assert entry["slug"] == "code-reviewer"

    async def test_returns_none_for_unknown(self, fake_manifest: None, fake_settings: None) -> None:
        assert await get_community_entry(_FakeSession(), "nope") is None

    async def test_returns_none_when_revoked(
        self, fake_manifest: None, fake_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _revoked(_session: Any, entry_id: str) -> bool:
            return entry_id == "entry-1"

        monkeypatch.setattr(community_module, "is_revoked", _revoked)
        assert await get_community_entry(_FakeSession(), "entry-1") is None


class TestInstallCommunityEntry:
    async def test_installs_registry_row(self, fake_manifest: None, fake_blob: None, fake_settings: None) -> None:
        session = _FakeSession()
        prim = await install_community_entry(session, ORG_ID, "entry-1")
        assert prim.source == "registry"
        assert prim.slug == "code-reviewer"
        assert prim.version == "1.0.0"
        assert prim.auto_update is False
        assert prim.verified is True
        assert prim.checksum == "a" * 64
        assert prim.download_count == 0
        assert prim.average_rating is None
        assert prim.review_count is None
        assert prim.visibility == "org"
        assert prim.organisation_id == ORG_ID
        assert prim.content_json == {"description": "A test agent", "name": "code-reviewer"}
        assert session.added == [prim]

    async def test_raises_already_installed(self, fake_manifest: None, fake_blob: None, fake_settings: None) -> None:
        session = _FakeSession(existing_id=uuid.uuid4())
        with pytest.raises(ValueError, match="already installed"):
            await install_community_entry(session, ORG_ID, "entry-1")

    async def test_raises_entry_not_found(self, fake_manifest: None, fake_settings: None) -> None:
        with pytest.raises(ValueError, match="entry not found"):
            await install_community_entry(_FakeSession(), ORG_ID, "nope")

    async def test_rejects_team_targeted_install(
        self, fake_manifest: None, fake_blob: None, fake_settings: None
    ) -> None:
        with pytest.raises(ValueError, match="registry entries are org-owned"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1", target_team_id=uuid.uuid4())

    async def test_raises_blob_fetch_failed(
        self, fake_manifest: None, fake_settings: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _blob_none(_sha256: str) -> None:
            return None

        monkeypatch.setattr(community_module, "_fetch_blob", _blob_none)
        with pytest.raises(ValueError, match="blob fetch failed"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1")
