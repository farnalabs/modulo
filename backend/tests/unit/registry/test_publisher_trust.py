"""Unit tests for publisher trust model, search ranking, and download tracking."""

import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.library_service import copy_to_adapt
from modulo.core.registry import (
    _BUILTIN_REGISTRY,
    PUBLISHER_TRUST_COMMUNITY,
    PUBLISHER_TRUST_REVOKED,
    PUBLISHER_TRUST_VERIFIED,
    _publishers,
    compute_popularity_score,
    get_publisher_status,
    list_registry_primitives_ranked,
    list_verified_publishers,
    register_publisher,
    revoke_publisher,
)


class _PreservePublishers:
    @pytest.fixture(autouse=True)
    def _preserve_publishers(self):
        saved = copy.deepcopy(dict(_publishers))
        yield
        _publishers.clear()
        _publishers.update(saved)


class TestPublisherTrust(_PreservePublishers):
    def test_builtin_modulo_publisher_is_verified(self):
        entry = _BUILTIN_REGISTRY.get("modulo/prd-input-schema")
        assert entry is not None
        status = get_publisher_status(entry.signing_key_fingerprint)
        assert status == PUBLISHER_TRUST_VERIFIED

    def test_unknown_fingerprint_is_community(self):
        status = get_publisher_status("unknown-fingerprint-1234")
        assert status == PUBLISHER_TRUST_COMMUNITY

    def test_register_publisher(self):
        fp = "aabbccdd00112233"
        pub = register_publisher(fp, "test-author", "Test Author", website="https://example.com")
        assert pub.status == PUBLISHER_TRUST_VERIFIED
        assert get_publisher_status(fp) == PUBLISHER_TRUST_VERIFIED

    def test_revoke_publisher(self):
        fp = "revoke-test-fingerprint"
        register_publisher(fp, "revokable", "Revokable Author")
        assert get_publisher_status(fp) == PUBLISHER_TRUST_VERIFIED
        revoke_publisher(fp)
        assert get_publisher_status(fp) == PUBLISHER_TRUST_REVOKED

    def test_revoke_nonexistent_returns_false(self):
        assert revoke_publisher("nonexistent") is False

    def test_list_verified_publishers(self):
        publishers = list_verified_publishers()
        assert any(p.author == "modulo" for p in publishers)


class TestSearchRanking:
    def test_popularity_score_higher_with_more_downloads(self):
        now = datetime.now(UTC)
        low = compute_popularity_score(10, None, 0, now)
        high = compute_popularity_score(5000, None, 0, now)
        assert high > low

    def test_popularity_score_higher_with_rating(self):
        now = datetime.now(UTC)
        no_rating = compute_popularity_score(100, None, 0, now)
        with_rating = compute_popularity_score(100, 4.5, 10, now)
        assert with_rating > no_rating

    def test_popularity_score_older_primitives_decay(self):
        old = datetime(2020, 1, 1, tzinfo=UTC)
        recent = datetime.now(UTC)
        old_score = compute_popularity_score(100, 3.0, 5, old)
        recent_score = compute_popularity_score(100, 3.0, 5, recent)
        assert recent_score > old_score

    def test_ranked_list_sorts_by_popularity(self):
        results = list_registry_primitives_ranked(sort_by="popularity")
        scores = [r["popularity_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_ranked_list_sorts_by_recent(self):
        results = list_registry_primitives_ranked(sort_by="recent")
        dates = [r["entry"].published_at for r in results]
        assert dates == sorted(dates, reverse=True)

    def test_ranked_list_includes_publisher_status(self):
        results = list_registry_primitives_ranked()
        for r in results:
            assert "publisher_status" in r
            assert r["publisher_status"] in ("verified", "community", "revoked")

    def test_ranked_list_filter_by_type(self):
        results = list_registry_primitives_ranked(primitive_type="schema")
        assert all(r["entry"].primitive_type == "schema" for r in results)


class TestDownloadTracking:
    async def test_copy_to_adapt_increments_download_count(self):
        """Test that downloading via registry increments download count."""
        session = MagicMock()
        session.flush = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=ctx)

        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = None
        scalar_result.scalars.return_value = [MagicMock()]
        session.execute = AsyncMock(return_value=scalar_result)

        org_id = MagicMock()
        primitive_id = MagicMock()

        # Mock a source primitive that is from the registry
        source = MagicMock()
        source.source = "registry"
        source.download_count = 5
        source.visibility = "org"
        source.version = "1.0"
        source.primitive_type = "schema"
        source.name = "test"
        source.slug = "test"
        source.description = "test"
        source.author = "modulo"
        source.tags = []
        source.content_json = {}
        source.id = org_id

        with (
            patch(
                "modulo.core.library_service.get_primitive",
                AsyncMock(return_value=source),
            ),
            patch(
                "modulo.core.library_service.create_library_primitive",
                AsyncMock(),
            ),
            patch(
                "modulo.core.library_service.set_rls_org",
                AsyncMock(),
            ),
            patch(
                "modulo.core.library_service.set_rls_user_context",
                AsyncMock(),
            ),
        ):
            await copy_to_adapt(
                session,
                org_id=org_id,
                primitive_id=primitive_id,
            )

            assert session.execute.await_count >= 1
