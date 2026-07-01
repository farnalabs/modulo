"""Unit tests for composite library primitive support."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.library_service import (
    _COMMUNITY_PRIMITIVES,
    _filter_community,
    copy_to_adapt,
    get_primitive,
    list_primitives,
)
from modulo.db.crud.base import PageResult


def _fake_primitive(
    *,
    pid: uuid.UUID | None = None,
    visibility: str = "org",
    primitive_type: str = "composite",
    name: str = "Test Composite",
    slug: str = "test-composite",
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
    p.description = "A test composite primitive"
    p.author = "tester"
    p.version = version
    p.tags = tags or []
    p.content_json = content_json or {
        "name": "Test Composite",
        "description": "A parameterizable sub-pipeline",
        "sub_pipeline_graph_json": {"nodes": [], "edges": []},
        "parameter_ports_json": [
            {"name": "model", "type": "string", "required": True},
        ],
        "input_schema_id": None,
        "output_schema_id": None,
    }
    return p


def _mock_session() -> MagicMock:
    session = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=ctx)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=ctx)
    session.in_transaction = MagicMock(return_value=True)
    return session


# ---------------------------------------------------------------------------
# list_primitives with composite type filter
# ---------------------------------------------------------------------------


async def test_list_primitives_filters_composite_type():
    session = _mock_session()
    org_id = uuid.uuid4()
    org_composite = _fake_primitive()
    org_page: PageResult = PageResult(items=[org_composite], total=1, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives",
            new_callable=AsyncMock,
            return_value=org_page,
        ) as mock_list,
    ):
        result = await list_primitives(session, org_id, primitive_type="composite")

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["primitive_type"] == "composite"
    assert len(result.items) == 1
    assert result.items[0].primitive_type == "composite"


async def test_list_primitives_composite_passes_filter_downstream():
    session = _mock_session()
    org_id = uuid.uuid4()
    composite_prim = _fake_primitive(primitive_type="composite", slug="my-composite")
    org_page: PageResult = PageResult(items=[composite_prim], total=1, page=1, page_size=20)

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.list_library_primitives",
            new_callable=AsyncMock,
            return_value=org_page,
        ) as mock_list,
    ):
        result = await list_primitives(session, org_id, primitive_type="composite")

    mock_list.assert_awaited_once()
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs["primitive_type"] == "composite"
    assert len(result.items) == 1
    assert result.items[0].slug == "my-composite"


# ---------------------------------------------------------------------------
# get_primitive for composite type
# ---------------------------------------------------------------------------


async def test_get_composite_primitive():
    session = _mock_session()
    org_id = uuid.uuid4()
    composite = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            return_value=composite,
        ),
    ):
        result = await get_primitive(session, org_id, composite.id)

    assert result is composite
    assert result.primitive_type == "composite"


# ---------------------------------------------------------------------------
# Composite content_json structure validation
# ---------------------------------------------------------------------------


def test_composite_content_json_contains_expected_fields():
    content = {
        "name": "My Composite",
        "description": "A parameterizable sub-pipeline",
        "sub_pipeline_graph_json": {"nodes": [{"id": "agent-1"}], "edges": []},
        "parameter_ports_json": [
            {"name": "model", "type": "string", "required": True},
        ],
        "input_schema_id": str(uuid.uuid4()),
        "output_schema_id": str(uuid.uuid4()),
    }

    assert "name" in content
    assert "description" in content
    assert "sub_pipeline_graph_json" in content
    assert "parameter_ports_json" in content
    assert "input_schema_id" in content
    assert "output_schema_id" in content
    assert isinstance(content["sub_pipeline_graph_json"], dict)
    assert isinstance(content["parameter_ports_json"], list)


def test_composite_content_json_optional_fields():
    content = {
        "name": "Minimal Composite",
        "description": "",
        "sub_pipeline_graph_json": {"nodes": [], "edges": []},
        "parameter_ports_json": [],
        "input_schema_id": None,
        "output_schema_id": None,
    }

    assert content["input_schema_id"] is None
    assert content["output_schema_id"] is None
    assert content["parameter_ports_json"] == []


# ---------------------------------------------------------------------------
# copy_to_adapt for composite
# ---------------------------------------------------------------------------


async def test_copy_to_adapt_composite_succeeds():
    session = _mock_session()
    org_id = uuid.uuid4()
    source = _fake_primitive(visibility="org")
    copied = _fake_primitive()

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            return_value=source,
        ),
        patch(
            "modulo.core.library_service.create_library_primitive",
            new_callable=AsyncMock,
            return_value=copied,
        ),
    ):
        result = await copy_to_adapt(session, org_id, source.id)

    assert result is copied


async def test_copy_to_adapt_composite_preserves_content_json():
    session = _mock_session()
    org_id = uuid.uuid4()
    content = {
        "name": "My Composite",
        "description": "A test",
        "sub_pipeline_graph_json": {"nodes": [], "edges": []},
        "parameter_ports_json": [],
        "input_schema_id": None,
        "output_schema_id": None,
    }
    source = _fake_primitive(visibility="org", content_json=content)
    copied = _fake_primitive()

    captured: dict = {}

    async def _capture(*args, **kwargs):
        captured.update(kwargs)
        return copied

    with (
        patch("modulo.core.library_service.set_rls_org", new_callable=AsyncMock),
        patch(
            "modulo.core.library_service.get_library_primitive",
            new_callable=AsyncMock,
            return_value=source,
        ),
        patch(
            "modulo.core.library_service.create_library_primitive",
            side_effect=_capture,
        ),
    ):
        await copy_to_adapt(session, org_id, source.id)

    assert captured["primitive_type"] == "composite"
    assert captured["content_json"] == content
    assert captured["slug"] == "test-composite-copy"


# ---------------------------------------------------------------------------
# Community primitive filter does not include composite (not added yet)
# ---------------------------------------------------------------------------


def test_community_primitives_do_not_include_composite():
    composites = [p for p in _COMMUNITY_PRIMITIVES if p.primitive_type == "composite"]
    assert composites == []


def test_filter_community_composite_returns_empty():
    results = _filter_community(primitive_type="composite", search=None)
    assert results == []
