"""Unit tests for the library service layer."""

import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError

from modulo.core.library_service import (
    _COMMUNITY_BY_ID,
    _COMMUNITY_BY_SLUG,
    _COMMUNITY_PRIMITIVES,
    _MODULO_BY_ID,
    _MODULO_PRIMITIVES,
    CONTRIBUTION_DRAFT,
    CONTRIBUTION_PUBLISHED,
    CONTRIBUTION_REVIEW_QUEUE,
    MODULO_ORG_ID,
    CommunityPrimitiveReadOnlyError,
    ContributionInvalidTransitionError,
    ContributionNotFoundError,
    _fetch_published_community_from_db,
    _filter_community,
    _filter_modulo,
    _resolve_primitive_types,
    contribute_primitive,
    copy_to_adapt,
    get_primitive,
    list_org_contributions,
    list_primitives,
    publish_contribution,
)
from modulo.db.crud.base import PageResult

_COMMUNITY_PRIMITIVES_BASELINE = tuple(_COMMUNITY_PRIMITIVES)
_COMMUNITY_BY_ID_BASELINE = dict(_COMMUNITY_BY_ID)
_COMMUNITY_BY_SLUG_BASELINE = dict(_COMMUNITY_BY_SLUG)

_EXPECTED_MODULO_SLUGS = {
    "agent": {"prd-ingestion", "requirements-writer", "spec-implementer"},
    "composite": {
        "approver",
        "booleaner",
        "complexity-estimator",
        "devils-advocate",
        "llm-council",
        "structured-output-enforcer",
        "triage",
    },
    "pipeline_template": {
        "incident-response-pipeline",
        "pr-review-pipeline",
        "release-checklist-pipeline",
    },
    "schema": {"prd-input", "requirements-output"},
    "test_fixture": {"example-test-fixture"},
    "workflow": {"prd-to-requirements", "simplest-workflow"},
}
_EXPECTED_COMMUNITY_SLUGS = {
    "commit-message-linter",
    "qa-reviewer",
    "translate-to-french",
}


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_primitive(
    *,
    pid: uuid.UUID | None = None,
    visibility: str = "org",
    primitive_type: str = "schema",
    name: str = "Test Prim",
    slug: str = "test-prim",
    version: str = "1.0",
    tags: list[str] | None = None,
    content_json: dict | None = None,
    tier: str = "native",
) -> MagicMock:
    p = MagicMock()
    p.id = pid or uuid.uuid4()
    p.visibility = visibility
    p.primitive_type = primitive_type
    p.name = name
    p.slug = slug
    p.description = "A test primitive"
    p.author = "tester"
    p.version = version
    p.tags = tags or []
    p.content_json = content_json or {}
    p.tier = tier
    return p


def _mock_session() -> MagicMock:
    """Return a mock AsyncSession that supports `async with session.begin():`."""
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


def _capture_create_with(captured: dict, return_value: MagicMock):
    """Return an async create_library_primitive side effect that records kwargs."""

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return return_value

    return _capture


# ---------------------------------------------------------------------------
# _filter_modulo
# ---------------------------------------------------------------------------


def test_filter_modulo_no_filters():
    results = _filter_modulo(primitive_type=None, search=None)
    assert len(results) == len(_MODULO_PRIMITIVES)


@pytest.mark.parametrize(("primitive_type", "expected_slugs"), _EXPECTED_MODULO_SLUGS.items())
def test_filter_modulo_by_type(primitive_type: str, expected_slugs: set[str]):
    results = _filter_modulo(primitive_type=primitive_type, search=None)
    assert all(p.primitive_type == primitive_type for p in results)
    assert {p.slug for p in results} == expected_slugs


# ---------------------------------------------------------------------------
# _resolve_primitive_types
# ---------------------------------------------------------------------------


def test_resolve_primitive_types_plural_wins():
    assert _resolve_primitive_types("schema", ["workflow", "agent"]) == ["workflow", "agent"]


def test_resolve_primitive_types_single_fallback():
    assert _resolve_primitive_types("schema", None) == ["schema"]


def test_resolve_primitive_types_none_when_unfiltered():
    assert _resolve_primitive_types(None, None) is None


def test_filter_modulo_multi_type():
    results = _filter_modulo(primitive_type=None, primitive_types=["agent", "workflow"], search=None)
    assert {p.slug for p in results} == _EXPECTED_MODULO_SLUGS["agent"] | _EXPECTED_MODULO_SLUGS["workflow"]
    assert all(p.primitive_type in {"agent", "workflow"} for p in results)


def test_filter_community_multi_type():
    results = _filter_community(primitive_type=None, primitive_types=["schema", "workflow"], search=None)
    assert all(p.primitive_type in {"schema", "workflow"} for p in results)


# ---------------------------------------------------------------------------
# _filter_community — community database (ADR 010 §2)
# ---------------------------------------------------------------------------


def test_filter_community_no_filters():
    results = _filter_community(primitive_type=None, search=None)
    assert len(results) == len(_COMMUNITY_PRIMITIVES)
    assert {p.slug for p in results} == _EXPECTED_COMMUNITY_SLUGS


def test_filter_community_items_are_source_community_and_unverified():
    results = _filter_community(primitive_type=None, search=None)
    for p in results:
        assert p.source == "community"
        assert p.verified is False
        assert p.visibility == "community"


def test_filter_community_by_search():
    results = _filter_community(primitive_type=None, search="French")
    assert len(results) == 1
    assert results[0].slug == "translate-to-french"


def test_filter_community_no_match():
    assert not _filter_community(primitive_type=None, search="zzz_no_match_zzz")


def test_community_by_id_index():
    for p in _COMMUNITY_PRIMITIVES:
        assert _COMMUNITY_BY_ID[p.id] is p


def test_filter_modulo_by_search():
    results = _filter_modulo(primitive_type=None, search="PRD")
    assert len(results) >= 1
    assert all("prd" in p.name.lower() or "prd" in (p.description or "").lower() for p in results)


def test_filter_modulo_no_match():
    results = _filter_modulo(primitive_type=None, search="zzz_no_match_zzz")
    assert results == []


# ---------------------------------------------------------------------------
# Community primitive constants
# ---------------------------------------------------------------------------


def test_community_primitives_have_correct_visibility():
    for p in _COMMUNITY_PRIMITIVES:
        assert p.visibility == "community"
        assert p.organisation_id == MODULO_ORG_ID


def test_community_primitives_count():
    actual = {(p.primitive_type, p.slug) for p in _MODULO_PRIMITIVES}
    expected = {(primitive_type, slug) for primitive_type, slugs in _EXPECTED_MODULO_SLUGS.items() for slug in slugs}
    assert actual == expected


def test_modulo_by_id_index():
    for p in _MODULO_PRIMITIVES:
        assert _MODULO_BY_ID[p.id] is p


# ---------------------------------------------------------------------------
# get_primitive
# ---------------------------------------------------------------------------


async def test_get_primitive_found_in_org():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
    ):
        result = await get_primitive(session, org_id, prim.id)

    assert result is prim


async def test_get_primitive_falls_back_to_community():
    session = _mock_session()
    org_id = uuid.uuid4()
    community_prim = _MODULO_PRIMITIVES[0]

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
    ):
        result = await get_primitive(session, org_id, community_prim.id)

    assert result is community_prim


async def test_get_primitive_not_found_returns_none():
    session = _mock_session()
    org_id = uuid.uuid4()
    unknown_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
    ):
        result = await get_primitive(session, org_id, unknown_id)

    assert result is None


# ---------------------------------------------------------------------------
# list_primitives
# ---------------------------------------------------------------------------


async def test_list_primitives_merges_community():
    session = _mock_session()
    org_id = uuid.uuid4()
    org_prim = _fake_primitive()
    org_page: PageResult = PageResult(items=[org_prim], total=1, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
    ):
        result = await list_primitives(session, org_id)

    assert org_prim in result.items
    assert any(p.visibility == "community" for p in result.items)
    assert result.total > 1


async def test_list_primitives_exclude_community():
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
    ):
        result = await list_primitives(session, org_id, include_community=False)

    assert not result.items
    assert result.total == 0


async def test_list_primitives_source_community_only():
    """?source=community returns only the community-database items — no Native, no org items."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_prim = _fake_primitive()
    org_prim.source = "local"
    org_page: PageResult = PageResult(items=[org_prim], total=1, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
    ):
        result = await list_primitives(session, org_id, source="community")

    assert org_prim not in result.items
    assert {p.slug for p in result.items} == _EXPECTED_COMMUNITY_SLUGS
    assert all(p.source == "community" for p in result.items)
    assert all(p.verified is False for p in result.items)


async def test_list_primitives_source_modulo_excludes_community():
    """?source=modulo returns only Native library items — no community-database items."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
    ):
        result = await list_primitives(session, org_id, source="modulo")

    assert all(p.source == "modulo" for p in result.items)
    assert not any(p.source == "community" for p in result.items)


async def test_list_primitives_default_merges_community_database():
    """Default (no source filter) merges org items, Native, and community-database items."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
    ):
        result = await list_primitives(session, org_id)

    assert any(p.source == "modulo" for p in result.items)
    assert any(p.source == "community" for p in result.items)


async def test_list_primitives_type_filter_propagated():
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page
        ) as mock_list,
    ):
        result = await list_primitives(session, org_id, primitive_type="schema")

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["primitive_type"] == "schema"
    # Community result should also be filtered
    assert all(p.primitive_type == "schema" for p in result.items)


async def test_list_primitives_multi_type_filter_propagated():
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page
        ) as mock_list,
    ):
        result = await list_primitives(session, org_id, primitive_types=["workflow", "agent"])

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["primitive_types"] == ["workflow", "agent"]
    assert call_kwargs["primitive_type"] is None
    # Plural filter takes precedence and applies to in-memory Native + community results too
    assert all(p.primitive_type in {"workflow", "agent"} for p in result.items)


async def test_list_primitives_plural_types_win_over_single():
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page
        ) as mock_list,
    ):
        result = await list_primitives(session, org_id, primitive_type="schema", primitive_types=["workflow"])

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["primitive_types"] == ["workflow"]
    assert call_kwargs["primitive_type"] == "schema"
    assert all(p.primitive_type == "workflow" for p in result.items)


async def test_list_primitives_passes_excluded_tiers_to_crud():
    """excluded_tiers is forwarded to list_library_primitives."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page
        ) as mock_list,
    ):
        await list_primitives(session, org_id, excluded_tiers=["preview"])

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["excluded_tiers"] == ["preview"]


async def test_list_primitives_default_excluded_tiers_is_in_dev():
    """Default (no excluded_tiers) passes ["in_dev"] to list_library_primitives."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page
        ) as mock_list,
    ):
        await list_primitives(session, org_id)

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["excluded_tiers"] == ["in_dev"]


async def test_list_primitives_filters_in_dev_modulo_items():
    """In-memory modulo items with tier='in_dev' are excluded by default."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    native_prim = _fake_primitive()
    native_prim.tier = "native"
    in_dev_prim = _fake_primitive()
    in_dev_prim.tier = "in_dev"
    modulo_with_in_dev = [native_prim, in_dev_prim]

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
        patch("modulo.core.library_service._filter_modulo", return_value=modulo_with_in_dev),
        patch("modulo.core.library_service._filter_community", return_value=[]),
    ):
        result = await list_primitives(session, org_id)

    assert native_prim in result.items
    assert in_dev_prim not in result.items


async def test_list_primitives_filters_in_dev_community_items():
    """In-memory community items with tier='in_dev' are excluded by default."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    native_prim = _fake_primitive()
    native_prim.tier = "native"
    in_dev_prim = _fake_primitive()
    in_dev_prim.tier = "in_dev"
    community_with_in_dev = [native_prim, in_dev_prim]

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
        patch("modulo.core.library_service._filter_modulo", return_value=[]),
        patch("modulo.core.library_service._filter_community", return_value=community_with_in_dev),
    ):
        result = await list_primitives(session, org_id)

    assert native_prim in result.items
    assert in_dev_prim not in result.items


async def test_list_primitives_custom_excluded_tiers_filters_modulo():
    """Passing excluded_tiers=["preview"] filters preview items from modulo."""
    session = _mock_session()
    org_id = uuid.uuid4()
    org_page: PageResult = PageResult(items=[], total=0, page=1, page_size=20)

    native_prim = _fake_primitive()
    native_prim.tier = "native"
    preview_prim = _fake_primitive()
    preview_prim.tier = "preview"
    modulo_with_preview = [native_prim, preview_prim]

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=org_page),
        patch("modulo.core.library_service._filter_modulo", return_value=modulo_with_preview),
        patch("modulo.core.library_service._filter_community", return_value=[]),
    ):
        result = await list_primitives(session, org_id, excluded_tiers=["preview"])

    assert native_prim in result.items
    assert preview_prim not in result.items


# ---------------------------------------------------------------------------
# copy_to_adapt
# ---------------------------------------------------------------------------


async def test_copy_to_adapt_community_via_mcp_raises():
    session = _mock_session()
    org_id = uuid.uuid4()
    community_prim = _MODULO_PRIMITIVES[0]

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
        pytest.raises(CommunityPrimitiveReadOnlyError),
    ):
        await copy_to_adapt(session, org_id, community_prim.id, via_mcp=True)


async def test_copy_to_adapt_community_via_browser_succeeds():
    session = _mock_session()
    org_id = uuid.uuid4()
    community_prim = _MODULO_PRIMITIVES[0]
    copied = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
        patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=copied),
    ):
        result = await copy_to_adapt(session, org_id, community_prim.id, via_mcp=False)

    assert result is copied


async def test_copy_to_adapt_org_primitive_succeeds():
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org")
    copied = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=copied),
    ):
        result = await copy_to_adapt(session, org_id, source.id, via_mcp=True)

    assert result is copied


async def test_copy_to_adapt_not_found_raises():
    session = _mock_session()
    org_id = uuid.uuid4()
    missing_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
        pytest.raises(LookupError, match=str(missing_id)),
    ):
        await copy_to_adapt(session, org_id, missing_id)


async def test_copy_to_adapt_bumps_version():
    """Verify the new version is minor-bumped from the source."""
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org", version="2.3")
    copied = _fake_primitive()

    captured: dict = {}

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch(
            "modulo.core.library_service.create_library_primitive", side_effect=_capture_create_with(captured, copied)
        ),
    ):
        await copy_to_adapt(session, org_id, source.id)

    assert captured["version"] == "2.4"


async def test_copy_to_adapt_propagates_tier():
    """A preview/in_dev primitive must keep its tier when copied (no silent downgrade to native)."""
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org", tier="preview")
    copied = _fake_primitive()

    captured: dict = {}

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch(
            "modulo.core.library_service.create_library_primitive", side_effect=_capture_create_with(captured, copied)
        ),
    ):
        await copy_to_adapt(session, org_id, source.id)

    assert captured["tier"] == "preview"


async def test_copy_to_adapt_native_tier_defaults():
    """A native primitive keeps native tier on copy."""
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org", tier="native")
    copied = _fake_primitive()

    captured: dict = {}

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch(
            "modulo.core.library_service.create_library_primitive", side_effect=_capture_create_with(captured, copied)
        ),
    ):
        await copy_to_adapt(session, org_id, source.id)

    assert captured["tier"] == "native"


# ---------------------------------------------------------------------------
# contribute_primitive
# ---------------------------------------------------------------------------


async def test_contribute_primitive_creates_draft():
    session = _mock_session()
    org_id = uuid.uuid4()
    created_by = uuid.uuid4()
    primitive_id = uuid.uuid4()

    created = _fake_primitive(pid=primitive_id)
    created.contribution_status = None
    updated = _fake_primitive(pid=primitive_id)
    updated.contribution_status = CONTRIBUTION_DRAFT

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.create_library_primitive", new_callable=AsyncMock, return_value=created),
        patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
    ):
        result = await contribute_primitive(
            session,
            org_id=org_id,
            created_by=created_by,
            primitive_type="schema",
            name="Test Schema",
            slug="test-schema",
            description="A test schema",
            tags=["test"],
            content_json={"fields": []},
            source_url="https://example.com/schema.json",
        )

    assert result.contribution_status == CONTRIBUTION_DRAFT
    assert result.id == primitive_id


async def test_contribute_primitive_forwards_correct_fields():
    session = _mock_session()
    org_id = uuid.uuid4()
    created_by = uuid.uuid4()
    primitive_id = uuid.uuid4()

    created = _fake_primitive(pid=primitive_id)
    updated = _fake_primitive(pid=primitive_id)
    updated.contribution_status = CONTRIBUTION_DRAFT

    captured: dict = {}

    async def _capture_create(*args, **kwargs):
        captured.update(kwargs)
        return created

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.create_library_primitive", side_effect=_capture_create),
        patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
    ):
        await contribute_primitive(
            session,
            org_id=org_id,
            created_by=created_by,
            primitive_type="agent",
            name="Test Agent",
            slug="test-agent",
            description="An agent",
            tags=["agent", "test"],
            content_json={"prompt": "hello"},
            source_url=None,
        )

    assert captured["org_id"] == org_id
    assert captured["source"] == "local"
    assert captured["primitive_type"] == "agent"
    assert captured["name"] == "Test Agent"
    assert captured["slug"] == "test-agent"
    assert captured["description"] == "An agent"
    assert captured["author"] == created_by.hex
    assert captured["version"] == "1.0"
    assert captured["tags"] == ["agent", "test"]
    assert captured["visibility"] == "org"
    assert captured["account_id"] == created_by
    assert captured["source_url"] is None


# ---------------------------------------------------------------------------
# list_org_contributions
# ---------------------------------------------------------------------------


async def test_list_org_contributions_returns_all_when_no_status_filter():
    session = _mock_session()
    org_id = uuid.uuid4()

    draft = _fake_primitive()
    draft.contribution_status = CONTRIBUTION_DRAFT
    published = _fake_primitive()
    published.contribution_status = CONTRIBUTION_PUBLISHED
    page = PageResult(items=[draft, published], total=2, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=page),
    ):
        result = await list_org_contributions(session, org_id)

    assert len(result.items) == 2
    assert result.total == 2


async def test_list_org_contributions_filters_by_status():
    session = _mock_session()
    org_id = uuid.uuid4()

    draft = _fake_primitive()
    draft.contribution_status = CONTRIBUTION_DRAFT
    review = _fake_primitive()
    review.contribution_status = CONTRIBUTION_REVIEW_QUEUE
    page = PageResult(items=[draft, review], total=2, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=page),
    ):
        result = await list_org_contributions(session, org_id, contribution_status=CONTRIBUTION_REVIEW_QUEUE)

    assert len(result.items) == 1
    assert result.items[0].contribution_status == CONTRIBUTION_REVIEW_QUEUE
    assert result.total == 1


async def test_list_org_contributions_empty_when_no_match():
    session = _mock_session()
    org_id = uuid.uuid4()

    draft = _fake_primitive()
    draft.contribution_status = CONTRIBUTION_DRAFT
    page = PageResult(items=[draft], total=1, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.list_library_primitives", new_callable=AsyncMock, return_value=page),
    ):
        result = await list_org_contributions(session, org_id, contribution_status=CONTRIBUTION_PUBLISHED)

    assert not result.items
    assert result.total == 0


async def test_list_org_contributions_passes_page_params():
    session = _mock_session()
    org_id = uuid.uuid4()

    page = PageResult(items=[], total=0, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives",
            new_callable=AsyncMock,
            return_value=page,
        ) as mock_list,
    ):
        await list_org_contributions(session, org_id, page=2, page_size=10)

    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["page"] == 2
    assert call_kwargs["page_size"] == 10


# ---------------------------------------------------------------------------
# _fetch_published_community_from_db
# ---------------------------------------------------------------------------


async def test_fetch_published_community_from_db_returns_empty_on_programming_error():
    session = _mock_session()
    org_id = uuid.uuid4()
    session.info = {}
    session.execute = AsyncMock(side_effect=ProgrammingError("mock", {}, ""))

    result = await _fetch_published_community_from_db(session, org_id)
    assert result == []


async def test_fetch_published_community_from_db_returns_empty_on_generic_error():
    session = _mock_session()
    org_id = uuid.uuid4()
    session.info = {}
    session.execute = AsyncMock(side_effect=RuntimeError("boom"))

    result = await _fetch_published_community_from_db(session, org_id)
    assert result == []


async def test_fetch_published_community_from_db_saves_and_restores_tenant():
    session = _mock_session()
    org_id = uuid.uuid4()
    saved_tenant = uuid.uuid4()
    session.info = {"org_id": saved_tenant}

    mock_item = _fake_primitive()
    mock_item.contribution_status = CONTRIBUTION_PUBLISHED
    mock_item.visibility = "community"

    mock_result = MagicMock()
    mock_result.scalars = MagicMock(return_value=[mock_item])
    session.execute = AsyncMock(return_value=mock_result)

    result = await _fetch_published_community_from_db(session, org_id)

    assert len(result) == 1
    assert result[0].id == mock_item.id
    assert session.info["org_id"] == saved_tenant


# ---------------------------------------------------------------------------
# publish_contribution
# ---------------------------------------------------------------------------


async def test_publish_contribution_accepts_draft_status():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim_id = uuid.uuid4()

    prim = _fake_primitive(pid=prim_id, visibility="org")
    prim.contribution_status = CONTRIBUTION_DRAFT

    updated = _fake_primitive(pid=prim_id, visibility="community")
    updated.contribution_status = CONTRIBUTION_PUBLISHED

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
        patch("modulo.core.library_service.notify_importers_of_update", new_callable=AsyncMock),
    ):
        result = await publish_contribution(session, org_id, prim_id)

    assert result.contribution_status == CONTRIBUTION_PUBLISHED
    assert result.visibility == "community"


async def test_publish_contribution_accepts_review_queue_status():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim_id = uuid.uuid4()

    prim = _fake_primitive(pid=prim_id, visibility="org")
    prim.contribution_status = CONTRIBUTION_REVIEW_QUEUE

    updated = _fake_primitive(pid=prim_id, visibility="community")
    updated.contribution_status = CONTRIBUTION_PUBLISHED

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
        patch("modulo.core.library_service.notify_importers_of_update", new_callable=AsyncMock),
    ):
        result = await publish_contribution(session, org_id, prim_id)

    assert result.contribution_status == CONTRIBUTION_PUBLISHED


async def test_publish_contribution_raises_for_published_status():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim_id = uuid.uuid4()

    prim = _fake_primitive(pid=prim_id)
    prim.contribution_status = CONTRIBUTION_PUBLISHED

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        pytest.raises(ContributionInvalidTransitionError),
    ):
        await publish_contribution(session, org_id, prim_id)


async def test_publish_contribution_raises_for_none_status():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim_id = uuid.uuid4()

    prim = _fake_primitive(pid=prim_id)
    prim.contribution_status = None

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        pytest.raises(ContributionInvalidTransitionError),
    ):
        await publish_contribution(session, org_id, prim_id)


async def test_publish_contribution_raises_not_found():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim_id = uuid.uuid4()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=None),
        pytest.raises(ContributionNotFoundError),
    ):
        await publish_contribution(session, org_id, prim_id)


async def test_publish_contribution_updates_in_memory_cache():
    session = _mock_session()
    org_id = uuid.uuid4()
    prim_id = uuid.uuid4()

    prim = _fake_primitive(pid=prim_id)
    prim.contribution_status = CONTRIBUTION_DRAFT
    prim.slug = "test-prim"
    prim.primitive_type = "schema"

    updated = _fake_primitive(pid=prim_id)
    updated.contribution_status = CONTRIBUTION_PUBLISHED
    updated.visibility = "community"
    updated.slug = "test-prim"
    updated.primitive_type = "schema"

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=prim),
        patch("modulo.core.library_service.update_library_primitive", new_callable=AsyncMock, return_value=updated),
        patch("modulo.core.library_service.notify_importers_of_update", new_callable=AsyncMock),
    ):
        await publish_contribution(session, org_id, prim_id)

    assert updated in _COMMUNITY_PRIMITIVES
    assert _COMMUNITY_BY_ID[updated.id] is updated
    assert _COMMUNITY_BY_SLUG[("schema", "test-prim")] is updated
