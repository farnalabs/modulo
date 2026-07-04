"""Unit tests for the library service layer."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.library_service import (
    _COMMUNITY_BY_ID,
    _COMMUNITY_PRIMITIVES,
    _MODULO_BY_ID,
    _MODULO_PRIMITIVES,
    MODULO_ORG_ID,
    CommunityPrimitiveReadOnlyError,
    _filter_community,
    _filter_modulo,
    copy_to_adapt,
    get_primitive,
    list_primitives,
)
from modulo.db.crud.base import PageResult

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


# ---------------------------------------------------------------------------
# _filter_modulo
# ---------------------------------------------------------------------------


def test_filter_modulo_no_filters():
    results = _filter_modulo(primitive_type=None, search=None)
    assert len(results) == len(_MODULO_PRIMITIVES)


def test_filter_modulo_by_type():
    schemas = _filter_modulo(primitive_type="schema", search=None)
    assert all(p.primitive_type == "schema" for p in schemas)
    assert len(schemas) == 7


# ---------------------------------------------------------------------------
# _filter_community — community database (ADR 010 §2)
# ---------------------------------------------------------------------------


def test_filter_community_no_filters():
    results = _filter_community(primitive_type=None, search=None)
    assert len(results) == len(_COMMUNITY_PRIMITIVES)
    assert len(results) == 3


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
    assert _filter_community(primitive_type=None, search="zzz_no_match_zzz") == []


def test_community_by_id_index():
    for p in _COMMUNITY_PRIMITIVES:
        assert _COMMUNITY_BY_ID[p.id] is p


def test_filter_modulo_by_type_agent():
    agents = _filter_modulo(primitive_type="agent", search=None)
    assert len(agents) == 7


def test_filter_modulo_by_type_workflow():
    workflows = _filter_modulo(primitive_type="workflow", search=None)
    assert len(workflows) == 2


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


def test_community_primitives_have_correct_visibiliy():
    for p in _MODULO_PRIMITIVES:
        assert p.visibility == "community"
        assert p.organisation_id == MODULO_ORG_ID


def test_community_primitives_count():
    # 7 schemas + 7 agents + 2 workflows + 1 fixture + 3 pipeline_templates + 7 composites
    assert len(_MODULO_PRIMITIVES) == 27


def test_modulo_by_id_index():
    for p in _MODULO_PRIMITIVES:
        assert _MODULO_BY_ID[p.id] is p


# ---------------------------------------------------------------------------
# Dogfood pipeline community primitives
# ---------------------------------------------------------------------------


def test_dogfood_schemas_exist():
    schemas = _filter_modulo(primitive_type="schema", search=None)
    dogfood = [p for p in schemas if "dogfood" in (p.tags or [])]
    assert len(dogfood) == 5

    slugs = {p.slug for p in dogfood}
    assert slugs == {
        "github-issue-input",
        "structured-requirements",
        "code-diff-output",
        "test-result-output",
        "pr-output",
    }


def test_dogfood_agents_exist():
    agents = _filter_modulo(primitive_type="agent", search=None)
    dogfood = [p for p in agents if "dogfood" in (p.tags or [])]
    assert len(dogfood) == 5

    slugs = {p.slug for p in dogfood}
    assert slugs == {
        "issue-reader",
        "code-generator",
        "code-applier",
        "test-runner",
        "pr-creator",
    }


def test_dogfood_workflow_exists():
    workflows = _filter_modulo(primitive_type="workflow", search=None)
    dogfood = [p for p in workflows if "dogfood" in (p.tags or [])]
    assert len(dogfood) == 1

    wf = dogfood[0]
    assert wf.slug == "modulo-dogfood-pipeline"
    assert wf.name == "Modulo Dogfood Pipeline"


def test_dogfood_workflow_has_correct_nodes():
    workflows = _filter_modulo(primitive_type="workflow", search="dogfood")
    assert len(workflows) == 1
    nodes = workflows[0].content_json["nodes"]
    node_ids = {n["id"] for n in nodes}
    assert node_ids == {"issue-reader", "code-generator", "code-applier", "test-runner", "pr-creator"}


def test_dogfood_workflow_has_correct_edges():
    workflows = _filter_modulo(primitive_type="workflow", search="dogfood")
    assert len(workflows) == 1
    edges = workflows[0].content_json["edges"]
    assert len(edges) == 4
    assert edges[0]["source"] == "issue-reader"
    assert edges[0]["target"] == "code-generator"
    assert edges[1]["source"] == "code-generator"
    assert edges[1]["target"] == "code-applier"
    assert edges[2]["source"] == "code-applier"
    assert edges[2]["target"] == "test-runner"
    assert edges[3]["source"] == "test-runner"
    assert edges[3]["target"] == "pr-creator"


def test_dogfood_workflow_has_hitl_gate():
    workflows = _filter_modulo(primitive_type="workflow", search="dogfood")
    edges = workflows[0].content_json["edges"]
    hitl_edge = edges[3]
    assert "hitl_gate_config" in hitl_edge
    config = hitl_edge["hitl_gate_config"]
    assert config["human_only"] is False
    assert config["gate_id"] == "review_before_pr"
    assert config["overdue_threshold_minutes"] == 60


def test_dogfood_workflow_entry_point():
    workflows = _filter_modulo(primitive_type="workflow", search="dogfood")
    assert workflows[0].content_json["entry"] == "issue-reader"


def test_dogfood_agents_reference_correct_schemas():
    agents = _filter_modulo(primitive_type="agent", search=None)
    dogfood_agents = {a.slug: a for a in agents if "dogfood" in (a.tags or [])}

    assert dogfood_agents["issue-reader"].content_json["input_schema"] == "github-issue-input"
    assert dogfood_agents["issue-reader"].content_json["output_schema"] == "structured-requirements"
    assert dogfood_agents["code-generator"].content_json["input_schema"] == "structured-requirements"
    assert dogfood_agents["code-generator"].content_json["output_schema"] == "code-diff-output"
    assert dogfood_agents["code-applier"].content_json["input_schema"] == "code-diff-output"
    assert dogfood_agents["code-applier"].content_json["output_schema"] == "code-diff-output"
    assert dogfood_agents["test-runner"].content_json["input_schema"] == "code-diff-output"
    assert dogfood_agents["test-runner"].content_json["output_schema"] == "test-result-output"
    assert dogfood_agents["pr-creator"].content_json["input_schema"] == "test-result-output"
    assert dogfood_agents["pr-creator"].content_json["output_schema"] == "pr-output"


def test_dogfood_agents_have_connector_refs():
    agents = _filter_modulo(primitive_type="agent", search=None)
    dogfood_agents = {a.slug: a for a in agents if "dogfood" in (a.tags or [])}

    assert dogfood_agents["issue-reader"].content_json["connector_type_refs"] == [
        {"connector_type": "github", "capabilities": ["issue_read"]}
    ]
    assert dogfood_agents["code-generator"].content_json["connector_type_refs"] == []
    assert dogfood_agents["code-applier"].content_json["connector_type_refs"] == [
        {"connector_type": "shell", "capabilities": ["write"]}
    ]
    assert dogfood_agents["test-runner"].content_json["connector_type_refs"] == [
        {"connector_type": "shell", "capabilities": ["read", "write"]}
    ]
    assert dogfood_agents["pr-creator"].content_json["connector_type_refs"] == [
        {"connector_type": "github", "capabilities": ["create_pr"]}
    ]


def test_dogfood_agents_have_environment_capabilities():
    agents = _filter_modulo(primitive_type="agent", search=None)
    dogfood_agents = {a.slug: a for a in agents if "dogfood" in (a.tags or [])}

    assert dogfood_agents["issue-reader"].content_json["required_environment_capabilities"] == ["egress:github.com"]
    assert dogfood_agents["code-generator"].content_json["required_environment_capabilities"] == []
    assert dogfood_agents["code-applier"].content_json["required_environment_capabilities"] == ["filesystem:write"]
    assert dogfood_agents["test-runner"].content_json["required_environment_capabilities"] == [
        "shell:exec",
        "python3.12",
        "uv",
    ]
    assert dogfood_agents["pr-creator"].content_json["required_environment_capabilities"] == ["egress:github.com"]


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

    assert result.items == []
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
    assert len(result.items) == 3
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
    ):
        with pytest.raises(CommunityPrimitiveReadOnlyError):
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
    ):
        with pytest.raises(LookupError, match=str(missing_id)):
            await copy_to_adapt(session, org_id, missing_id)


async def test_copy_to_adapt_bumps_version():
    """Verify the new version is minor-bumped from the source."""
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org", version="2.3")
    copied = _fake_primitive()

    captured: dict = {}

    async def _capture(*args, **kwargs):  # type: ignore[misc]
        captured.update(kwargs)
        return copied

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch("modulo.core.library_service.get_library_primitive", new_callable=AsyncMock, return_value=source),
        patch("modulo.core.library_service.create_library_primitive", side_effect=_capture),
    ):
        await copy_to_adapt(session, org_id, source.id)

    assert captured["version"] == "2.4"
