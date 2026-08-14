"""Unit tests for the housekeeping scan service (modulo.core.housekeeping)."""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from modulo.core.housekeeping import (
    _CATEGORY_TO_ENTITY,
    _SCANNERS,
    ENTITY_MODEL_MAP,
    Candidate,
    CategoryResult,
    scan_all,
)


class TestCandidate:
    def test_to_dict_includes_entity_type(self) -> None:
        c = Candidate(
            id="abc",
            name="key",
            detail="detail",
            created_at="2026-01-01T00:00:00+00:00",
            entity_type="secret",
        )
        assert c.to_dict() == {
            "id": "abc",
            "name": "key",
            "detail": "detail",
            "created_at": "2026-01-01T00:00:00+00:00",
            "entity_type": "secret",
        }

    def test_to_dict_defaults_entity_type_to_empty(self) -> None:
        c = Candidate(id="abc", name="key", detail="detail")
        assert not c.to_dict()["entity_type"]


class TestCategoryResult:
    def test_to_dict_uses_known_label_and_description(self) -> None:
        r = CategoryResult(category="orphan_secrets", candidates=[Candidate(id="a", name="k", detail="d")])
        data = r.to_dict()
        assert data["category"] == "orphan_secrets"
        assert data["label"] == "Orphan Secrets"
        assert data["description"]
        assert data["count"] == 1
        assert not data["candidates"][0]["entity_type"]

    def test_to_dict_falls_back_to_category_label(self) -> None:
        r = CategoryResult(category="mystery_category", candidates=[])
        data = r.to_dict()
        assert data["label"] == "mystery_category"
        assert data["count"] == 0


class TestScanAll:
    async def _fake_session(self) -> AsyncMock:
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_scan_all_enriches_candidates_with_entity_type(self) -> None:
        session = await self._fake_session()
        scanned_candidates = [Candidate(id=str(uuid.uuid4()), name="k", detail="d")]

        scanners = [("orphan_secrets", AsyncMock(return_value=scanned_candidates))]
        with patch("modulo.core.housekeeping._SCANNERS", scanners):
            results = await scan_all(session, uuid.uuid4())

        assert len(results) == 1
        assert results[0].category == "orphan_secrets"
        assert results[0].candidates[0].entity_type == "secret"

    @pytest.mark.asyncio
    async def test_scan_all_isolates_failing_scanner(self) -> None:
        session = await self._fake_session()
        org_id = uuid.uuid4()
        ok_candidates = [Candidate(id=str(uuid.uuid4()), name="ok", detail="d")]

        async def broken_scanner(_s, _o):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

        with (
            patch(
                "modulo.core.housekeeping._SCANNERS",
                [
                    ("orphan_secrets", AsyncMock(return_value=ok_candidates)),
                    ("stale_pipelines", broken_scanner),
                ],
            ),
        ):
            results = await scan_all(session, org_id)

        assert len(results) == 2
        assert results[0].candidates[0].entity_type == "secret"
        assert results[1].category == "stale_pipelines"
        assert not results[1].candidates

    @pytest.mark.asyncio
    async def test_scan_all_returns_category_for_every_scanner(self) -> None:
        session = await self._fake_session()
        with patch("modulo.core.housekeeping._SCANNERS", [("empty_teams", AsyncMock(return_value=[]))]):
            results = await scan_all(session, uuid.uuid4())
        assert len(results) == 1
        assert results[0].category == "empty_teams"
        assert not results[0].candidates


class TestMappings:
    def test_category_to_entity_covers_all_scanners(self) -> None:
        scanner_categories = {name for name, _ in _SCANNERS}
        assert set(_CATEGORY_TO_ENTITY) == scanner_categories

    def test_entity_types_are_valid_cleanup_targets(self) -> None:
        for entity_type in set(_CATEGORY_TO_ENTITY.values()):
            assert entity_type in ENTITY_MODEL_MAP, f"{entity_type} missing from ENTITY_MODEL_MAP"

    def test_every_scanner_entity_type_is_deletable(self) -> None:
        for category, _ in _SCANNERS:
            assert category in _CATEGORY_TO_ENTITY, f"{category} missing from _CATEGORY_TO_ENTITY"
            assert _CATEGORY_TO_ENTITY[category] in ENTITY_MODEL_MAP
