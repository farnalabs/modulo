"""Unit tests for the fixture contribution flow in library_service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.library_service import (
    _MODULO_PRIMITIVES,
    CONTRIBUTION_DRAFT,
    CONTRIBUTION_PUBLISHED,
    CONTRIBUTION_REVIEW_QUEUE,
    MODULO_ORG_ID,
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
    contribute_fixture,
    list_contribution_versions,
    list_contributions,
    publish_contribution,
    submit_contribution_for_review,
    submit_contribution_version,
)


def _mock_session() -> MagicMock:
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def _fake_primitive(
    *,
    pid: uuid.UUID | None = None,
    contribution_status: str | None = CONTRIBUTION_DRAFT,
    visibility: str = "org",
    **overrides,
) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.contribution_status = contribution_status
    p.visibility = visibility
    for k, v in overrides.items():
        setattr(p, k, v)
    return p


class TestContributeFixture:
    """Tests for contribute_fixture()."""

    async def test_creates_draft_fixture(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_create,
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_update,
        ):
            result = await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="My Fixture",
                slug="my-fixture",
                description="A test fixture",
                tags=["test"],
                fixture_map={"input": "output"},
            )

        assert result is prim
        mock_create.assert_awaited_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["primitive_type"] == "test_fixture"
        assert call_kwargs["name"] == "My Fixture"
        assert call_kwargs["slug"] == "my-fixture"
        assert call_kwargs["visibility"] == "org"
        assert call_kwargs["author"] == created_by.hex
        assert call_kwargs["account_id"] == created_by
        assert call_kwargs["content_json"]["fixture_map"] == {"input": "output"}
        mock_update.assert_awaited_once_with(session, prim.id, {"contribution_status": CONTRIBUTION_DRAFT})

    async def test_creates_draft_with_no_description(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_create,
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
        ):
            await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="Minimal",
                slug="minimal",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["description"] is None

    async def test_with_source_references(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        run_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()
        prim = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_create,
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
        ):
            await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="With Source",
                slug="with-source",
                description="From a run",
                tags=["auto"],
                fixture_map={"p": "r"},
                source_run_id=run_id,
                source_pipeline_id=pipeline_id,
            )

        content = mock_create.call_args.kwargs["content_json"]
        assert content["source_run_id"] == str(run_id)
        assert content["source_pipeline_id"] == str(pipeline_id)

    async def test_with_owner_team(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        team_id = uuid.uuid4()
        prim = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ) as mock_create,
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
        ):
            await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="Team Fixture",
                slug="team-fixture",
                description=None,
                tags=[],
                fixture_map={"x": "y"},
                owner_team_id=team_id,
            )

        assert mock_create.call_args.kwargs["owner_team_id"] == team_id

    async def test_returns_update_when_available(self):
        """contribute_fixture returns the update result if it succeeds."""
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim = _fake_primitive()
        updated_prim = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=updated_prim,
            ),
        ):
            result = await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="X",
                slug="x",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )

        assert result is updated_prim

    async def test_raises_when_update_returns_none(self):
        """When update_library_primitive returns None, raise ContributionNotFoundError."""
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim = _fake_primitive()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.create_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ContributionNotFoundError),
        ):
            await contribute_fixture(
                session,
                org_id=org_id,
                created_by=created_by,
                name="X",
                slug="x",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )


class TestSubmitForReview:
    """Tests for submit_contribution_for_review()."""

    async def test_submit_draft_to_review_queue(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_DRAFT)
        updated = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=updated,
            ),
        ):
            result = await submit_contribution_for_review(session, org_id, prim_id, _created_by=created_by)

        assert result.contribution_status == CONTRIBUTION_REVIEW_QUEUE
        assert result is updated

    async def test_submit_not_found_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ContributionNotFoundError, match=str(prim_id)),
        ):
            await submit_contribution_for_review(session, org_id, prim_id, _created_by=uuid.uuid4())

    async def test_submit_when_already_published_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            pytest.raises(ContributionInvalidTransitionError, match=str(prim_id)),
        ):
            await submit_contribution_for_review(session, org_id, prim_id, _created_by=uuid.uuid4())

    async def test_submit_when_already_in_review_queue_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            pytest.raises(ContributionInvalidTransitionError),
        ):
            await submit_contribution_for_review(session, org_id, prim_id, _created_by=uuid.uuid4())

    async def test_submit_update_returns_none_raises_not_found(self):
        """If the update returns None (e.g. concurrent delete), raise."""
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_DRAFT)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ContributionNotFoundError),
        ):
            await submit_contribution_for_review(session, org_id, prim_id, _created_by=created_by)


class TestPublish:
    """Tests for publish_contribution()."""

    async def test_publish_reviewed_contribution(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)
        updated = _fake_primitive(
            pid=prim_id,
            contribution_status=CONTRIBUTION_PUBLISHED,
            visibility="community",
        )

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=updated,
            ) as mock_update,
            patch(
                "modulo.core.library_service.notify_importers_of_update",
                new_callable=AsyncMock,
            ),
        ):
            result = await publish_contribution(session, org_id, prim_id)

        assert result.contribution_status == CONTRIBUTION_PUBLISHED
        assert result.visibility == "community"
        mock_update.assert_awaited_once_with(
            session,
            prim_id,
            {
                "contribution_status": CONTRIBUTION_PUBLISHED,
                "visibility": "community",
                "organisation_id": MODULO_ORG_ID,
            },
        )

    async def test_publish_not_found_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ContributionNotFoundError, match=str(prim_id)),
        ):
            await publish_contribution(session, org_id, prim_id)

    async def test_publish_from_draft_succeeds(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_DRAFT)
        updated = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, visibility="community")

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=updated,
            ),
            patch("modulo.core.library_service.notify_importers_of_update", new_callable=AsyncMock),
        ):
            result = await publish_contribution(session, org_id, prim_id)
            assert result.contribution_status == CONTRIBUTION_PUBLISHED
            assert result.visibility == "community"

    async def test_publish_already_published_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            pytest.raises(ContributionInvalidTransitionError),
        ):
            await publish_contribution(session, org_id, prim_id)

    async def test_publish_update_returns_none_raises_not_found(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        prim = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.get_library_primitive",
                new_callable=AsyncMock,
                return_value=prim,
            ),
            patch(
                "modulo.core.library_service.update_library_primitive",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ContributionNotFoundError),
        ):
            await publish_contribution(session, org_id, prim_id)


class TestListContributions:
    """Tests for list_contributions()."""

    async def test_filters_by_test_fixture_type(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim = _fake_primitive()
        page_result = MagicMock()
        page_result.items = [prim]
        page_result.total = 1
        page_result.page = 1
        page_result.page_size = 20

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.list_library_primitives",
                new_callable=AsyncMock,
                return_value=page_result,
            ) as mock_list,
        ):
            result = await list_contributions(session, org_id)

        mock_list.assert_awaited_once()
        assert mock_list.call_args.kwargs["primitive_type"] == "test_fixture"
        assert result.total == 1
        assert result.items == [prim]

    async def test_empty_result(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        page_result = MagicMock()
        page_result.items = []
        page_result.total = 0

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.list_library_primitives",
                new_callable=AsyncMock,
                return_value=page_result,
            ),
        ):
            result = await list_contributions(session, org_id)

        assert result.total == 0
        assert not result.items

    async def test_contribution_status_param_filters_results(self):
        """contribution_status param filters results from CRUD layer."""
        session = _mock_session()
        org_id = uuid.uuid4()
        draft_prim = _fake_primitive(contribution_status="draft")
        published_prim = _fake_primitive(contribution_status="published")
        page_result = MagicMock()
        page_result.items = [draft_prim, published_prim]
        page_result.total = 2

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch(
                "modulo.core.library_service.list_library_primitives",
                new_callable=AsyncMock,
                return_value=page_result,
            ),
        ):
            result = await list_contributions(session, org_id, contribution_status="draft")

        assert len(result.items) == 1
        assert result.items[0] is draft_prim
        assert result.total == 1


class TestCommunityFixturePrimitive:
    """Built-in community primitives include one test_fixture."""

    def test_example_test_fixture_exists_in_community(self):
        fixtures = [p for p in _MODULO_PRIMITIVES if p.primitive_type == "test_fixture"]
        assert len(fixtures) == 1
        fixture = fixtures[0]
        assert fixture.name == "Example Test Fixture"
        assert fixture.slug == "example-test-fixture"
        assert fixture.contribution_status == "published"

    def test_example_fixture_has_fixture_map(self):
        fixtures = [p for p in _MODULO_PRIMITIVES if p.primitive_type == "test_fixture"]
        content = fixtures[0].content_json
        assert "fixture_map" in content
        assert len(content["fixture_map"]) == 2


class TestContributionConstants:
    """Contribution status constants are defined correctly."""

    def test_draft(self):
        assert CONTRIBUTION_DRAFT == "draft"

    def test_review_queue(self):
        assert CONTRIBUTION_REVIEW_QUEUE == "review_queue"

    def test_published(self):
        assert CONTRIBUTION_PUBLISHED == "published"


class TestSubmitContributionVersion:
    """Tests for submit_contribution_version()."""

    async def test_submit_new_version_creates_draft(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim_id = uuid.uuid4()
        group_id = uuid.uuid4()
        existing = _fake_primitive(
            pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, version="1.1", version_group_id=group_id
        )
        new_prim = _fake_primitive(pid=uuid.uuid4(), contribution_status=CONTRIBUTION_DRAFT, version="1.2")

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=new_prim
            ) as mock_create,
            patch(
                "modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=new_prim
            ),
        ):
            result = await submit_contribution_version(
                session,
                org_id,
                prim_id,
                created_by=created_by,
                name="Fixture v2",
                slug="fixture-v2",
                description="Updated",
                tags=["v2"],
                fixture_map={"a": "b"},
            )

        assert result is new_prim
        mock_create.assert_awaited_once()
        assert mock_create.call_args.kwargs["version"] == "1.2"

    async def test_submit_version_not_found_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError),
        ):
            await submit_contribution_version(
                session,
                org_id,
                uuid.uuid4(),
                created_by=uuid.uuid4(),
                name="X",
                slug="x",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )

    async def test_submit_version_on_draft_raises_invalid_transition(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        existing = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_DRAFT)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            pytest.raises(ContributionInvalidTransitionError),
        ):
            await submit_contribution_version(
                session,
                org_id,
                prim_id,
                created_by=uuid.uuid4(),
                name="X",
                slug="x",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )

    async def test_submit_version_update_returns_none_raises_not_found(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim_id = uuid.uuid4()
        existing = _fake_primitive(pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, version="1.0")
        new_prim = _fake_primitive(pid=uuid.uuid4(), contribution_status=CONTRIBUTION_DRAFT, version="1.1")

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=new_prim
            ),
            patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError),
        ):
            await submit_contribution_version(
                session,
                org_id,
                prim_id,
                created_by=created_by,
                name="X",
                slug="x",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )

    async def test_submit_version_seeds_version_group_id(self):
        """When existing has no version_group_id, it gets seeded to its own id."""
        session = _mock_session()
        org_id = uuid.uuid4()
        created_by = uuid.uuid4()
        prim_id = uuid.uuid4()
        existing = _fake_primitive(
            pid=prim_id, contribution_status=CONTRIBUTION_PUBLISHED, version_group_id=None, version="1.0"
        )
        new_prim = _fake_primitive(pid=uuid.uuid4(), contribution_status=CONTRIBUTION_DRAFT, version="1.1")

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=new_prim
            ),
            patch(
                "modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=new_prim
            ) as mock_update,
        ):
            await submit_contribution_version(
                session,
                org_id,
                prim_id,
                created_by=created_by,
                name="X",
                slug="x",
                description=None,
                tags=[],
                fixture_map={"a": "b"},
            )

        # Should seed version_group_id to prim_id
        seed_calls = [c for c in mock_update.call_args_list if c.args[2].get("version_group_id") == prim_id]
        assert len(seed_calls) >= 1, "version_group_id should be seeded when missing"


class TestListContributionVersions:
    """Tests for list_contribution_versions()."""

    async def test_list_versions_returns_all_versions(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        group_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        existing = _fake_primitive(pid=prim_id, version="1.0", version_group_id=group_id)
        v2 = _fake_primitive(pid=uuid.uuid4(), version="1.1", version_group_id=group_id)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.list_primitives_by_version_group",
                new_callable=AsyncMock,
                return_value=[v2, existing],
            ),
        ):
            result = await list_contribution_versions(session, org_id, prim_id)

        assert len(result) == 2

    async def test_list_versions_not_found_raises(self):
        session = _mock_session()
        org_id = uuid.uuid4()

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
            pytest.raises(ContributionNotFoundError),
        ):
            await list_contribution_versions(session, org_id, uuid.uuid4())

    async def test_list_versions_no_group_id_returns_single(self):
        session = _mock_session()
        org_id = uuid.uuid4()
        prim_id = uuid.uuid4()
        existing = _fake_primitive(pid=prim_id, version="1.0", version_group_id=None)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.list_primitives_by_version_group", new_callable=AsyncMock, return_value=[]
            ),
        ):
            result = await list_contribution_versions(session, org_id, prim_id)

        assert len(result) == 1
        assert result[0] is existing

    async def test_list_versions_includes_seed_primitive(self):
        """When seed primitive has version_group_id set but doesn't appear in results, append it."""
        session = _mock_session()
        org_id = uuid.uuid4()
        group_id = prim_id = uuid.uuid4()
        existing = _fake_primitive(pid=prim_id, version="1.0", version_group_id=group_id)
        v2 = _fake_primitive(pid=uuid.uuid4(), version="1.1", version_group_id=group_id)

        with (
            patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
            patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=existing),
            patch(
                "modulo.core.library_service.list_primitives_by_version_group",
                new_callable=AsyncMock,
                return_value=[v2],
            ),
        ):
            result = await list_contribution_versions(session, org_id, prim_id)

        assert len(result) == 2
        assert existing in result
