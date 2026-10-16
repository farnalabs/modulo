"""FAR-1856 — the empty-lifecycle-map scanner against a real Postgres.

``_scan_empty_lifecycle_maps`` builds ``LifecycleMap.content_json == {}``.
SQLite cannot reproduce the defect: the model declared a bare generic ``JSON``
while migration ``0147_json_to_jsonb_standardize`` converted the column to
``jsonb``, so Postgres rejected the predicate with
``operator does not exist: jsonb = json``. ``scan_all`` tolerates an exception
per-scanner, so the category reported zero candidates forever while the route
still returned 200 — a silent failure path. The model now carries the
repository's ``JSON().with_variant(JSONB(), "postgresql")`` convention (the
``agent.py`` / ``audit_event.py`` precedent) so the parameter binds as
``jsonb``.

The companion FAR-1857 case (``perform_cleanup`` over-reporting
``deleted_count``) lands in the next commit on this branch and extends this
module.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.housekeeping import _scan_empty_lifecycle_maps, scan_all
from modulo.db.models.account import Account
from modulo.db.models.lifecycle_map import LifecycleMap
from modulo.db.models.organisation import Organisation

pytestmark = pytest.mark.integration


class TestEmptyLifecycleMapScanOnPostgres:
    """FAR-1856: ``content_json == {}`` must compile to ``jsonb = jsonb``."""

    async def test_empty_map_is_surfaced_and_non_empty_map_is_not(
        self,
        db_session: AsyncSession,
    ) -> None:
        org_id = uuid.uuid4()
        account_id = uuid.uuid4()
        db_session.add(Organisation(id=org_id, name="Housekeeping Test Org", slug=f"far1856-{org_id.hex[:8]}"))
        db_session.add(
            Account(
                id=account_id,
                email=f"far1856-{account_id.hex[:8]}@test.local",
                display_name="Housekeeping Test User",
                auth_provider="local",
            )
        )
        # Flush the FK targets first so the dependent rows below cannot be
        # ordered ahead of them by a single big autoflush.
        await db_session.flush()

        empty_id = uuid.uuid4()
        filled_id = uuid.uuid4()
        db_session.add(
            LifecycleMap(
                id=empty_id,
                organisation_id=org_id,
                account_id=account_id,
                name="hk-empty-map",
                content_json={},
            )
        )
        db_session.add(
            LifecycleMap(
                id=filled_id,
                organisation_id=org_id,
                account_id=account_id,
                name="hk-filled-map",
                content_json={"stages": ["build", "test"]},
            )
        )
        await db_session.flush()

        # Direct scanner call: pre-fix this RAISED ProgrammingError
        # ("operator does not exist: jsonb = json") instead of returning.
        candidates = await _scan_empty_lifecycle_maps(db_session, org_id)

        surfaced_names = sorted(c.name for c in candidates)
        assert surfaced_names == ["hk-empty-map"], f"expected only the empty map, got {surfaced_names}"
        assert str(empty_id) in {c.id for c in candidates}
        assert str(filled_id) not in {c.id for c in candidates}

        # Production path: scan_all swallows a per-scanner exception and
        # substitutes [], so the category itself must carry the candidate —
        # pre-fix it came back empty while the route still reported 200.
        results = await scan_all(db_session, org_id)
        by_category = {r.category: r for r in results}
        category = by_category["empty_lifecycle_maps"]
        category_names = sorted(c.name for c in category.candidates)
        assert category_names == ["hk-empty-map"], f"scan_all category was {category_names}"
