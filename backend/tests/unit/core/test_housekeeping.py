"""Unit tests for the housekeeping scan service (modulo.core.housekeeping)."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Column, String, Uuid
from sqlalchemy.orm import declarative_base

from modulo.core import housekeeping as hk
from modulo.core.housekeeping import (
    _CATEGORY_TO_ENTITY,
    _SCANNERS,
    ENTITY_MODEL_MAP,
    Candidate,
    CategoryResult,
    scan_all,
)

_FakeBase = declarative_base()


class _FakeTenantModel(_FakeBase):
    __tablename__ = "pipelines"
    id = Column(Uuid(), primary_key=True)
    organisation_id = Column(Uuid())


class _FakeNonIdPkTenantModel(_FakeBase):
    """Tenant-scoped model whose PK is NOT ``id`` (mirrors OAuthAuthorizationCode)."""

    __tablename__ = "oauth_authorization_codes"
    code = Column(String(64), primary_key=True)
    organisation_id = Column(Uuid())


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


class TestScanInvalidOrgFk:
    @pytest.mark.asyncio
    async def test_missing_org_floats_orphaned_rows(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()
        fake_rows = [SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4())]

        def fake_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            lowered = str(stmt).lower()
            if "organisations" in lowered and "where" in lowered:
                # First call: organisation existence check -> missing.
                result.scalar_one_or_none.return_value = None
            else:
                result.scalars.return_value.all.return_value = fake_rows
            return result

        session.execute.side_effect = fake_execute
        with patch.object(hk, "_tenant_models", return_value=[_FakeTenantModel]):
            candidates = await hk._scan_invalid_org_fk(session, org_id)

        assert len(candidates) == 2
        assert all(c.entity_type == "invalid_org_fk" for c in candidates)
        assert all(str(org_id) in c.detail for c in candidates)

    @pytest.mark.asyncio
    async def test_valid_org_returns_no_candidates(self) -> None:
        session = AsyncMock()
        org_id = uuid.uuid4()

        def fake_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            result.scalar_one_or_none.return_value = org_id  # org exists
            return result

        session.execute.side_effect = fake_execute
        candidates = await hk._scan_invalid_org_fk(session, org_id)
        assert candidates == []

    @pytest.mark.asyncio
    async def test_missing_org_floats_orphaned_rows_for_non_id_pk_model(self) -> None:
        """Regression test for the AttributeError raised when a tenant-scoped
        model's PK is not ``id`` (e.g. OAuthAuthorizationCode PK ``code``)."""
        session = AsyncMock()
        org_id = uuid.uuid4()
        orphan_code = "abc123def456"
        fake_rows = [SimpleNamespace(code=orphan_code)]

        def fake_execute(stmt: object, *args: object, **kwargs: object) -> MagicMock:
            result = MagicMock()
            lowered = str(stmt).lower()
            if "organisations" in lowered and "where" in lowered:
                result.scalar_one_or_none.return_value = None  # org missing
            else:
                result.scalars.return_value.all.return_value = fake_rows
            return result

        session.execute.side_effect = fake_execute
        with patch.object(hk, "_tenant_models", return_value=[_FakeNonIdPkTenantModel]):
            candidates = await hk._scan_invalid_org_fk(session, org_id)

        assert len(candidates) == 1
        assert candidates[0].entity_type == "invalid_org_fk"
        # The candidate id must be derived from the real PK, not a non-existent ``r.id``.
        assert candidates[0].id == orphan_code
        assert candidates[0].name.startswith("oauth_authorization_codes#")

    def test_invalid_org_fk_is_registered_and_metadata_present(self) -> None:
        assert ("invalid_org_fk", hk._scan_invalid_org_fk) in hk._SCANNERS
        assert hk._CATEGORY_LABELS["invalid_org_fk"] == "Invalid Organisation FK"
        assert "orphaned" in hk._CATEGORY_DESCRIPTIONS["invalid_org_fk"]


class TestMappings:
    def test_cleanup_categories_are_a_subset_of_scanners(self) -> None:
        scanner_categories = {name for name, _ in _SCANNERS}
        # Detection-only categories (e.g. invalid_org_fk) are intentionally not
        # in _CATEGORY_TO_ENTITY, so the relation is a subset, not equality.
        assert set(_CATEGORY_TO_ENTITY).issubset(scanner_categories)

    def test_entity_types_are_valid_cleanup_targets(self) -> None:
        for entity_type in set(_CATEGORY_TO_ENTITY.values()):
            assert entity_type in ENTITY_MODEL_MAP, f"{entity_type} missing from ENTITY_MODEL_MAP"

    def test_every_cleanup_scanner_entity_type_is_deletable(self) -> None:
        for category, entity_type in _CATEGORY_TO_ENTITY.items():
            assert entity_type in ENTITY_MODEL_MAP
            assert category in {name for name, _ in _SCANNERS}
