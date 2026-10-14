"""FAR-1374 contract: real credentials persist, mask sentinels fail closed.

Inverse of the pre-fix repro (a masked ``save-as-composite`` template value
propagating into run snapshots and executing as a literal credential). Covers:

- the snapshot hop: a template storing the REAL credential value (what
  ``save-as-composite`` persists after the write-side mask removal) expands
  into the run snapshot unchanged;
- expansion-time rejection: a pre-fix or direct-DB-written sentinel is refused
  with ``MaskedCredentialExpansionError``, naming the sub-node, the field, and
  the env key under ``COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL``;
- detection is the substring rule (``SENSITIVE_VALUE_MASK in value``), so a
  sentinel embedded in a wider string (``Bearer ......``) is caught too;
- all four credential-bearing fields are guarded, not just ``env_vars``.

Fixture values are fake (``FAKE_CREDENTIAL_FOR_TEST``) - never real-shaped
tokens in test files.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from modulo.api.middleware.sensitive_mask import resolve_and_reject_mask_sentinels
from modulo.core.composite_engine.expander import (
    MaskedCredentialExpansionError,
    expand_composite_node,
    expand_composites_in_graph,
)
from modulo.core.graph_validator.mask_sentinel import (
    GRAPH_NODE_SECRET_FIELDS,
    MASK_SENTINEL_ISSUE_CODE,
)
from modulo.core.secret_patterns import SENSITIVE_VALUE_MASK
from modulo.db.crud.pipeline_snapshot import create_snapshot_from_live_graph
from modulo.db.models.pipeline_snapshot import PipelineSnapshot

_FAKE_CREDENTIAL = "FAKE_CREDENTIAL_FOR_TEST"


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


def _scalars_result(values: list[object]) -> MagicMock:
    result = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    scalars_mock.__iter__.return_value = iter(values)
    result.scalars.return_value = scalars_mock
    return result


def _bind_lock_connection(session: AsyncMock) -> Any:
    """Build the stubbed DEDICATED lock engine and return a patch that installs it.

    FAR-1287: the snapshot advisory lock is acquired/released on a connection
    from a dedicated NullPool engine resolved by ``_dedicated_lock_engine`` —
    never on the caller's session or its pool — so no lock/unlock statement ever
    appears in ``session.execute``'s sequence. Enter the returned patch around
    the snapshot call.
    """
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = True
    lock_conn = AsyncMock()
    lock_conn.execute.side_effect = [lock_result, MagicMock()]  # try-lock, then unlock
    engine = MagicMock(spec=AsyncEngine)
    engine.connect = AsyncMock(return_value=lock_conn)
    session.bind = engine
    return patch("modulo.db.crud.pipeline_snapshot._dedicated_lock_engine", return_value=engine)


def _template_mock(template_id: uuid.UUID, sub_graph: dict[str, Any]) -> MagicMock:
    template = MagicMock()
    template.id = template_id
    template.version = "0.1.0"
    template.sub_pipeline_graph_json = sub_graph
    return template


def _session_returning(*values: object) -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = list(values)
    return session


def _composite_graph(
    *,
    env_value: str,
    template_id: uuid.UUID,
    composite_id: str = "comp-1",
) -> tuple[list[dict[str, Any]], AsyncMock]:
    """Build (parent graph nodes, session) for a one-sub-node composite."""
    template = _template_mock(
        template_id,
        {
            "nodes": [
                {
                    "id": "gh-node",
                    "node_type": "sandbox_agent",
                    "env_vars": {"GITHUB_TOKEN": env_value},
                }
            ],
            "edges": [],
        },
    )
    nodes: list[dict[str, Any]] = [
        {
            "id": composite_id,
            "node_type": "composite",
            "composite_ref": str(template_id),
        }
    ]
    return nodes, _session_returning(_scalar_result(template))


async def test_snapshot_hop_carries_real_credential_from_save_as_composite() -> None:
    """End-to-end hop: save-as-composite's persist step -> run snapshot.

    The persist step (``resolve_and_reject_mask_sentinels`` with no stored
    counterpart - what ``save-as-composite`` calls) keeps the REAL credential,
    and ``create_snapshot_from_live_graph`` expands it into ``graph_json``
    unchanged: no mask literal ever reaches the run snapshot.
    """
    org_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()
    template_id = uuid.uuid4()
    composite_id = uuid.uuid4()

    # save-as-composite persist step: the pipeline's sub-node carries the real
    # credential; no write-side masking is applied (FAR-1374).
    pipeline_sub_nodes = [
        {
            "id": "gh-node",
            "node_type": "sandbox_agent",
            "env_vars": {"GITHUB_TOKEN": _FAKE_CREDENTIAL},
        }
    ]
    persisted_nodes = resolve_and_reject_mask_sentinels(list(pipeline_sub_nodes), [])
    assert persisted_nodes[0]["env_vars"] == {"GITHUB_TOKEN": _FAKE_CREDENTIAL}

    template = _template_mock(template_id, {"nodes": persisted_nodes, "edges": []})

    pipeline = MagicMock()
    pipeline.id = pipeline_id
    pipeline.organisation_id = org_id
    pipeline.graph_nodes_json = [
        {
            "id": str(composite_id),
            "node_type": "composite",
            "composite_ref": str(template_id),
        }
    ]
    pipeline.run_context_defaults = {}

    session = AsyncMock(spec=AsyncSession)
    session.execute.side_effect = [
        _scalar_result(pipeline),  # 1 pipeline
        _scalars_result([]),  # 2 edges
        _scalar_result(template),  # 3 composite template (expander)
        MagicMock(),  # FAR-1625 allocation row lock (result ignored)
        _scalar_result(0),  # 4 snapshot version max
        _scalars_result([]),  # 5 guardrail rows
        _scalars_result([]),  # 6 policy-gate rows (FAR-967 chunk 10 pin loader)
    ]

    # FAR-1287: the advisory lock is acquired/released on the dedicated lock
    # connection opened from the dedicated engine — never on the caller's session.
    with _bind_lock_connection(session):
        snapshot = await create_snapshot_from_live_graph(session, pipeline_id=pipeline_id)

    assert isinstance(snapshot, PipelineSnapshot)
    nodes = snapshot.graph_json["nodes"]
    assert len(nodes) == 1
    sub = nodes[0]
    assert sub["_composite_parent_id"] == str(composite_id)
    # The snapshot carries the REAL credential, not the mask sentinel.
    assert sub["env_vars"] == {"GITHUB_TOKEN": _FAKE_CREDENTIAL}


def test_resolve_rejects_scalar_field_sentinel_once_even_when_unresolved() -> None:
    """A sentinel in a non-container credential field is found by BOTH scans.

    ``find_mask_sentinel_values`` sees it survive the merge (the merge skips a
    non-dict field), and ``find_unresolved_mask_sentinels`` sees no stored
    counterpart; the de-duplication keeps a single finding and the request
    still fails closed with 422 - never double-counted, never silently dropped.
    """
    nodes = [{"id": "gh-node", "env_vars": f"Bearer {SENSITIVE_VALUE_MASK}"}]

    with pytest.raises(HTTPException) as exc_info:
        resolve_and_reject_mask_sentinels(list(nodes), [])

    assert exc_info.value.status_code == 422
    detail = str(exc_info.value.detail)
    assert MASK_SENTINEL_ISSUE_CODE in detail
    assert "gh-node" in detail
    assert "env_vars" in detail


async def test_expansion_rejects_stored_sentinel_naming_sub_node_and_key() -> None:
    """A pre-fix / direct-DB-written sentinel fails closed at expansion.

    The failure names the sub-node and the env key under the shared
    ``COMPOSITE_SUBGRAPH_MASKED_CREDENTIAL`` code, and never silently drops
    the key or substitutes a host credential.
    """
    template_id = uuid.uuid4()
    nodes, session = _composite_graph(env_value=SENSITIVE_VALUE_MASK, template_id=template_id)

    with pytest.raises(MaskedCredentialExpansionError) as exc_info:
        await expand_composites_in_graph(session, None, nodes, [])

    message = str(exc_info.value)
    assert MASK_SENTINEL_ISSUE_CODE in message
    assert "gh-node" in message
    assert "GITHUB_TOKEN" in message
    assert exc_info.value.error_code == MASK_SENTINEL_ISSUE_CODE
    # ValueError subclass: every existing composite-expansion handler keeps
    # catching it.
    assert isinstance(exc_info.value, ValueError)


async def test_expansion_rejects_sentinel_embedded_in_wider_string() -> None:
    """Substring detection: ``Bearer ......`` is a sentinel, not a real value."""
    template_id = uuid.uuid4()
    nodes, session = _composite_graph(
        env_value=f"Bearer {SENSITIVE_VALUE_MASK}",
        template_id=template_id,
    )

    with pytest.raises(MaskedCredentialExpansionError) as exc_info:
        await expand_composites_in_graph(session, None, nodes, [])

    message = str(exc_info.value)
    assert "gh-node" in message
    assert "GITHUB_TOKEN" in message


@pytest.mark.parametrize("field", GRAPH_NODE_SECRET_FIELDS)
async def test_expansion_rejects_sentinel_in_every_credential_field(field: str) -> None:
    """All four credential-bearing fields are guarded, not just ``env_vars``."""
    template_id = uuid.uuid4()
    template = _template_mock(
        template_id,
        {
            "nodes": [
                {
                    "id": "gh-node",
                    "node_type": "sandbox_agent",
                    field: {"TARGET_KEY": SENSITIVE_VALUE_MASK},
                }
            ],
            "edges": [],
        },
    )
    nodes: list[dict[str, Any]] = [
        {
            "id": "comp-1",
            "node_type": "composite",
            "composite_ref": str(template_id),
        }
    ]
    session = _session_returning(_scalar_result(template))

    with pytest.raises(MaskedCredentialExpansionError) as exc_info:
        await expand_composites_in_graph(session, None, nodes, [])

    message = str(exc_info.value)
    assert field in message
    assert "TARGET_KEY" in message


def test_expand_composite_node_rejects_stored_sentinel() -> None:
    """The sync public expansion path applies the same gate."""
    template_id = uuid.uuid4()
    composite_template = {
        "nodes": [
            {
                "id": "gh-node",
                "node_type": "sandbox_agent",
                "env_vars": {"GITHUB_TOKEN": SENSITIVE_VALUE_MASK},
            }
        ],
        "edges": [],
    }
    node_def = {"id": "comp-1", "node_type": "composite", "composite_ref": str(template_id)}

    with pytest.raises(MaskedCredentialExpansionError) as exc_info:
        expand_composite_node(node_def, composite_template, {})

    message = str(exc_info.value)
    assert MASK_SENTINEL_ISSUE_CODE in message
    assert "gh-node" in message
    assert "GITHUB_TOKEN" in message


async def test_expansion_passes_credential_bearing_template_through() -> None:
    """The positive control: a real credential expands unchanged (no false positive)."""
    template_id = uuid.uuid4()
    nodes, session = _composite_graph(env_value=_FAKE_CREDENTIAL, template_id=template_id)

    expanded_nodes, _edges, _bindings = await expand_composites_in_graph(session, None, nodes, [])

    sub = next(n for n in expanded_nodes if n.get("_composite_parent_id") == "comp-1")
    assert sub["env_vars"] == {"GITHUB_TOKEN": _FAKE_CREDENTIAL}
    assert expanded_nodes[0]["node_type"] == "sandbox_agent"
