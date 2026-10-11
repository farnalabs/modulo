"""Unit tests for community library browse + install helpers (FAR-363).

The helpers read the cached manifest (via ``library_sync``) and install
``source="registry"`` primitives. We monkeypatch the manifest reads and the
blob fetch, and use a fake session for the DB queries.
"""

from __future__ import annotations

import hashlib
import logging
import types
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import InvalidRequestError

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

# Sentinel for "field is absent from the entry" in the malformed-field matrix.
_MISSING = object()


def _manifest_with(entry: dict[str, Any]) -> dict[str, Any]:
    return {**MANIFEST, "entries": [entry]}


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
        self.id_query_params: list[Any] = []
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
        # Mirror the real session's ``autobegin=False`` shape: every helper
        # opens a transaction via ``_enter_org_context`` before executing.
        if not self._in_tx:
            raise InvalidRequestError("execute() called outside a transaction")
        # Discriminate on the selected columns, not the rendered SQL text, and
        # do not depend on column order.
        names = {description["name"] for description in stmt.column_descriptions}
        if names == {"slug", "version"}:
            return _FakeResult(rows=[(s, v) for s, v in self.installed_rows])
        if names == {"id"}:
            self.id_query_params = list(stmt.compile().params.values())
            return _FakeResult(scalar=self.existing_id)
        raise AssertionError(f"unexpected query columns: {sorted(names)}")

    def add(self, obj: Any) -> None:
        self.added.append(obj)


@pytest.fixture
def fake_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the three manifest-layer calls the community helpers make.

    Patches ``get_cached_manifest``, ``is_revoked`` AND ``set_rls_org`` so the
    browse/install paths never touch the DB or a signed manifest.
    """

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


class _FakeLibraryClient:
    """Stand-in for ``LibraryClient`` exercising the real ``_fetch_blob`` body."""

    def __init__(
        self,
        blob: bytes | None,
        *,
        fetch_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self._blob = blob
        self._fetch_error = fetch_error
        self._close_error = close_error
        self.closed = False

    async def fetch_blob(self, _sha256: str) -> bytes | None:
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._blob

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class TestListCommunityEntries:
    async def test_returns_entries_with_installed_flags(self, fake_manifest: None) -> None:
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

    async def test_skips_revoked_entries(self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch) -> None:
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
        assert {e["id"] for e in entries} == {"entry-1"}

    async def test_rolls_back_when_set_rls_fails(self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _boom(_session: Any, _org_id: Any) -> None:
            raise RuntimeError("rls set failed")

        monkeypatch.setattr(community_module, "set_rls_org", _boom)

        session = _FakeSession()
        with pytest.raises(RuntimeError, match="rls set failed"):
            await list_community_entries(session, ORG_ID)
        assert session.rolled_back is True

    async def test_reuses_active_transaction_without_committing(self, fake_manifest: None) -> None:
        """A caller-owned transaction is reused, never committed by the callee."""
        session = _FakeSession(installed_rows=[("code-reviewer", "1.0.0")])
        session._in_tx = True

        entries = await list_community_entries(session, ORG_ID)

        assert len(entries) == 2
        # began=False in ``_installed_registry_keys`` must skip the commit and
        # leave the caller's transaction open.
        assert session._in_tx is True


class TestGetCommunityEntry:
    async def test_returns_entry(self, fake_manifest: None) -> None:
        entry = await get_community_entry(_FakeSession(), "entry-1")
        assert entry is not None
        assert entry["slug"] == "code-reviewer"

    async def test_returns_none_for_unknown(self, fake_manifest: None) -> None:
        assert await get_community_entry(_FakeSession(), "nope") is None

    async def test_returns_none_when_revoked(self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _revoked(_session: Any, entry_id: str) -> bool:
            return entry_id == "entry-1"

        monkeypatch.setattr(community_module, "is_revoked", _revoked)
        assert await get_community_entry(_FakeSession(), "entry-1") is None

    async def test_returns_none_when_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _none(_session: Any) -> None:
            return None

        monkeypatch.setattr(community_module, "get_cached_manifest", _none)
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
        # Registry rows must stay org-owned: ``owner_team_id`` is NULL.
        assert prim.owner_team_id is None
        assert prim.content_json == {"description": "A test agent", "name": "code-reviewer"}
        assert session.added == [prim]

    async def test_raises_already_installed(self, fake_manifest: None) -> None:
        session = _FakeSession(existing_id=uuid.uuid4())
        with pytest.raises(ValueError, match="already installed"):
            await install_community_entry(session, ORG_ID, "entry-1")
        # The existence probe must key on the entry's slug AND version, scoped
        # to the installing org.
        assert ORG_ID in session.id_query_params
        assert "code-reviewer" in session.id_query_params
        assert "1.0.0" in session.id_query_params

    async def test_raises_entry_not_found(self, fake_manifest: None) -> None:
        with pytest.raises(ValueError, match="entry not found"):
            await install_community_entry(_FakeSession(), ORG_ID, "nope")

    async def test_raises_revoked_entry(self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _revoked(_session: Any, entry_id: str) -> bool:
            return entry_id == "entry-1"

        monkeypatch.setattr(community_module, "is_revoked", _revoked)
        with pytest.raises(ValueError, match="entry not found"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1")

    async def test_raises_entry_not_found_when_no_manifest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _none(_session: Any) -> None:
            return None

        monkeypatch.setattr(community_module, "get_cached_manifest", _none)
        with pytest.raises(ValueError, match="entry not found"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1")

    async def test_raises_unsupported_primitive_type(
        self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _manifest(_session: Any) -> dict | None:
            return _manifest_with({**ENTRY_1, "type": "bogus"})

        monkeypatch.setattr(community_module, "get_cached_manifest", _manifest)
        with pytest.raises(ValueError, match="unsupported primitive type"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1")

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("content_sha256", _MISSING),
            ("slug", _MISSING),
            ("version", _MISSING),
            ("author", _MISSING),
            ("slug", 123),
            ("content_sha256", 123),
        ],
        ids=[
            "missing-content_sha256",
            "missing-slug",
            "missing-version",
            "missing-author",
            "non-string-slug",
            "non-string-content_sha256",
        ],
    )
    async def test_raises_entry_not_found_for_malformed_required_fields(
        self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch, field: str, value: object
    ) -> None:
        entry = dict(ENTRY_1)
        if value is _MISSING:
            del entry[field]
        else:
            entry[field] = value

        async def _manifest(_session: Any) -> dict | None:
            return _manifest_with(entry)

        monkeypatch.setattr(community_module, "get_cached_manifest", _manifest)
        with pytest.raises(ValueError, match="entry not found"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1")

    async def test_rejects_team_targeted_install(self, fake_manifest: None) -> None:
        with pytest.raises(ValueError, match="registry entries are org-owned"):
            await install_community_entry(_FakeSession(), ORG_ID, "entry-1", target_team_id=uuid.uuid4())

    async def test_raises_blob_fetch_failed(self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _blob_none(_sha256: str) -> None:
            return None

        monkeypatch.setattr(community_module, "_fetch_blob", _blob_none)
        session = _FakeSession()
        with pytest.raises(ValueError, match="blob fetch failed"):
            await install_community_entry(session, ORG_ID, "entry-1")
        # ``_enter_org_context`` opened the txn (began=True), so the failure path
        # owns the rollback and must have executed it.
        assert session.rolled_back is True

    async def test_caller_owned_transaction_is_never_rolled_back(
        self, fake_manifest: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _blob_none(_sha256: str) -> None:
            return None

        monkeypatch.setattr(community_module, "_fetch_blob", _blob_none)
        session = _FakeSession()
        session._in_tx = True

        with pytest.raises(ValueError, match="blob fetch failed"):
            await install_community_entry(session, ORG_ID, "entry-1")

        # began=False in ``_enter_org_context``: the caller's transaction is
        # reused and left open — the callee must not roll it back.
        assert session.rolled_back is False
        assert session._in_tx is True


class TestFetchBlob:
    """Exercises the real ``_fetch_blob`` body (elsewhere monkeypatched away)."""

    def _install_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        blob: bytes | None,
        *,
        fetch_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> _FakeLibraryClient:
        client = _FakeLibraryClient(blob, fetch_error=fetch_error, close_error=close_error)
        monkeypatch.setattr(community_module, "LibraryClient", lambda **_: client)
        monkeypatch.setattr(
            community_module,
            "get_settings",
            lambda: types.SimpleNamespace(
                modulo_library_endpoint="https://library.modulo.run",
                modulo_library_root_public_key="",
                modulo_library_sync_timeout_seconds=15,
            ),
        )
        return client

    async def test_returns_none_when_client_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._install_client(monkeypatch, None)
        assert await community_module._fetch_blob("a" * 64) is None
        # The ``finally: await client.close()`` must run on the failure path too.
        assert client.closed is True

    async def test_returns_none_on_hash_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Valid JSON whose sha256 does NOT match the requested hash: only the
        # hash guard can produce None (were it removed, the object would parse
        # and be returned).
        payload = b'{"name": "x"}'
        self._install_client(monkeypatch, payload)
        expected = hashlib.sha256(b"expected").hexdigest()
        assert await community_module._fetch_blob(expected) is None

    async def test_returns_none_on_non_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"not json"
        self._install_client(monkeypatch, payload)
        assert await community_module._fetch_blob(hashlib.sha256(payload).hexdigest()) is None

    async def test_returns_none_on_non_object_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b"[1,2]"
        self._install_client(monkeypatch, payload)
        assert await community_module._fetch_blob(hashlib.sha256(payload).hexdigest()) is None

    async def test_returns_parsed_object_when_hash_matches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = b'{"name": "code-reviewer"}'
        client = self._install_client(monkeypatch, payload)
        result = await community_module._fetch_blob(hashlib.sha256(payload).hexdigest())
        assert result == {"name": "code-reviewer"}
        assert client.closed is True

    async def test_raising_close_is_logged_and_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A ``close()`` that raises never escapes ``_fetch_blob``.

        The teardown sits in ``finally`` AFTER the ``except ValueError`` arm,
        so without its own guard a raising ``close()`` would replace the
        body's return on BOTH paths: the parsed-content return (success) and
        the ``None`` degrade (ValueError). Proves both returns survive and
        the teardown failure is logged.
        """
        payload = b'{"name": "code-reviewer"}'
        client = self._install_client(monkeypatch, payload, close_error=RuntimeError("teardown failed"))
        sha = hashlib.sha256(payload).hexdigest()

        with caplog.at_level(logging.ERROR, logger=community_module.logger.name):
            content = await community_module._fetch_blob(sha)

        assert content == {"name": "code-reviewer"}
        assert client.closed is True
        assert "community.install.client_close_failed" in caplog.messages

        # Failure path: the ValueError degrade still returns None.
        failing = self._install_client(monkeypatch, b"not json", close_error=RuntimeError("teardown failed"))
        not_json_sha = hashlib.sha256(b"not json").hexdigest()

        with caplog.at_level(logging.ERROR, logger=community_module.logger.name):
            degraded = await community_module._fetch_blob(not_json_sha)

        assert degraded is None
        assert failing.closed is True

    async def test_raising_close_does_not_displace_body_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Body exception + failing teardown: the BODY's error wins.

        Without the teardown guard the ``finally``'s exception replaces the
        propagating ``RuntimeError`` from ``fetch_blob``, masking the real
        failure with the teardown's own message.
        """
        self._install_client(
            monkeypatch,
            None,
            fetch_error=RuntimeError("blob boom"),
            close_error=RuntimeError("teardown failed"),
        )

        with pytest.raises(RuntimeError, match="blob boom"):
            await community_module._fetch_blob("a" * 64)
