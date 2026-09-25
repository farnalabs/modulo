"""Unit tests for FAR-1161 owner-email resolution in bundle export/import.

Covers ``_resolve_owner_emails_for_export`` (id -> email, missing account
warns) and ``_resolve_owner_emails_for_import`` (email -> eligible id, with
every strip-and-warn path). The wider round-trip is exercised by the
integration suite.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from modulo.core.workflow_import_export import (
    _ImportContext,
    _resolve_owner_emails_for_export,
    _resolve_owner_emails_for_import,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_BUSINESS_ID = uuid.UUID("00000000-0000-0000-0000-0000000000cc")
_RELIABILITY_ID = uuid.UUID("00000000-0000-0000-0000-0000000000dd")


def _export_session(rows: list[SimpleNamespace]) -> MagicMock:
    session = MagicMock()
    result = MagicMock()
    result.scalars.return_value = rows
    session.execute = AsyncMock(return_value=result)
    return session


class TestResolveOwnerEmailsForExport:
    async def test_no_owners_issues_no_query(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        pipeline = SimpleNamespace(business_owner_id=None, reliability_owner_id=None)
        assert await _resolve_owner_emails_for_export(session, pipeline) == (None, None)
        session.execute.assert_not_awaited()

    async def test_non_uuid_attributes_treated_as_unassigned(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        pipeline = SimpleNamespace(business_owner_id=MagicMock(), reliability_owner_id="not-a-uuid")
        assert await _resolve_owner_emails_for_export(session, pipeline) == (None, None)
        session.execute.assert_not_awaited()

    async def test_resolves_emails(self) -> None:
        session = _export_session(
            [
                SimpleNamespace(id=_BUSINESS_ID, email="biz@example.com"),
                SimpleNamespace(id=_RELIABILITY_ID, email="sre@example.com"),
            ]
        )
        pipeline = SimpleNamespace(business_owner_id=_BUSINESS_ID, reliability_owner_id=_RELIABILITY_ID)
        assert await _resolve_owner_emails_for_export(session, pipeline) == (
            "biz@example.com",
            "sre@example.com",
        )

    async def test_single_owner_missing_account_warns_and_omits_email(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _export_session([])
        pipeline = SimpleNamespace(business_owner_id=_BUSINESS_ID, reliability_owner_id=None)
        with caplog.at_level(logging.WARNING):
            result = await _resolve_owner_emails_for_export(session, pipeline)
        assert result == (None, None)
        assert "business owner account" in caplog.text

    async def test_reliability_owner_missing_account_warns(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _export_session([])
        pipeline = SimpleNamespace(business_owner_id=None, reliability_owner_id=_RELIABILITY_ID)
        with caplog.at_level(logging.WARNING):
            result = await _resolve_owner_emails_for_export(session, pipeline)
        assert result == (None, None)
        assert "reliability owner account" in caplog.text


def _ctx(session: MagicMock) -> _ImportContext:
    return _ImportContext(
        session=session,
        org_id=_ORG_ID,
        created_by=_USER_ID,
        warnings=[],
        owner_team_id=None,
    )


class TestResolveOwnerEmailsForImport:
    async def test_no_emails_strips_both(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        ctx = _ctx(session)
        assert await _resolve_owner_emails_for_import(ctx, {}) == (None, None)
        assert not ctx.warnings

    async def test_empty_string_treated_as_unassigned(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        ctx = _ctx(session)
        result = await _resolve_owner_emails_for_import(
            ctx,
            {"business_owner_email": "", "reliability_owner_email": ""},
        )
        assert result == (None, None)
        assert not ctx.warnings

    async def test_non_string_email_warns(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        ctx = _ctx(session)
        result = await _resolve_owner_emails_for_import(ctx, {"business_owner_email": 123})
        assert result == (None, None)
        assert any("is not a string" in w for w in ctx.warnings)

    async def test_unresolvable_email_warns(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        ctx = _ctx(session)
        with patch(
            "modulo.core.workflow_import_export.get_account_by_email",
            new=AsyncMock(return_value=None),
        ):
            result = await _resolve_owner_emails_for_import(ctx, {"business_owner_email": "ghost@example.com"})
        assert result == (None, None)
        assert any("does not resolve to an account" in w for w in ctx.warnings)

    async def test_ineligible_email_warns_and_strips(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        ctx = _ctx(session)
        account = SimpleNamespace(id=_BUSINESS_ID)
        with (
            patch(
                "modulo.core.workflow_import_export.get_account_by_email",
                new=AsyncMock(return_value=account),
            ),
            patch(
                "modulo.core.workflow_import_export.validate_accountability_owner",
                new=AsyncMock(side_effect=HTTPException(status_code=422, detail="is deactivated")),
            ),
        ):
            result = await _resolve_owner_emails_for_import(ctx, {"business_owner_email": "dead@example.com"})
        assert result == (None, None)
        assert any("is not an eligible owner" in w for w in ctx.warnings)

    async def test_eligible_emails_resolve_to_ids(self) -> None:
        session = MagicMock()
        session.execute = AsyncMock()
        ctx = _ctx(session)
        with (
            patch(
                "modulo.core.workflow_import_export.get_account_by_email",
                new=AsyncMock(side_effect=[SimpleNamespace(id=_BUSINESS_ID), SimpleNamespace(id=_RELIABILITY_ID)]),
            ),
            patch(
                "modulo.core.workflow_import_export.validate_accountability_owner",
                new=AsyncMock(),
            ) as validate,
        ):
            result = await _resolve_owner_emails_for_import(
                ctx,
                {
                    "business_owner_email": "biz@example.com",
                    "reliability_owner_email": "sre@example.com",
                },
            )
        assert result == (_BUSINESS_ID, _RELIABILITY_ID)
        assert validate.await_count == 2
        assert all(c.kwargs["visibility"] == "org" for c in validate.await_args_list)
        assert not ctx.warnings
