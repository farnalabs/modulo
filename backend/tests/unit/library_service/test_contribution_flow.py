"""Unit tests for the library contribution flow — draft, review, publish,
versioning, import notification, and the version/parse helpers.

Targets the contribution lifecycle in ``modulo.core.library_service``:
  - ``contribute_fixture`` / ``contribute_primitive`` draft creation
  - ``submit_contribution_for_review`` status transitions
  - ``publish_contribution`` community cache + importer notification
  - ``submit_contribution_version`` / ``list_contribution_versions``
  - ``notify_importers_of_update`` fork copy back-propagation
  - ``_bump_version`` / ``_parse_version_key`` edge cases
  - ``_filter_primitives`` empty-list short-circuit
"""

import asyncio
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError

from modulo.core.library_service import (
    _COMMUNITY_BY_ID,
    _COMMUNITY_BY_SLUG,
    _COMMUNITY_PRIMITIVES,
    CONTRIBUTION_DRAFT,
    CONTRIBUTION_PUBLISHED,
    CONTRIBUTION_REVIEW_QUEUE,
    MODULO_ORG_ID,
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
    _bump_version,
    _filter_primitives,
    _parse_version_key,
    contribute_fixture,
    contribute_primitive,
    list_contribution_versions,
    list_contributions,
    list_org_contributions,
    notify_importers_of_update,
    publish_contribution,
    submit_contribution_for_review,
    submit_contribution_version,
)
from modulo.db.crud.base import PageResult

_COMMUNITY_PRIMITIVES_BASELINE = tuple(_COMMUNITY_PRIMITIVES)
_COMMUNITY_BY_ID_BASELINE = dict(_COMMUNITY_BY_ID)
_COMMUNITY_BY_SLUG_BASELINE = dict(_COMMUNITY_BY_SLUG)


def _restore_community_cache() -> None:
    _COMMUNITY_PRIMITIVES[:] = _COMMUNITY_PRIMITIVES_BASELINE
    _COMMUNITY_BY_ID.clear()
    _COMMUNITY_BY_ID.update(_COMMUNITY_BY_ID_BASELINE)
    _COMMUNITY_BY_SLUG.clear()
    _COMMUNITY_BY_SLUG.update(_COMMUNITY_BY_SLUG_BASELINE)


@pytest.fixture(autouse=True)
def isolate_community_cache() -> Iterator[None]:
    _restore_community_cache()
    try:
        yield
    finally:
        _restore_community_cache()


def _fake_primitive(*, pid: uuid.UUID | None = None, **overrides: object) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.visibility = "org"
    p.primitive_type = "test_fixture"
    p.name = "Fixture"
    p.slug = "fixture"
    p.description = "A test fixture"
    p.author = "tester"
    p.version = "1.0"
    p.tags = []
    p.content_json = {}
    p.auto_update = True
    p.contribution_status = None
    p.version_group_id = None
    p.forked_from = None
    for key, value in overrides.items():
        setattr(p, key, value)
    return p


def _mock_session() -> MagicMock:
    """Return a mock AsyncSession that supports ``async with session.begin():``."""
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def _scalars_result(values: list[object]) -> MagicMock:
    r = MagicMock()
    r.scalars = MagicMock(return_value=values)
    return r


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


class TestBumpVersion:
    def test_increments_last_segment(self) -> None:
        assert _bump_version("1.0") == "1.1"
        assert _bump_version("2.3.7") == "2.3.8"

    def test_empty_version_defaults_to_1_0(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            assert _bump_version("") == "1.0"
        assert any("_bump_version called with empty string" in rec.message for rec in caplog.records)

    def test_non_numeric_last_segment_defaults_to_1_0(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING):
            assert _bump_version("1.beta") == "1.0"
        assert any("non-numeric last segment" in rec.message for rec in caplog.records)

    def test_version_without_dot(self) -> None:
        assert _bump_version("3") == "4"


class TestParseVersionKey:
    def test_none_returns_zero_tuple(self) -> None:
        assert _parse_version_key(None) == (0,)

    def test_empty_string_returns_zero_tuple(self) -> None:
        assert _parse_version_key("") == (0,)

    def test_dotted_version_parsed_to_ints(self) -> None:
        assert _parse_version_key("1.2.3") == (1, 2, 3)

    def test_non_numeric_version_returns_zero_tuple(self) -> None:
        assert _parse_version_key("v1.beta") == (0,)


class TestFilterPrimitivesEmpty:
    def test_empty_input_returns_empty_list(self) -> None:
        assert not _filter_primitives([], primitive_type="schema", search="x")

    def test_none_input_returns_empty_list(self) -> None:
        assert not _filter_primitives(None, primitive_type=None, search=None)


# ---------------------------------------------------------------------------
# contribute_fixture
# ---------------------------------------------------------------------------


class TestContributeFixture:
    async def test_creates_draft_with_fixture_metadata(self) -> None:
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        source_run_id = uuid.uuid4()
        source_pipeline_id = uuid.uuid4()
        prim_id = uuid.uuid4()

        created = _fake_primitive(pid=prim_id)
        updated = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_DRAFT)

        captured: dict = {}

        async def _capture(*args: object, **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return created

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.create_library_primitive", side_effect=_capture),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
        ):
            result = await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="My Fixture",
                slug="my-fixture",
                description="A fixture",
                tags=["test"],
                fixture_map={"in": "out"},
                source_run_id=source_run_id,
                source_pipeline_id=source_pipeline_id,
            )

        assert result is updated
        assert result.contribution_status == CONTRIBUTION_DRAFT
        assert captured["source"] == "local"
        assert captured["primitive_type"] == "test_fixture"
        assert captured["visibility"] == "org"
        assert captured["account_id"] == created_by
        assert captured["author"] == created_by.hex
        content = captured["content_json"]
        assert content["fixture_map"] == {"in": "out"}
        assert content["source_run_id"] == str(source_run_id)
        assert content["source_pipeline_id"] == str(source_pipeline_id)

    async def test_omits_none_source_ids(self) -> None:
        session = _mock_session()
        created = _fake_primitive()
        updated = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)
        captured: dict = {}

        async def _capture(*args: object, **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return created

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.create_library_primitive", side_effect=_capture),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
        ):
            await contribute_fixture(
                session,
                org_id=uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

        content = captured["content_json"]
        assert content["source_run_id"] is None
        assert content["source_pipeline_id"] is None

    async def test_raises_not_found_when_update_returns_none(self) -> None:
        session = _mock_session()
        created = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=created),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found after creation"),
        ):
            await contribute_fixture(
                session,
                org_id=uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await contribute_fixture(
                session,
                org_id=uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )


# ---------------------------------------------------------------------------
# contribute_primitive — error paths
# ---------------------------------------------------------------------------


class TestContributePrimitiveErrors:
    async def test_raises_not_found_when_update_returns_none(self) -> None:
        session = _mock_session()
        created = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=created),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found after creation"),
        ):
            await contribute_primitive(
                session,
                org_id=uuid.uuid4(),
                created_by=uuid.uuid4(),
                primitive_type="schema",
                name="n",
                slug="s",
                description=None,
                tags=[],
                content_json={},
            )

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await contribute_primitive(
                session,
                org_id=uuid.uuid4(),
                created_by=uuid.uuid4(),
                primitive_type="schema",
                name="n",
                slug="s",
                description=None,
                tags=[],
                content_json={},
            )


# ---------------------------------------------------------------------------
# submit_contribution_for_review
# ---------------------------------------------------------------------------


class TestSubmitContributionForReview:
    async def test_draft_moves_to_review_queue(self) -> None:
        session = _mock_session()
        prim_id = uuid.uuid4()

        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_DRAFT)
        updated = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
        ):
            result = await submit_contribution_for_review(session, uuid.uuid4(), prim_id, _created_by=uuid.uuid4())

        assert result.contribution_status == CONTRIBUTION_REVIEW_QUEUE

    async def test_raises_not_found_when_missing(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found"),
        ):
            await submit_contribution_for_review(session, uuid.uuid4(), uuid.uuid4(), _created_by=uuid.uuid4())

    async def test_raises_invalid_transition_for_non_draft(self) -> None:
        session = _mock_session()
        prim = _fake_primitive(contribution_status=CONTRIBUTION_PUBLISHED)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            pytest.raises(ContributionInvalidTransitionError, match="expected status 'draft'"),
        ):
            await submit_contribution_for_review(session, uuid.uuid4(), uuid.uuid4(), _created_by=uuid.uuid4())

    async def test_raises_not_found_when_update_returns_none(self) -> None:
        session = _mock_session()
        prim = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found"),
        ):
            await submit_contribution_for_review(session, uuid.uuid4(), uuid.uuid4(), _created_by=uuid.uuid4())

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await submit_contribution_for_review(session, uuid.uuid4(), uuid.uuid4(), _created_by=uuid.uuid4())


# ---------------------------------------------------------------------------
# publish_contribution — error paths + cache
# ---------------------------------------------------------------------------


class TestPublishContributionErrors:
    async def test_raises_not_found_when_update_returns_none(self) -> None:
        session = _mock_session()
        prim = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found"),
        ):
            await publish_contribution(session, uuid.uuid4(), uuid.uuid4())

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await publish_contribution(session, uuid.uuid4(), uuid.uuid4())

    async def test_notifies_importers_after_publish(self) -> None:
        session = _mock_session()
        prim_id = uuid.uuid4()

        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)
        updated = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, visibility="community")

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
            patch("modulo.core.library_service.notify_importers_of_update", new_callable=AsyncMock) as notify,
        ):
            await publish_contribution(session, uuid.uuid4(), prim_id)

        notify.assert_awaited_once()
        assert notify.await_args.args == (session, MODULO_ORG_ID, prim_id)

    async def test_deduped_publish_does_not_duplicate_cache(self) -> None:
        """Publishing a primitive already in the community cache is a no-op."""
        session = _mock_session()
        existing = _COMMUNITY_PRIMITIVES[0]
        existing.contribution_status = CONTRIBUTION_REVIEW_QUEUE

        before_count = len(_COMMUNITY_PRIMITIVES)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=existing,
            ),
            patch("modulo.core.library_service.notify_importers_of_update", new_callable=AsyncMock),
        ):
            await publish_contribution(session, uuid.uuid4(), existing.id)

        assert len(_COMMUNITY_PRIMITIVES) == before_count


# ---------------------------------------------------------------------------
# list_contributions / list_org_contributions
# ---------------------------------------------------------------------------


class TestListContributions:
    async def test_lists_only_test_fixtures(self) -> None:
        session = _mock_session()
        page = PageResult(items=[], total=0, page=1, page_size=20)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.list_library_primitives",
                new_callable=AsyncMock,
                return_value=page,
            ) as mock_list,
        ):
            result = await list_contributions(session, uuid.uuid4())

        assert not result.items
        assert mock_list.await_args.kwargs["primitive_type"] == "test_fixture"

    async def test_filters_by_status(self) -> None:
        session = _mock_session()
        draft = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)
        published = _fake_primitive(contribution_status=CONTRIBUTION_PUBLISHED)
        page = PageResult(items=[draft, published], total=2, page=1, page_size=20)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=page),
        ):
            result = await list_contributions(session, uuid.uuid4(), contribution_status=CONTRIBUTION_PUBLISHED)

        assert len(result.items) == 1
        assert result.items[0].contribution_status == CONTRIBUTION_PUBLISHED
        assert result.total == 1

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.list_library_primitives",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await list_contributions(session, uuid.uuid4())

    async def test_org_contributions_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.list_library_primitives",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await list_org_contributions(session, uuid.uuid4())


# ---------------------------------------------------------------------------
# submit_contribution_version
# ---------------------------------------------------------------------------


class TestSubmitContributionVersion:
    async def test_seeds_version_group_and_creates_bumped_draft(self) -> None:
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()

        existing = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, version="1.0")
        existing.version_group_id = None
        new_prim = _fake_primitive(version="1.1")
        updated = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)

        captured: dict = {}

        async def _capture(*args: object, **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return new_prim

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
            patch("modulo.core.library_service.create_library_primitive", side_effect=_capture),
        ):
            result = await submit_contribution_version(
                session,
                org_id,
                prim_id,
                created_by=uuid.uuid4(),
                name="Fixture v2",
                slug="fixture-v2",
                description=None,
                tags=[],
                fixture_map={},
            )

        assert result is updated
        assert captured["version"] == "1.1"
        assert captured["forked_from"] == prim_id
        assert not captured["content_json"]["fixture_map"]

    async def test_reuses_existing_version_group(self) -> None:
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        group_id = uuid.uuid4()

        existing = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, version="2.0")
        existing.version_group_id = group_id
        new_prim = _fake_primitive(version="2.1")
        updated = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)

        captured: dict = {}

        async def _capture(*args: object, **kwargs: object) -> MagicMock:
            captured.update(kwargs)
            return new_prim

        seed_calls: list[dict] = []

        async def _updater(*args: object, **kwargs: object) -> MagicMock:
            updates = kwargs.get("updates", args[2] if len(args) > 2 else {})
            seed_calls.append(dict(updates))
            return updated

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch("modulo.core.library_service.update_library_primitive", side_effect=_updater),
            patch("modulo.core.library_service.create_library_primitive", side_effect=_capture),
        ):
            await submit_contribution_version(
                session,
                org_id,
                prim_id,
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

        # version_group_id was NOT seeded (already present) — only the new
        # draft row carries it.
        assert all(
            "version_group_id" not in call or call.get("contribution_status") == CONTRIBUTION_DRAFT
            for call in seed_calls
        )
        assert captured["version"] == "2.1"

    async def test_raises_not_found_when_existing_missing(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found"),
        ):
            await submit_contribution_version(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

    async def test_raises_invalid_transition_when_not_published(self) -> None:
        session = _mock_session()
        existing = _fake_primitive(contribution_status=CONTRIBUTION_DRAFT)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            pytest.raises(ContributionInvalidTransitionError, match="expected status 'published'"),
        ):
            await submit_contribution_version(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

    async def test_raises_not_found_when_seed_update_returns_none(self) -> None:
        session = _mock_session()
        existing = _fake_primitive(contribution_status=CONTRIBUTION_PUBLISHED)
        existing.version_group_id = None

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="version group seeding"),
        ):
            await submit_contribution_version(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

    async def test_raises_not_found_when_version_update_returns_none(self) -> None:
        """The post-creation draft update returning None raises ContributionNotFoundError."""
        session = _mock_session()
        existing = _fake_primitive(contribution_status=CONTRIBUTION_PUBLISHED, version="1.0")
        existing.version_group_id = uuid.uuid4()  # already seeded -> skip seed branch
        new_prim = _fake_primitive(version="1.1")

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=new_prim,
            ),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found after creation"),
        ):
            await submit_contribution_version(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()
        existing = _fake_primitive(contribution_status=CONTRIBUTION_PUBLISHED)
        existing.version_group_id = uuid.uuid4()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await submit_contribution_version(
                session,
                uuid.uuid4(),
                uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="n",
                slug="s",
                description=None,
                tags=[],
                fixture_map={},
            )


# ---------------------------------------------------------------------------
# list_contribution_versions
# ---------------------------------------------------------------------------


class TestListContributionVersions:
    async def test_single_primitive_when_no_version_group(self) -> None:
        session = _mock_session()
        prim = _fake_primitive()
        prim.version_group_id = None

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        ):
            result = await list_contribution_versions(session, uuid.uuid4(), uuid.uuid4())

        assert result == [prim]

    async def test_returns_sorted_versions_newest_first(self) -> None:
        session = _mock_session()
        group_id = uuid.uuid4()
        prim = _fake_primitive(version="1.0")
        prim.version_group_id = group_id

        older = _fake_primitive(version="1.1")
        older.version_group_id = group_id
        newest = _fake_primitive(version="1.2")
        newest.version_group_id = group_id

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch(
                "modulo.core.library_service.list_primitives_by_version_group",
                new_callable=AsyncMock,
                return_value=[older, newest],
            ),
        ):
            result = await list_contribution_versions(session, uuid.uuid4(), uuid.uuid4())

        assert result == [newest, older, prim]

    async def test_raises_not_found_when_missing(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError, match="not found"),
        ):
            await list_contribution_versions(session, uuid.uuid4(), uuid.uuid4())

    async def test_programming_error_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            pytest.raises(ProgrammingError),
        ):
            await list_contribution_versions(session, uuid.uuid4(), uuid.uuid4())


# ---------------------------------------------------------------------------
# notify_importers_of_update
# ---------------------------------------------------------------------------


class TestNotifyImportersOfUpdate:
    async def test_noop_when_primitive_missing(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        updater.assert_not_called()

    async def test_noop_when_no_version_group(self) -> None:
        session = _mock_session()
        prim = _fake_primitive()
        prim.version_group_id = None

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        updater.assert_not_called()

    async def test_updates_auto_update_copies(self) -> None:
        session = _mock_session()
        group_id = uuid.uuid4()
        prim = _fake_primitive(version="1.1")
        prim.version_group_id = group_id

        copy1 = _fake_primitive()
        copy1.auto_update = True
        copy2 = _fake_primitive()
        copy2.auto_update = False

        session.execute = AsyncMock(return_value=_scalars_result([copy1, copy2]))

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock) as updater,
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        # Only the auto_update copy is touched.
        updater.assert_awaited_once()
        assert updater.await_args.args[1] == copy1.id
        assert updater.await_args.args[2]["update_available_version_id"] == prim.id

    async def test_per_copy_update_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _mock_session()
        group_id = uuid.uuid4()
        prim = _fake_primitive(version="1.1")
        prim.version_group_id = group_id

        copy = _fake_primitive()
        copy.auto_update = True

        session.execute = AsyncMock(return_value=_scalars_result([copy]))

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                side_effect=SQLAlchemyError("boom"),
            ),
            caplog.at_level("ERROR", logger="modulo.core.library_service"),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        assert any("failed to update copy" in rec.message and str(copy.id) in rec.message for rec in caplog.records)

    async def test_programming_error_swallowed(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=ProgrammingError("stmt", {}, Exception("no such table")),
            ),
            caplog.at_level("WARNING", logger="modulo.core.library_service"),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        assert any("failed (DB not migrated)" in rec.message for rec in caplog.records)

    async def test_cancellation_propagates(self) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=asyncio.CancelledError(),
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

    async def test_generic_error_is_logged_not_raised(self, caplog: pytest.LogCaptureFixture) -> None:
        session = _mock_session()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                side_effect=RuntimeError("boom"),
            ),
            caplog.at_level("ERROR", logger="modulo.core.library_service"),
        ):
            await notify_importers_of_update(session, uuid.uuid4(), uuid.uuid4())

        assert any("unexpected error" in rec.message for rec in caplog.records)
