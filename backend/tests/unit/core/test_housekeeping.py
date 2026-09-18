"""Unit tests for the housekeeping scan service (modulo.core.housekeeping)."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import Column, String, Uuid
from sqlalchemy.orm import declarative_base

from modulo.core import housekeeping as hk
from modulo.core.housekeeping import (
    _SCANNERS,
    ENTITY_MODEL_MAP,
    SCANNERS_BY_CATEGORY,
    Candidate,
    CategoryResult,
    Scanner,
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

        scanners = [
            Scanner(
                category="orphan_secrets",
                scan_func=AsyncMock(return_value=scanned_candidates),
                label="Orphan Secrets",
                description="d",
                entity_type="secret",
            )
        ]
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

        with patch(
            "modulo.core.housekeeping._SCANNERS",
            [
                Scanner(
                    category="orphan_secrets",
                    scan_func=AsyncMock(return_value=ok_candidates),
                    label="Orphan Secrets",
                    description="d",
                    entity_type="secret",
                ),
                Scanner(
                    category="stale_pipelines",
                    scan_func=broken_scanner,
                    label="Stale Pipelines",
                    description="d",
                    entity_type="pipeline",
                ),
            ],
        ):
            results = await scan_all(session, org_id)

        assert len(results) == 2
        assert results[0].candidates[0].entity_type == "secret"
        assert results[1].category == "stale_pipelines"
        assert not results[1].candidates

    @pytest.mark.asyncio
    async def test_scan_all_returns_category_for_every_scanner(self) -> None:
        session = await self._fake_session()
        with patch(
            "modulo.core.housekeeping._SCANNERS",
            [
                Scanner(
                    category="empty_teams",
                    scan_func=AsyncMock(return_value=[]),
                    label="Empty Teams",
                    description="d",
                    entity_type="team",
                )
            ],
        ):
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

    def test_invalid_org_fk_is_registered_in_scanners(self) -> None:
        entry = SCANNERS_BY_CATEGORY.get("invalid_org_fk")
        assert entry is not None
        assert entry.scan_func is hk._scan_invalid_org_fk
        assert entry.label == "Invalid Organisation FK"
        assert "orphaned" in entry.description.lower()
        assert entry.entity_type is None


class TestScanCheckpointRetention:
    _ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")

    @pytest.mark.asyncio
    async def test_reports_only_runs_with_reclaimable_checkpoint_bytes(self) -> None:
        session = AsyncMock()
        run1 = SimpleNamespace(
            langgraph_thread_id="org:t1", status="complete", run_number=1, created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        run2 = SimpleNamespace(
            langgraph_thread_id="org:t2", status="failed", run_number=2, created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        result = MagicMock()
        result.scalars.return_value.all.return_value = [run1, run2]
        session.execute = AsyncMock(return_value=result)

        detail_mock = AsyncMock(return_value=({"org:t1": 500, "org:t2": 0}, {}))
        with patch.object(hk, "_checkpoint_detail", new=detail_mock):
            candidates = await hk._scan_checkpoint_retention(session, self._ORG)

        # Only run1 has reclaimable checkpoint bytes (> 0); run2 is skipped.
        assert len(candidates) == 1
        assert candidates[0].id == "org:t1"
        assert candidates[0].name == "Run 1"
        assert "500" in candidates[0].detail

        # The byte estimator was asked about both selected runs' thread-ids.
        est_call = detail_mock.await_args
        assert est_call is not None
        assert est_call.args[1] == ["org:t1", "org:t2"]
        assert est_call.args[2] == self._ORG

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_reclaimable_runs(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)
        with patch.object(hk, "_checkpoint_detail", new=AsyncMock(return_value=({}, {}))):
            candidates = await hk._scan_checkpoint_retention(session, self._ORG)
        assert candidates == []

    def test_checkpoint_retention_is_registered_detection_only(self) -> None:
        entry = SCANNERS_BY_CATEGORY.get("checkpoint_retention")
        assert entry is not None
        assert entry.scan_func is hk._scan_checkpoint_retention
        assert entry.label == "Checkpoint Retention"
        assert entry.entity_type is None


class TestScannerRegistry:
    def test_every_scanner_has_label_and_description(self) -> None:
        for entry in _SCANNERS:
            assert entry.category
            assert entry.label
            assert entry.description

    def test_every_scanner_is_registered_in_lookup(self) -> None:
        for entry in _SCANNERS:
            assert SCANNERS_BY_CATEGORY[entry.category] is entry

    def test_entity_types_are_valid_cleanup_targets(self) -> None:
        for entry in _SCANNERS:
            if entry.entity_type is None:
                continue
            assert entry.entity_type in ENTITY_MODEL_MAP, (
                f"{entry.category} -> {entry.entity_type} missing from ENTITY_MODEL_MAP"
            )

    @pytest.mark.asyncio
    async def test_detection_only_scanner_preserves_candidate_entity_type(self) -> None:
        # DETECTION-ONLY categories (e.g. invalid_org_fk) set entity_type directly
        # on their candidates and must NOT be overridden by the category default.
        scanned = [Candidate(id="x", name="n", detail="d", entity_type="invalid_org_fk")]
        detection_only = Scanner(
            category="invalid_org_fk",
            scan_func=AsyncMock(return_value=scanned),
            label="Invalid Org FK",
            description="Orphan rows referencing a missing organisation",
            entity_type=None,
        )
        with patch("modulo.core.housekeeping._SCANNERS", [detection_only]):
            results = await scan_all(AsyncMock(), uuid.uuid4())
        assert results[0].candidates[0].entity_type == "invalid_org_fk"
