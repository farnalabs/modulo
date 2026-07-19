"""Unit tests for convert-to-agent and revert-to-manual endpoint handlers.

Tests the handler functions directly with mocked dependencies.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.routes.pipelines import (
    ConvertToAgentRequest,
    convert_node_to_agent_endpoint,
    revert_node_to_manual_endpoint,
)
from modulo.auth.jwt import AuthenticatedPrincipal

ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
PIPELINE_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
NODE_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
AGENT_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
CONNECTOR_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
MODEL_BACKEND_ID = uuid.UUID("55555555-5555-5555-5555-555555555555")
SNAPSHOT_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


def make_principal():
    return AuthenticatedPrincipal(
        username="testuser",
        organisation_id=ORG_ID,
        account_id=USER_ID,
        org_role="admin",
    )


def make_session():
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=None)
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin_nested = MagicMock(return_value=begin_nested_cm)
    session.in_transaction = MagicMock(return_value=True)
    bind = MagicMock()
    bind.dialect.name = "sqlite"
    session.get_bind = MagicMock(return_value=bind)
    session.info = {}
    session.add = MagicMock()
    default_result = MagicMock()
    default_result.scalar_one_or_none = MagicMock(return_value=None)
    default_result.scalar_one = MagicMock(return_value=0)
    scalar_mock = MagicMock()
    scalar_mock.all = MagicMock(return_value=[])
    default_result.scalars = MagicMock(return_value=scalar_mock)
    session.execute = AsyncMock(return_value=default_result)
    return session


def make_pipeline_row(nodes=None, edges=None):
    pipeline = MagicMock()
    pipeline.id = PIPELINE_ID
    pipeline.graph_nodes_json = nodes or []
    pipeline.edges = edges or []
    return pipeline


def make_agent_mock():
    agent = MagicMock()
    agent.id = AGENT_ID
    agent.organisation_id = ORG_ID
    return agent


def make_connector_mock(connector_type="github"):
    connector = MagicMock()
    connector.id = CONNECTOR_ID
    connector.organisation_id = ORG_ID
    connector.connector_type_id = connector_type
    return connector


def make_model_backend_mock():
    mb = MagicMock()
    mb.id = MODEL_BACKEND_ID
    mb.organisation_id = ORG_ID
    return mb


def make_manual_node():
    return {
        "id": str(NODE_ID),
        "node_type": "manual",
        "position": {"x": 0, "y": 0},
        "label": "qa-review",
    }


def make_agent_node():
    return {
        "id": str(NODE_ID),
        "node_type": "agent",
        "agent_id": str(AGENT_ID),
        "position": {"x": 0, "y": 0},
        "label": "qa-review",
    }


def make_convert_body():
    return ConvertToAgentRequest(
        agent_id=AGENT_ID,
        connector_binding={"type": "github", "instance_id": CONNECTOR_ID},
        model_backend_id=MODEL_BACKEND_ID,
    )


def make_snapshot(nodes=None):
    snapshot = MagicMock()
    snapshot.id = SNAPSHOT_ID
    snapshot.graph_json = {
        "nodes": nodes or [{"id": str(NODE_ID), "node_type": "manual", "output_schema_id": str(uuid.uuid4())}],
        "edges": [],
    }
    return snapshot


def setup_execute_side_effect(session, results):
    call_count = 0

    async def execute_side(*args, **kwargs):
        nonlocal call_count
        idx = call_count
        call_count += 1
        result = MagicMock()
        if idx < len(results):
            val = results[idx]
            if isinstance(val, type) and issubclass(val, Exception):
                raise val
            result.scalar_one_or_none = MagicMock(return_value=val)
        else:
            val = None
            result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalar_one = MagicMock(return_value=0)
        scalar_result = MagicMock()
        scalar_result.all = MagicMock(return_value=val if isinstance(val, list) else [])
        result.scalars = MagicMock(return_value=scalar_result)
        return result

    session.execute = AsyncMock(side_effect=execute_side)


# ===========================================================================
#  Convert-to-agent tests
# ===========================================================================


class TestConvertToAgent:
    """Tests for convert_node_to_agent_endpoint."""

    async def test_happy_path(self):
        session = make_session()
        setup_execute_side_effect(
            session,
            [
                make_pipeline_row(nodes=[make_manual_node()]),
                [],
                make_agent_mock(),
                make_connector_mock(),
                make_model_backend_mock(),
            ],
        )
        principal = make_principal()
        body = make_convert_body()

        saved_nodes = [make_manual_node()]
        saved_nodes[0]["node_type"] = "agent"
        saved_nodes[0]["agent_id"] = str(AGENT_ID)

        with patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(saved_nodes, [])):
            resp = await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert resp is not None
        assert hasattr(resp, "nodes")

    async def test_pipeline_not_found(self):
        session = make_session()
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_node_not_found(self):
        session = make_session()
        other_id = uuid.uuid4()
        nodes = [{"id": str(other_id), "node_type": "manual", "position": {"x": 0, "y": 0}}]
        setup_execute_side_effect(session, [make_pipeline_row(nodes=nodes)])
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_node_not_manual(self):
        session = make_session()
        nodes = [{"id": str(NODE_ID), "node_type": "agent", "position": {"x": 0, "y": 0}}]
        setup_execute_side_effect(session, [make_pipeline_row(nodes=nodes)])
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_agent_not_found(self):
        session = make_session()
        setup_execute_side_effect(
            session,
            [
                make_pipeline_row(nodes=[make_manual_node()]),
                [],
                None,
            ],
        )
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Agent not found" in excinfo.value.detail

    async def test_connector_not_found(self):
        session = make_session()
        setup_execute_side_effect(
            session,
            [
                make_pipeline_row(nodes=[make_manual_node()]),
                [],
                make_agent_mock(),
                None,
            ],
        )
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Connector not found" in excinfo.value.detail

    async def test_connector_type_mismatch(self):
        session = make_session()
        setup_execute_side_effect(
            session,
            [
                make_pipeline_row(nodes=[make_manual_node()]),
                [],
                make_agent_mock(),
                make_connector_mock(connector_type="gitlab"),
            ],
        )
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_model_backend_not_found(self):
        session = make_session()
        setup_execute_side_effect(
            session,
            [
                make_pipeline_row(nodes=[make_manual_node()]),
                [],
                make_agent_mock(),
                make_connector_mock(),
                None,
            ],
        )
        principal = make_principal()
        body = make_convert_body()

        with pytest.raises(HTTPException) as excinfo:
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Model backend not found" in excinfo.value.detail

    async def test_programming_error_caught(self):
        session = make_session()
        setup_execute_side_effect(
            session,
            [
                make_pipeline_row(nodes=[make_manual_node()]),
                [],
                make_agent_mock(),
                make_connector_mock(),
                make_model_backend_mock(),
            ],
        )
        principal = make_principal()
        body = make_convert_body()

        with (
            patch(
                "modulo.api.routes.pipelines.replace_pipeline_graph",
                side_effect=ProgrammingError("stmt", {}, "table not found"),
            ),
            pytest.raises(HTTPException) as excinfo,
        ):
            await convert_node_to_agent_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                req=body,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_501_NOT_IMPLEMENTED


# ===========================================================================
#  Revert-to-manual tests
# ===========================================================================


class TestRevertToManual:
    """Tests for revert_node_to_manual_endpoint."""

    async def test_happy_path(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot()

        saved_nodes = [make_agent_node()]
        saved_nodes[0]["node_type"] = "manual"
        saved_nodes[0].pop("agent_id", None)
        saved_nodes[0]["output_schema_id"] = str(uuid.uuid4())
        saved_nodes[0]["label"] = "qa-review"

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            patch("modulo.api.routes.pipelines.replace_pipeline_graph", return_value=(saved_nodes, [])),
        ):
            resp = await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert resp is not None
        assert hasattr(resp, "nodes")

    async def test_pipeline_not_found(self):
        session = make_session()
        principal = make_principal()

        with pytest.raises(HTTPException) as excinfo:
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_node_not_found(self):
        session = make_session()
        other_id = uuid.uuid4()
        nodes = [{"id": str(other_id), "node_type": "agent", "position": {"x": 0, "y": 0}}]
        setup_execute_side_effect(session, [make_pipeline_row(nodes=nodes)])
        principal = make_principal()

        with pytest.raises(HTTPException) as excinfo:
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_node_not_agent(self):
        session = make_session()
        nodes = [{"id": str(NODE_ID), "node_type": "manual", "position": {"x": 0, "y": 0}}]
        setup_execute_side_effect(session, [make_pipeline_row(nodes=nodes)])
        principal = make_principal()

        with pytest.raises(HTTPException) as excinfo:
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_snapshot_not_found(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=None),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )
        assert excinfo.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Snapshot not found" in excinfo.value.detail

    async def test_snapshot_no_node(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot(
            nodes=[
                {"id": str(uuid.uuid4()), "node_type": "manual", "output_schema_id": str(uuid.uuid4())},
            ]
        )

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_snapshot_node_not_manual(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot(
            nodes=[
                {"id": str(NODE_ID), "node_type": "agent", "output_schema_id": str(uuid.uuid4())},
            ]
        )

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_snapshot_no_output_schema(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot(
            nodes=[
                {"id": str(NODE_ID), "node_type": "manual"},
            ]
        )

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT

    async def test_integrity_error_returns_409(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot()

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            patch("modulo.api.routes.pipelines._save_graph", side_effect=IntegrityError("stmt", "params", "orig")),
            patch("modulo.api.routes.pipelines.append_audit_event", AsyncMock()),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_409_CONFLICT

    async def test_programming_error_returns_501(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot()

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            patch("modulo.api.routes.pipelines._save_graph", side_effect=ProgrammingError("stmt", "params", "orig")),
            patch("modulo.api.routes.pipelines.append_audit_event", AsyncMock()),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_501_NOT_IMPLEMENTED

    async def test_sqlalchemy_error_returns_503(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot()

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            patch("modulo.api.routes.pipelines._save_graph", side_effect=SQLAlchemyError("stmt", "params", "orig")),
            patch("modulo.api.routes.pipelines.append_audit_event", AsyncMock()),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE

    async def test_unexpected_exception_returns_500(self):
        session = make_session()
        setup_execute_side_effect(session, [make_pipeline_row(nodes=[make_agent_node()])])
        principal = make_principal()
        snapshot = make_snapshot()

        with (
            patch("modulo.api.routes.pipelines.get_snapshot_detail", return_value=snapshot),
            patch("modulo.api.routes.pipelines._save_graph", side_effect=ValueError("unexpected")),
            patch("modulo.api.routes.pipelines.append_audit_event", AsyncMock()),
            pytest.raises(HTTPException) as excinfo,
        ):
            await revert_node_to_manual_endpoint(
                pipeline_id=PIPELINE_ID,
                node_id=NODE_ID,
                snapshot_id=SNAPSHOT_ID,
                session=session,
                principal=principal,
            )

        assert excinfo.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
