"""Unit tests for the login-active CRUD helpers (FAR-856).

Tests for:
- ``is_login_active_org``: the ORM-level predicate.
- ``list_login_active_orgs``: the SQL-level query.
- ``get_login_active_org_by_slug``: the single-org slug lookup.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.db.crud.organisation import (
    _SENTINEL_ORG_IDS,
    _seed_evidence_redaction_catch_all,
    create_organisation,
    get_login_active_org_by_slug,
    is_login_active_org,
    list_login_active_orgs,
)
from modulo.db.models.evidence_redaction_rule import (
    CATCH_ALL_REDACTED_FIELDS,
    EVIDENCE_REDACTION_CATCH_ALL_PATTERN,
    EVIDENCE_REDACTION_CATCH_ALL_ROLE,
    EvidenceRedactionRule,
)
from modulo.db.models.organisation import MODULO_REGISTRY_ORG_ID, SYSTEM_ORG_ID


def _make_org(
    *,
    id: uuid.UUID | None = None,
    status: str = "active",
    deleted_at: datetime | None = None,
) -> MagicMock:
    org = MagicMock()
    org.id = id or uuid.uuid4()
    org.status = status
    org.deleted_at = deleted_at
    org.slug = "test-org"
    org.name = "Test Org"
    return org


class TestIsLoginActiveOrg:
    """Tests for the ``is_login_active_org`` ORM-level predicate."""

    def test_active_org_is_login_active(self) -> None:
        org = _make_org()
        assert is_login_active_org(org) is True

    def test_suspended_org_is_not_login_active(self) -> None:
        org = _make_org(status="suspended")
        assert is_login_active_org(org) is False

    def test_deleted_org_is_not_login_active(self) -> None:
        org = _make_org(deleted_at=datetime.now(UTC))
        assert is_login_active_org(org) is False

    def test_orphan_org_is_not_login_active(self) -> None:
        org = _make_org(id=SYSTEM_ORG_ID)
        assert is_login_active_org(org) is False

    def test_registry_org_is_not_login_active(self) -> None:
        org = _make_org(id=MODULO_REGISTRY_ORG_ID)
        assert is_login_active_org(org) is False

    def test_active_org_with_non_sentinel_id_is_login_active(self) -> None:
        # Any UUID not in the sentinel set should be login-active.
        org = _make_org(id=uuid.uuid4())
        assert is_login_active_org(org) is True


class TestSentinelIds:
    """Verify the sentinel set is correctly populated."""

    def test_sentinel_ids_contain_both_sentinels(self) -> None:
        assert SYSTEM_ORG_ID in _SENTINEL_ORG_IDS
        assert MODULO_REGISTRY_ORG_ID in _SENTINEL_ORG_IDS

    def test_sentinel_ids_only_contain_expected(self) -> None:
        assert len(_SENTINEL_ORG_IDS) == 2


class TestListLoginActiveOrgs:
    """Tests for ``list_login_active_orgs`` (SQL-level query)."""

    @pytest.mark.asyncio
    async def test_returns_login_active_orgs(self) -> None:
        session = AsyncMock()
        org = _make_org()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [org]
        session.execute = AsyncMock(return_value=result)

        orgs = await list_login_active_orgs(session)
        assert len(orgs) == 1
        assert orgs[0] is org

    @pytest.mark.asyncio
    async def test_empty_when_no_orgs(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=result)

        orgs = await list_login_active_orgs(session)
        assert orgs == []


class TestGetLoginActiveOrgBySlug:
    """Tests for ``get_login_active_org_by_slug``."""

    @pytest.mark.asyncio
    async def test_returns_org_for_valid_slug(self) -> None:
        session = AsyncMock()
        org = _make_org()
        result = MagicMock()
        result.scalars.return_value.first.return_value = org
        session.execute = AsyncMock(return_value=result)

        found = await get_login_active_org_by_slug(session, "test-org")
        assert found is org

    @pytest.mark.asyncio
    async def test_returns_none_for_unknown_slug(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.first.return_value = None
        session.execute = AsyncMock(return_value=result)

        found = await get_login_active_org_by_slug(session, "nonexistent")
        assert found is None


class TestSeedEvidenceRedactionCatchAll:
    """New orgs must be seeded with the mandatory §2.4 catch-all rule (FAR-944)."""

    @pytest.mark.asyncio
    async def test_seeds_catch_all_row_when_absent(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()  # sync call — not awaitable
        result = MagicMock()
        result.first.return_value = None
        session.execute = AsyncMock(return_value=result)
        org_id = uuid.uuid4()

        await _seed_evidence_redaction_catch_all(session, org_id)

        session.add.assert_called_once()
        added = session.add.call_args.args[0]
        assert isinstance(added, EvidenceRedactionRule)
        assert added.organisation_id == org_id
        assert added.key_pattern == EVIDENCE_REDACTION_CATCH_ALL_PATTERN
        assert added.role == EVIDENCE_REDACTION_CATCH_ALL_ROLE
        assert tuple(added.redacted_fields) == CATCH_ALL_REDACTED_FIELDS
        session.flush.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_catch_all_row_already_present(self) -> None:
        session = AsyncMock()
        result = MagicMock()
        result.first.return_value = (uuid.uuid4(),)
        session.execute = AsyncMock(return_value=result)

        await _seed_evidence_redaction_catch_all(session, uuid.uuid4())

        session.add.assert_not_called()
        session.flush.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_organisation_invokes_the_catch_all_seed(self) -> None:
        session = AsyncMock()
        session.add = MagicMock()  # sync call — not awaitable

        with patch(
            "modulo.db.crud.organisation._seed_evidence_redaction_catch_all",
            new=AsyncMock(),
        ) as seed:
            await create_organisation(session, name="Fresh Org", slug="fresh-org")

        seed.assert_awaited_once()
